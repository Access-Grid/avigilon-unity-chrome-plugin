"""
HTTP API client for Avigilon Unity access control system.

Ported from avigilon-unity-service with identical API surface.
Handles both JSON and XML responses, session management, CSRF tokens,
and SSL verification bypass for self-signed certificates.
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

import requests
import urllib3

from .constants import (
    HTTP_TIMEOUT,
    HTTP_USER_AGENT,
    AVIGILON_TOKEN_STATUS_ACTIVE,
    AVIGILON_TOKEN_TYPE_STANDARD,
)

logger = logging.getLogger(__name__)


class AvigilonAuthError(Exception):
    pass


class AvigilonAPIError(Exception):
    pass


class AvigilonClient:
    """
    Session-based HTTP client for Avigilon Unity.
    SSL verification is disabled by default for self-signed certs.
    """

    def __init__(self, host: str, username: str, password: str, verify_ssl: bool = False):
        self.base_url = f"https://{host}"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl

        self.session = requests.Session()
        self.session.headers.update({'User-Agent': HTTP_USER_AGENT})
        self._logged_in = False
        self._csrf_meta_token = ''
        # Some Avigilon Unity deployments use "plasec" element/field names
        # (the product's legacy brand), others use "avigilon". Detected from
        # login HTML and XML responses; defaults to avigilon.
        self._prefix = 'avigilon'

        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        logger.info(
            f"AvigilonClient init: base_url={self.base_url} "
            f"username={self.username!r} verify_ssl={self.verify_ssl} "
            f"timeout={HTTP_TIMEOUT}s user_agent={HTTP_USER_AGENT!r}"
        )

    @property
    def csrf_token(self) -> str:
        # Avigilon Unity puts the CSRF token in a <meta name="csrf-token"> tag
        # rather than a cookie. Prefer the value scraped from the login body;
        # fall back to the cookie for deployments that expose it there.
        return self._csrf_meta_token or self.session.cookies.get('XSRF-TOKEN', '')

    @staticmethod
    def _extract_csrf_meta(html: str) -> str:
        m = re.search(
            r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
            html or '',
            re.IGNORECASE,
        )
        return m.group(1) if m else ''

    def _detect_prefix_from_text(self, text: str) -> None:
        """Sniff plasec vs avigilon field-prefix from an HTML/JSON/XML body.

        Looks for field names specific enough that a false positive is
        unlikely (Fname/Lname/Idstatus). First match wins; plasec is checked
        first because newer deployments have rebranded to avigilon and
        legacy-branded ones are the ones that need detection.
        """
        if not text:
            return
        signals = ('Fname', 'Lname', 'Idstatus', 'identityEmailaddress')
        for prefix in ('plasec', 'avigilon'):
            for sig in signals:
                if f'{prefix}{sig}' in text:
                    if self._prefix != prefix:
                        logger.info(f"Detected Avigilon field prefix: {prefix!r}")
                    self._prefix = prefix
                    return

    def login(self) -> bool:
        login_url = f"{self.base_url}/sessions"
        logger.info(f"Avigilon login: POST {login_url} (user={self.username!r}, verify_ssl={self.verify_ssl}, body=json)")
        try:
            resp = self.session.post(
                login_url,
                json={'login': self.username, 'password': self.password},
                headers={
                    'Accept': 'application/json, text/javascript, */*; q=0.01',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Origin': self.base_url,
                    'Referer': f'{self.base_url}/sessions/new',
                },
                allow_redirects=False,
                verify=self.verify_ssl,
                timeout=HTTP_TIMEOUT,
            )
            logger.info(
                f"Avigilon login response: status={resp.status_code} "
                f"url={resp.url} elapsed={resp.elapsed.total_seconds():.2f}s"
            )
            logger.debug(f"Avigilon login response headers: {dict(resp.headers)}")
            cookie_names = [c.name for c in self.session.cookies]
            logger.info(f"Avigilon login cookies set: {cookie_names}")
            body_preview = (resp.text or '')[:500].replace('\n', ' ')
            logger.debug(f"Avigilon login body (first 500 chars): {body_preview!r}")

            if resp.status_code == 200 and self.session.cookies.get('_session_id'):
                self._logged_in = True
                self._detect_prefix_from_text(resp.text)
                # JSON login response has no CSRF meta tag. Fetch the
                # dashboard HTML to pick up both CSRF and confirm the prefix.
                self._fetch_dashboard_state()
                cookie_csrf = self.session.cookies.get('XSRF-TOKEN', '')
                logger.info(
                    f"Avigilon login SUCCESS: _session_id cookie present, "
                    f"csrf from meta={'set' if self._csrf_meta_token else 'missing'}, "
                    f"csrf from cookie={'set' if cookie_csrf else 'missing'}, "
                    f"field prefix={self._prefix!r}"
                )
                if not self._csrf_meta_token and not cookie_csrf:
                    logger.warning(
                        "No CSRF token found after login. Write operations may be rejected."
                    )
                return True
            if resp.status_code == 404:
                logger.error(
                    f"Avigilon login FAILED: 404 Not Found at {login_url} — "
                    f"verify the host/IP is correct and that the Avigilon Unity "
                    f"web server is actually listening on HTTPS at this address"
                )
            elif resp.status_code in (401, 403):
                logger.error(
                    f"Avigilon login FAILED: HTTP {resp.status_code} — "
                    f"credentials rejected by server"
                )
            elif resp.status_code >= 500:
                logger.error(
                    f"Avigilon login FAILED: HTTP {resp.status_code} — "
                    f"server-side error at {login_url}"
                )
            else:
                logger.error(
                    f"Avigilon login FAILED: status={resp.status_code} but no "
                    f"_session_id cookie was set. Cookies received: {cookie_names}. "
                    f"Final URL: {resp.url}"
                )
        except requests.exceptions.SSLError as e:
            logger.error(
                f"Avigilon login FAILED: SSL error talking to {login_url}: {e}. "
                f"(verify_ssl={self.verify_ssl}; if this is a self-signed cert, "
                f"SSL bypass should be on)"
            )
        except requests.exceptions.ConnectTimeout as e:
            logger.error(
                f"Avigilon login FAILED: connection timed out after {HTTP_TIMEOUT}s "
                f"connecting to {login_url}: {e}. Host may be unreachable or firewalled."
            )
        except requests.exceptions.ReadTimeout as e:
            logger.error(
                f"Avigilon login FAILED: read timed out after {HTTP_TIMEOUT}s "
                f"waiting for {login_url}: {e}. Host accepted the connection but "
                f"did not respond in time."
            )
        except requests.exceptions.ConnectionError as e:
            logger.error(
                f"Avigilon login FAILED: connection error to {login_url}: "
                f"{type(e).__name__}: {e}. Check that the host is reachable "
                f"(DNS, network, firewall, port 443)."
            )
        except requests.RequestException as e:
            logger.error(
                f"Avigilon login FAILED: {type(e).__name__}: {e} "
                f"(url={login_url})"
            )
        except Exception as e:
            logger.exception(
                f"Avigilon login FAILED: unexpected {type(e).__name__}: {e}"
            )
        return False

    def _fetch_dashboard_state(self) -> None:
        """Fetch `/` after login to scrape CSRF meta and confirm field prefix.

        JSON login sets `_session_id` but doesn't return HTML, so there's no
        CSRF meta tag in its response. The dashboard HTML has one.
        """
        try:
            resp = self.session.get(
                f"{self.base_url}/",
                headers={'Accept': 'text/html,application/xhtml+xml'},
                allow_redirects=True,
                verify=self.verify_ssl,
                timeout=HTTP_TIMEOUT,
            )
            logger.debug(
                f"Dashboard fetch: status={resp.status_code} "
                f"content_length={len(resp.content)}"
            )
            csrf = self._extract_csrf_meta(resp.text)
            if csrf:
                self._csrf_meta_token = csrf
            self._detect_prefix_from_text(resp.text)
        except Exception as e:
            logger.warning(f"Dashboard state fetch failed: {e} (writes may still work)")

    def _ensure_authenticated(self):
        if not self._logged_in:
            if not self.login():
                raise AvigilonAuthError("Cannot authenticate with Avigilon server")

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        self._ensure_authenticated()

        headers = kwargs.pop('headers', {})
        if method.upper() in ('POST', 'PUT', 'PATCH', 'DELETE'):
            headers.setdefault('X-CSRF-Token', self.csrf_token)

        resp = self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            verify=self.verify_ssl,
            timeout=HTTP_TIMEOUT,
            **kwargs,
        )

        if self._is_session_expired(resp, path):
            logger.warning("Avigilon session expired — re-authenticating")
            self._logged_in = False
            if self.login():
                headers['X-CSRF-Token'] = self.csrf_token
                resp = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    verify=self.verify_ssl,
                    timeout=HTTP_TIMEOUT,
                    **kwargs,
                )
        return resp

    def _is_session_expired(self, resp: requests.Response, requested_path: str) -> bool:
        if resp.status_code == 302:
            return '/sessions' in resp.headers.get('Location', '')
        if resp.status_code == 200 and requested_path != '/sessions':
            return '/sessions' in resp.url
        return False

    # ------------------------------------------------------------------
    # Identity operations
    # ------------------------------------------------------------------

    def get_all_identities(self) -> List[Dict]:
        # Avigilon Unity's /identities.json returns 406 — the controller only
        # offers HTML/XML representations. Use the XML search endpoint.
        identities = self.get_identities_xml()
        logger.debug(f"Found {len(identities)} identities (via XML)")
        return identities

    def get_identity(self, identity_id: str) -> Optional[Dict]:
        resp = self._request(
            'GET', f'/identities/{identity_id}.json',
            headers={'Accept': 'application/json'},
        )
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
            raw = body.get('data', body) if isinstance(body, dict) else body
            if isinstance(raw, dict):
                return self._normalize_identity(raw)
        except Exception as e:
            logger.error(f"get_identity {identity_id} parse failed: {e}")
        return None

    def get_identity_tokens(self, identity_id: str) -> List[Dict]:
        resp = self._request(
            'GET', f'/identities/{identity_id}/tokens.json',
            headers={'Accept': 'application/json'},
        )
        if resp.status_code != 200:
            return []
        try:
            body = resp.json()
            if isinstance(body, list):
                items = body
            elif isinstance(body, dict):
                raw_list = body.get('tokens') or body.get('data')
                if isinstance(raw_list, list):
                    items = raw_list
                elif isinstance(raw_list, dict) and raw_list:
                    items = [raw_list]
                else:
                    items = []
            else:
                items = []
            return [self._normalize_token(t, identity_id) for t in items]
        except Exception as e:
            logger.error(f"get_identity_tokens {identity_id} parse failed: {e}")
        return []

    def get_identity_tokens_xml(self, identity_id: str) -> List[Dict]:
        """Fetch tokens via XML endpoint as fallback."""
        resp = self._request('GET', f'/identities/{identity_id}/tokens.xml')
        if resp.status_code != 200:
            return []
        try:
            return self._parse_tokens_xml(resp.text, identity_id)
        except Exception as e:
            logger.error(f"XML token parse failed for {identity_id}: {e}")
        return []

    def get_identities_xml(self) -> List[Dict]:
        """Fetch identities via XML endpoint.

        The controller only populates @identities when a search runs, so we
        mirror the full param set the Avigilon UI sends for "no filter"
        (empty lnam/fnam, empty adv_search_val_0). If any adv_search_* field
        is omitted the controller 500s.
        """
        resp = self._request(
            'GET', '/identities.xml',
            params={
                'identity_search_exec_search': 'true',
                'adv_search_field_0': '',
                'adv_search_udf_0': '',
                'adv_search_val_0': '',
                'search_pattern_0': '2',
                'adv_search_and_or': '&',
                'adv_search_cnt': '0',
                'adv_search_exec_search': 'true',
                'quick_search': 'true',
                'qck_search_and_or': '&',
                'lnam': '',
                'fnam': '',
                'tkn': '',
                'search_pattern_fnam': '2',
                'search_pattern_lnam': '2',
                'group_id': '',
                'id': '',
            },
            headers={
                'X-CSRF-Token': self.csrf_token,
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': '*/*',
                'Referer': f'{self.base_url}/identities',
            },
        )
        logger.info(
            f"get_identities_xml: status={resp.status_code} "
            f"content_length={len(resp.content)} elapsed={resp.elapsed.total_seconds():.2f}s"
        )
        if resp.status_code != 200:
            logger.error(f"get_identities_xml: HTTP {resp.status_code}")
            return []
        body_preview = (resp.text or '')[:300].replace('\n', ' ')
        logger.debug(f"get_identities_xml body (first 300 chars): {body_preview!r}")
        try:
            parsed = self._parse_identities_xml(resp.text)
            logger.info(f"get_identities_xml: parsed {len(parsed)} identities")
            return parsed
        except Exception as e:
            logger.error(f"XML identity parse failed: {e}")
        return []

    def create_identity(self, data: Dict) -> Optional[str]:
        p = self._prefix
        form = {
            'utf8': '\u2713',
            'authenticity_token': self.csrf_token,
            f'identity[{p}Lname]': data.get('last_name', ''),
            f'identity[{p}Fname]': data.get('first_name', ''),
            f'identity[{p}identityEmailaddress]': data.get('email', ''),
            f'identity[{p}identityPhone]': data.get('phone', ''),
            f'identity[{p}identityWorkphone]': data.get('work_phone', ''),
            f'identity[{p}identityTitle]': data.get('title', ''),
            f'identity[{p}identityDepartment]': data.get('department', ''),
            f'identity[{p}Idstatus]': '1',
            f'identity[{p}identityPagetimeout]': '600000',
            f'identity[{p}identityForcedPasswordChange]': 'TRUE',
        }
        resp = self._request(
            'POST', '/identities', data=form, allow_redirects=False,
            headers={'Referer': f'{self.base_url}/identities/new'},
        )
        location = resp.headers.get('Location', '')
        if resp.status_code == 302 and location:
            m = re.search(r'/identities/([a-f0-9]+)', location)
            if m:
                return m.group(1)
        logger.error(f"create_identity failed: HTTP {resp.status_code}")
        return None

    def create_token(self, identity_id: str, token_data: Dict) -> Optional[str]:
        p = self._prefix
        form = {
            'utf8': '\u2713',
            'authenticity_token': self.csrf_token,
            f'token[{p}Internalnumber]': token_data.get('internal_number', ''),
            f'token[{p}Embossednumber]': token_data.get('embossed_number', ''),
            f'token[{p}PIN]': token_data.get('pin', ''),
            f'token[{p}TokenType]': token_data.get('token_type', AVIGILON_TOKEN_TYPE_STANDARD),
            f'token[{p}Tokenlevel]': token_data.get('level', '0'),
            f'token[{p}Tokenstatus]': token_data.get('status', AVIGILON_TOKEN_STATUS_ACTIVE),
            f'token[{p}Download]': 'TRUE',
            f'token[{p}TokenMobileAppType]': '0',
            f'token[{p}TokenOrigoMobileIdType]': '0',
            f'token[{p}TokenUnitofUpdatePeriod]': '0',
            f'token[{p}Tokennoexpire]': 'FALSE',
            f'{p}Issuedate': token_data.get('issue_date', ''),
            f'{p}Activatedate': token_data.get('activate_date', ''),
            f'{p}Deactivatedate': token_data.get('deactivate_date', ''),
            'enrollVirdiAfter': 'false',
        }
        resp = self._request(
            'POST', f'/identities/{identity_id}/tokens',
            data=form, allow_redirects=False,
            headers={'Referer': f'{self.base_url}/identities/{identity_id}/tokens/new'},
        )
        location = resp.headers.get('Location', '')
        if resp.status_code == 302 and location:
            m = re.search(rf'/identities/{identity_id}/tokens/([a-f0-9]+)', location)
            if m:
                return m.group(1)
        logger.error(f"create_token for {identity_id} failed: HTTP {resp.status_code}")
        return None

    def update_token_status(
        self, identity_id: str, token_id: str,
        avigilon_status: str, current_token_data: Optional[Dict] = None,
    ) -> bool:
        p = self._prefix
        td = current_token_data or {}
        form = {
            'utf8': '\u2713',
            '_method': 'put',
            'authenticity_token': self.csrf_token,
            f'token[{p}Tokenstatus]': avigilon_status,
            f'token[{p}Internalnumber]': td.get('internal_number', ''),
            f'token[{p}Embossednumber]': td.get('embossed_number', ''),
            f'token[{p}PIN]': td.get('pin', ''),
            f'token[{p}TokenType]': td.get('token_type', '0'),
            f'token[{p}Tokenlevel]': td.get('level', '0'),
            f'token[{p}TokenMobileAppType]': '0',
            f'token[{p}TokenOrigoMobileIdType]': '0',
            f'token[{p}Download]': 'TRUE',
            f'token[{p}TokenUnitofUpdatePeriod]': '0',
            f'token[{p}Tokennoexpire]': 'FALSE',
            f'{p}Issuedate': td.get('issue_date', ''),
            f'{p}Activatedate': td.get('activate_date', ''),
            f'{p}Deactivatedate': td.get('deactivate_date', ''),
            'enrollVirdiAfter': 'false',
        }
        resp = self._request(
            'POST', f'/identities/{identity_id}/tokens/{token_id}',
            data=form, allow_redirects=False,
            headers={'Referer': f'{self.base_url}/identities/{identity_id}/tokens/{token_id}/edit'},
        )
        return resp.status_code == 302

    def delete_token(self, identity_id: str, token_id: str) -> bool:
        form = {
            '_method': 'delete',
            'authenticity_token': self.csrf_token,
        }
        resp = self._request(
            'POST', f'/identities/{identity_id}/tokens/{token_id}',
            data=form, allow_redirects=False,
            headers={'Referer': f'{self.base_url}/identities/{identity_id}/tokens'},
        )
        return resp.status_code == 302

    def get_card_formats(self) -> List[Dict]:
        resp = self._request(
            'GET', '/card_formats.json',
            headers={'Accept': 'application/json'},
        )
        if resp.status_code != 200:
            return []
        try:
            body = resp.json()
            items = body.get('data', body) if isinstance(body, dict) else body
            if isinstance(items, list):
                return [self._normalize_card_format(f) for f in items]
        except Exception as e:
            logger.error(f"get_card_formats parse failed: {e}")
        return []

    def test_connection(self) -> bool:
        logger.info(f"test_connection: starting (base_url={self.base_url})")
        try:
            if not self._logged_in:
                logger.info("test_connection: not logged in yet, attempting login()")
                if not self.login():
                    logger.error("test_connection: login() returned False — aborting")
                    return False
            else:
                logger.info("test_connection: session already authenticated, skipping login()")

            probe_path = '/identities.xml'
            logger.info(f"test_connection: probing GET {self.base_url}{probe_path}")
            resp = self._request('GET', probe_path)
            logger.info(
                f"test_connection: probe response status={resp.status_code} "
                f"final_url={resp.url} elapsed={resp.elapsed.total_seconds():.2f}s"
            )
            logger.debug(f"test_connection: probe response headers: {dict(resp.headers)}")
            body_preview = (resp.text or '')[:500].replace('\n', ' ')
            logger.debug(f"test_connection: probe body (first 500 chars): {body_preview!r}")

            if resp.status_code == 200:
                logger.info("test_connection: SUCCESS (probe returned 200)")
                return True
            logger.error(
                f"test_connection: FAILED — probe returned HTTP {resp.status_code} "
                f"(expected 200). Final URL: {resp.url}"
            )
            return False
        except Exception as e:
            logger.exception(f"test_connection: EXCEPTION {type(e).__name__}: {e}")
            return False

    # ------------------------------------------------------------------
    # XML parsing
    # ------------------------------------------------------------------

    # Avigilon Unity was originally branded "plasec"; some deployments still
    # return element names with the plasec prefix. Accept either.
    _XML_PREFIXES = ('avigilon', 'plasec')

    def _find_prefixed(self, elem, suffix: str):
        for prefix in self._XML_PREFIXES:
            found = elem.find(f'{prefix}{suffix}')
            if found is not None:
                if self._prefix != prefix:
                    logger.info(f"Detected Avigilon field prefix from XML: {prefix!r}")
                    self._prefix = prefix
                return found
        return None

    def _prefixed_text(self, elem, suffix: str) -> str:
        found = self._find_prefixed(elem, suffix)
        return found.text if found is not None and found.text else ''

    @classmethod
    def _prefixed_get(cls, d: Dict, suffix: str, default=''):
        """Look up dict keys with either avigilon<suffix> or plasec<suffix>."""
        for prefix in cls._XML_PREFIXES:
            val = d.get(f'{prefix}{suffix}')
            if val not in (None, ''):
                return val
        return default

    def _parse_identities_xml(self, xml_text: str) -> List[Dict]:
        """Parse XML identity list into normalized dicts."""
        root = ET.fromstring(xml_text)
        results = []
        for identity_elem in root.findall('identity'):
            cn_elem = identity_elem.find('.//cn')
            cn = cn_elem.text if cn_elem is not None else ''

            first_name = self._prefixed_text(identity_elem, 'Fname')
            last_name = self._prefixed_text(identity_elem, 'Lname')
            avigilon_name = self._prefixed_text(identity_elem, 'Name')

            if not first_name and not last_name and avigilon_name:
                parts = [p.strip() for p in avigilon_name.split(',')]
                last_name = parts[0] if parts else ''
                first_name = parts[1] if len(parts) > 1 else ''

            full_name = f"{first_name} {last_name}".strip() or avigilon_name
            raw_status = self._prefixed_text(identity_elem, 'Idstatus') or '1'

            results.append({
                'id': cn,
                'first_name': first_name or '',
                'last_name': last_name or '',
                'full_name': full_name,
                'email': self._prefixed_text(identity_elem, 'identityEmailaddress'),
                'phone': self._prefixed_text(identity_elem, 'identityPhone'),
                'work_phone': self._prefixed_text(identity_elem, 'identityWorkphone'),
                'status': self._normalize_identity_status(raw_status),
                'title': self._prefixed_text(identity_elem, 'identityTitle'),
                'department': self._prefixed_text(identity_elem, 'identityDepartment'),
            })
        return results

    def _parse_tokens_xml(self, xml_text: str, identity_id: str) -> List[Dict]:
        """Parse XML token list into normalized dicts."""
        root = ET.fromstring(xml_text)
        results = []
        for token_elem in root.findall('token'):
            cn_elem = token_elem.find('.//cn')
            cn = cn_elem.text if cn_elem is not None else ''

            def _text(suffix: str) -> str:
                return self._prefixed_text(token_elem, suffix)

            results.append({
                'id': cn,
                'identity_id': identity_id,
                'internal_number': _text('Internalnumber'),
                'embossed_number': _text('Embossednumber'),
                'pin': _text('PIN'),
                'status': _text('Tokenstatus') or '1',
                'token_type': _text('TokenType') or '0',
                'level': _text('Tokenlevel') or '0',
                'issue_date': _text('Issuedate'),
                'activate_date': _text('Activatedate'),
                'deactivate_date': _text('Deactivatedate'),
            })
        return results

    # ------------------------------------------------------------------
    # Response normalization
    # ------------------------------------------------------------------

    def _normalize_card_format(self, raw: Dict) -> Dict:
        if 'attributes' in raw:
            attrs = raw.get('attributes', {})
            fmt_id = raw.get('id', '') or attrs.get('cn', '')
        else:
            attrs = raw
            fmt_id = raw.get('cn', '') or raw.get('id', '')
        return {
            'id': str(fmt_id),
            'name': str(self._prefixed_get(attrs, 'Name') or ''),
            'facility_code': str(self._prefixed_get(attrs, 'cfmtFacilitycode') or ''),
            'total_bits': str(self._prefixed_get(attrs, 'cfmtMaxdigits') or ''),
            'fc_bits': str(self._prefixed_get(attrs, 'cfmtFcodelen') or ''),
            'cn_bits': str(self._prefixed_get(attrs, 'cfmtCardlen') or ''),
            'format_type': str(self._prefixed_get(attrs, 'cfmtType') or ''),
        }

    def _normalize_identity(self, raw: Dict) -> Dict:
        if 'attributes' in raw:
            attrs = raw.get('attributes', {})
            identity_id = raw.get('id', '') or attrs.get('cn', '')
        else:
            attrs = raw
            identity_id = raw.get('cn', '') or raw.get('id', '')

        first_name = str(self._prefixed_get(attrs, 'Fname') or '')
        last_name = str(self._prefixed_get(attrs, 'Lname') or '')
        avigilon_name = str(self._prefixed_get(attrs, 'Name') or '')

        if not first_name and not last_name and avigilon_name:
            parts = [p.strip() for p in avigilon_name.split(',')]
            last_name = parts[0] if parts else ''
            first_name = parts[1] if len(parts) > 1 else ''

        full_name = f"{first_name} {last_name}".strip() or avigilon_name
        raw_status = str(
            self._prefixed_get(attrs, 'Idstatus')
            or attrs.get('status', '')
            or ''
        )

        return {
            'id': identity_id,
            'first_name': first_name,
            'last_name': last_name,
            'full_name': full_name,
            'email': str(self._prefixed_get(attrs, 'identityEmailaddress') or ''),
            'phone': str(self._prefixed_get(attrs, 'identityPhone') or ''),
            'work_phone': str(self._prefixed_get(attrs, 'identityWorkphone') or ''),
            'status': self._normalize_identity_status(raw_status),
            'title': str(self._prefixed_get(attrs, 'identityTitle') or ''),
            'department': str(self._prefixed_get(attrs, 'identityDepartment') or ''),
        }

    _TOKEN_STATUS_MAP = {
        'active': '1',
        'inactive': '2',
        'not yet active': '3',
        'expired': '4',
    }

    def _normalize_identity_status(self, raw: str) -> str:
        mapping = {'active': '1', 'inactive': '2', 'not yet active': '3', 'expired': '4'}
        lower = raw.lower()
        if lower in mapping:
            return mapping[lower]
        if raw in ('1', '2', '3', '4'):
            return raw
        return '1'

    def _normalize_token(self, raw: Dict, identity_id: str = '') -> Dict:
        if 'attributes' in raw:
            attrs = raw.get('attributes', {})
            token_id = raw.get('id', '') or attrs.get('cn', '')
        else:
            attrs = raw
            token_id = raw.get('cn', '') or raw.get('id', '')

        ext = attrs.get('extended_attributes', {}) if isinstance(attrs, dict) else {}

        if ext:
            raw_status = str(ext.get('token_status', '') or '').lower()
            status = self._TOKEN_STATUS_MAP.get(raw_status, '1')
            issue_date = ext.get('formatted_issue_date', '') or ''
            activate_date = ext.get('formatted_activate_date', '') or ''
            deactivate_date = ext.get('formatted_deactivate_date', '') or ''
        else:
            raw_status = str(self._prefixed_get(attrs, 'Tokenstatus') or '1')
            status = self._normalize_identity_status(raw_status)
            issue_date = str(self._prefixed_get(attrs, 'Issuedate') or '')
            activate_date = str(self._prefixed_get(attrs, 'Activatedate') or '')
            deactivate_date = str(self._prefixed_get(attrs, 'Deactivatedate') or '')

        return {
            'id': token_id,
            'identity_id': identity_id,
            'internal_number': str(self._prefixed_get(attrs, 'Internalnumber') or ''),
            'embossed_number': str(self._prefixed_get(attrs, 'Embossednumber') or ''),
            'pin': str(self._prefixed_get(attrs, 'PIN') or ''),
            'status': status,
            'token_type': str(
                attrs.get('TokenTypeId')
                or self._prefixed_get(attrs, 'TokenType', '0')
                or '0'
            ),
            'level': str(self._prefixed_get(attrs, 'Tokenlevel', '0') or '0'),
            'issue_date': issue_date,
            'activate_date': activate_date,
            'deactivate_date': deactivate_date,
        }
