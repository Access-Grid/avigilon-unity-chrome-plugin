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

        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        logger.info(
            f"AvigilonClient init: base_url={self.base_url} "
            f"username={self.username!r} verify_ssl={self.verify_ssl} "
            f"timeout={HTTP_TIMEOUT}s user_agent={HTTP_USER_AGENT!r}"
        )

    @property
    def csrf_token(self) -> str:
        return self.session.cookies.get('XSRF-TOKEN', '')

    def login(self) -> bool:
        login_url = f"{self.base_url}/sessions"
        logger.info(f"Avigilon login: POST {login_url} (user={self.username!r}, verify_ssl={self.verify_ssl})")
        try:
            resp = self.session.post(
                login_url,
                data={'login': self.username, 'password': self.password},
                allow_redirects=True,
                verify=self.verify_ssl,
                timeout=HTTP_TIMEOUT,
            )
            logger.info(
                f"Avigilon login response: status={resp.status_code} "
                f"final_url={resp.url} elapsed={resp.elapsed.total_seconds():.2f}s "
                f"redirects={len(resp.history)}"
            )
            if resp.history:
                for i, hop in enumerate(resp.history):
                    logger.debug(
                        f"  redirect [{i}]: {hop.status_code} {hop.url} -> "
                        f"Location: {hop.headers.get('Location', '')}"
                    )
            logger.debug(f"Avigilon login response headers: {dict(resp.headers)}")
            cookie_names = [c.name for c in self.session.cookies]
            logger.info(f"Avigilon login cookies set: {cookie_names}")
            body_preview = (resp.text or '')[:500].replace('\n', ' ')
            logger.debug(f"Avigilon login body (first 500 chars): {body_preview!r}")

            if self.session.cookies.get('_session_id'):
                self._logged_in = True
                logger.info(
                    f"Avigilon login SUCCESS: _session_id cookie present, "
                    f"XSRF-TOKEN={'set' if self.csrf_token else 'missing'}"
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
        page = 1
        per_page = 100
        result: Dict[str, Dict] = {}

        while True:
            resp = self._request(
                'GET', '/identities.json',
                params={'page': page, 'perpage': per_page, 'sort_by': 'avigilonName', 'order': 'ascend'},
                headers={'Accept': 'application/json'},
            )
            if resp.status_code != 200:
                logger.error(f"get_all_identities page {page}: HTTP {resp.status_code}")
                break
            try:
                body = resp.json()
            except Exception as e:
                logger.error(f"Identity list JSON parse failed: {e}")
                break

            items = body.get('data', [])
            for raw in items:
                ident = self._normalize_identity(raw)
                if ident.get('id'):
                    result[ident['id']] = ident

            meta = body.get('meta', {})
            total = meta.get('recordsFiltered', 0)
            if page * per_page >= total or not items:
                break
            page += 1

        logger.debug(f"Found {len(result)} identities")
        return list(result.values())

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
        """Fetch identities via XML search endpoint."""
        resp = self._request(
            'GET', '/identities.xml',
            params={
                'identity_search_exec_search': 'true',
                'adv_search_exec_search': 'true',
                'quick_search': 'true',
                'qck_search_and_or': '&',
            },
            headers={'X-CSRF-Token': self.csrf_token, 'X-Requested-With': 'XMLHttpRequest'},
        )
        if resp.status_code != 200:
            return []
        try:
            return self._parse_identities_xml(resp.text)
        except Exception as e:
            logger.error(f"XML identity parse failed: {e}")
        return []

    def create_identity(self, data: Dict) -> Optional[str]:
        form = {
            'utf8': '\u2713',
            'authenticity_token': self.csrf_token,
            'identity[avigilonLname]': data.get('last_name', ''),
            'identity[avigilonFname]': data.get('first_name', ''),
            'identity[avigilonidentityEmailaddress]': data.get('email', ''),
            'identity[avigilonidentityPhone]': data.get('phone', ''),
            'identity[avigilonidentityWorkphone]': data.get('work_phone', ''),
            'identity[avigilonidentityTitle]': data.get('title', ''),
            'identity[avigilonidentityDepartment]': data.get('department', ''),
            'identity[avigilonIdstatus]': '1',
            'identity[avigilonidentityPagetimeout]': '600000',
            'identity[avigilonidentityForcedPasswordChange]': 'TRUE',
        }
        resp = self._request('POST', '/identities', data=form, allow_redirects=False)
        location = resp.headers.get('Location', '')
        if resp.status_code == 302 and location:
            m = re.search(r'/identities/([a-f0-9]+)', location)
            if m:
                return m.group(1)
        logger.error(f"create_identity failed: HTTP {resp.status_code}")
        return None

    def create_token(self, identity_id: str, token_data: Dict) -> Optional[str]:
        form = {
            'utf8': '\u2713',
            'authenticity_token': self.csrf_token,
            'token[avigilonInternalnumber]': token_data.get('internal_number', ''),
            'token[avigilonEmbossednumber]': token_data.get('embossed_number', ''),
            'token[avigilonPIN]': token_data.get('pin', ''),
            'token[avigilonTokenType]': token_data.get('token_type', AVIGILON_TOKEN_TYPE_STANDARD),
            'token[avigilonTokenlevel]': token_data.get('level', '0'),
            'token[avigilonTokenstatus]': token_data.get('status', AVIGILON_TOKEN_STATUS_ACTIVE),
            'token[avigilonDownload]': 'TRUE',
            'token[avigilonTokenMobileAppType]': '0',
            'token[avigilonTokenOrigoMobileIdType]': '0',
            'token[avigilonTokenUnitofUpdatePeriod]': '0',
            'token[avigilonTokennoexpire]': 'FALSE',
            'avigilonIssuedate': token_data.get('issue_date', ''),
            'avigilonActivatedate': token_data.get('activate_date', ''),
            'avigilonDeactivatedate': token_data.get('deactivate_date', ''),
            'enrollVirdiAfter': 'false',
        }
        resp = self._request(
            'POST', f'/identities/{identity_id}/tokens',
            data=form, allow_redirects=False,
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
        td = current_token_data or {}
        form = {
            'utf8': '\u2713',
            '_method': 'put',
            'authenticity_token': self.csrf_token,
            'token[avigilonTokenstatus]': avigilon_status,
            'token[avigilonInternalnumber]': td.get('internal_number', ''),
            'token[avigilonEmbossednumber]': td.get('embossed_number', ''),
            'token[avigilonPIN]': td.get('pin', ''),
            'token[avigilonTokenType]': td.get('token_type', '0'),
            'token[avigilonTokenlevel]': td.get('level', '0'),
            'token[avigilonTokenMobileAppType]': '0',
            'token[avigilonTokenOrigoMobileIdType]': '0',
            'token[avigilonDownload]': 'TRUE',
            'token[avigilonTokenUnitofUpdatePeriod]': '0',
            'token[avigilonTokennoexpire]': 'FALSE',
            'avigilonIssuedate': td.get('issue_date', ''),
            'avigilonActivatedate': td.get('activate_date', ''),
            'avigilonDeactivatedate': td.get('deactivate_date', ''),
            'enrollVirdiAfter': 'false',
        }
        resp = self._request(
            'POST', f'/identities/{identity_id}/tokens/{token_id}',
            data=form, allow_redirects=False,
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

            probe_path = '/identities.json'
            logger.info(f"test_connection: probing GET {self.base_url}{probe_path}?page=1&perpage=1")
            resp = self._request(
                'GET', probe_path,
                params={'page': 1, 'perpage': 1},
                headers={'Accept': 'application/json'},
            )
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

    def _parse_identities_xml(self, xml_text: str) -> List[Dict]:
        """Parse XML identity list into normalized dicts."""
        root = ET.fromstring(xml_text)
        results = []
        for identity_elem in root.findall('identity'):
            cn_elem = identity_elem.find('.//cn')
            cn = cn_elem.text if cn_elem is not None else ''
            fname_elem = identity_elem.find('avigilonFname')
            lname_elem = identity_elem.find('avigilonLname')
            name_elem = identity_elem.find('avigilonName')
            status_elem = identity_elem.find('avigilonIdstatus')

            first_name = fname_elem.text if fname_elem is not None else ''
            last_name = lname_elem.text if lname_elem is not None else ''
            avigilon_name = name_elem.text if name_elem is not None else ''

            if not first_name and not last_name and avigilon_name:
                parts = [p.strip() for p in avigilon_name.split(',')]
                last_name = parts[0] if parts else ''
                first_name = parts[1] if len(parts) > 1 else ''

            full_name = f"{first_name} {last_name}".strip() or avigilon_name
            raw_status = status_elem.text if status_elem is not None else '1'

            results.append({
                'id': cn,
                'first_name': first_name or '',
                'last_name': last_name or '',
                'full_name': full_name,
                'email': '',
                'phone': '',
                'work_phone': '',
                'status': self._normalize_identity_status(raw_status),
                'title': '',
                'department': '',
            })
        return results

    def _parse_tokens_xml(self, xml_text: str, identity_id: str) -> List[Dict]:
        """Parse XML token list into normalized dicts."""
        root = ET.fromstring(xml_text)
        results = []
        for token_elem in root.findall('token'):
            cn_elem = token_elem.find('.//cn')
            cn = cn_elem.text if cn_elem is not None else ''

            def _text(tag):
                el = token_elem.find(tag)
                return el.text if el is not None and el.text else ''

            results.append({
                'id': cn,
                'identity_id': identity_id,
                'internal_number': _text('avigilonInternalnumber'),
                'embossed_number': _text('avigilonEmbossednumber'),
                'pin': _text('avigilonPIN'),
                'status': _text('avigilonTokenstatus') or '1',
                'token_type': _text('avigilonTokenType') or '0',
                'level': _text('avigilonTokenlevel') or '0',
                'issue_date': _text('avigilonIssuedate'),
                'activate_date': _text('avigilonActivatedate'),
                'deactivate_date': _text('avigilonDeactivatedate'),
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
            'name': str(attrs.get('avigilonName', '') or ''),
            'facility_code': str(attrs.get('avigiloncfmtFacilitycode', '') or ''),
            'total_bits': str(attrs.get('avigiloncfmtMaxdigits', '') or ''),
            'fc_bits': str(attrs.get('avigiloncfmtFcodelen', '') or ''),
            'cn_bits': str(attrs.get('avigiloncfmtCardlen', '') or ''),
            'format_type': str(attrs.get('avigiloncfmtType', '') or ''),
        }

    def _normalize_identity(self, raw: Dict) -> Dict:
        if 'attributes' in raw:
            attrs = raw.get('attributes', {})
            identity_id = raw.get('id', '') or attrs.get('cn', '')
            first_name = attrs.get('avigilonFname', '') or ''
            last_name = attrs.get('avigilonLname', '') or ''
            avigilon_name = attrs.get('avigilonName', '') or ''

            if not first_name and not last_name and avigilon_name:
                parts = [p.strip() for p in avigilon_name.split(',')]
                last_name = parts[0] if parts else ''
                first_name = parts[1] if len(parts) > 1 else ''

            full_name = f"{first_name} {last_name}".strip() or avigilon_name
            raw_status = str(attrs.get('avigilonIdstatus', '') or '')

            return {
                'id': identity_id,
                'first_name': first_name,
                'last_name': last_name,
                'full_name': full_name,
                'email': attrs.get('avigilonidentityEmailaddress', '') or '',
                'phone': attrs.get('avigilonidentityPhone', '') or '',
                'work_phone': attrs.get('avigilonidentityWorkphone', '') or '',
                'status': self._normalize_identity_status(raw_status),
                'title': attrs.get('avigilonidentityTitle', '') or '',
                'department': attrs.get('avigilonidentityDepartment', '') or '',
            }

        identity_id = raw.get('cn', '') or raw.get('id', '')
        first_name = raw.get('avigilonFname', '') or ''
        last_name = raw.get('avigilonLname', '') or ''
        avigilon_name = raw.get('avigilonName', '') or ''

        if not first_name and not last_name and avigilon_name:
            parts = [p.strip() for p in avigilon_name.split(',')]
            last_name = parts[0] if parts else ''
            first_name = parts[1] if len(parts) > 1 else ''

        full_name = f"{first_name} {last_name}".strip() or avigilon_name
        raw_status = str(raw.get('avigilonIdstatus', '') or raw.get('status', ''))

        return {
            'id': identity_id,
            'first_name': first_name,
            'last_name': last_name,
            'full_name': full_name,
            'email': raw.get('avigilonidentityEmailaddress', '') or '',
            'phone': raw.get('avigilonidentityPhone', '') or '',
            'work_phone': raw.get('avigilonidentityWorkphone', '') or '',
            'status': self._normalize_identity_status(raw_status),
            'title': raw.get('avigilonidentityTitle', '') or '',
            'department': raw.get('avigilonidentityDepartment', '') or '',
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
            ext = attrs.get('extended_attributes', {})

            if ext:
                raw_status = str(ext.get('token_status', '') or '').lower()
                status = self._TOKEN_STATUS_MAP.get(raw_status, '1')
                issue_date = ext.get('formatted_issue_date', '') or ''
                activate_date = ext.get('formatted_activate_date', '') or ''
                deactivate_date = ext.get('formatted_deactivate_date', '') or ''
            else:
                raw_status = str(attrs.get('avigilonTokenstatus', '1') or '1')
                status = self._normalize_identity_status(raw_status)
                issue_date = attrs.get('avigilonIssuedate', '') or ''
                activate_date = attrs.get('avigilonActivatedate', '') or ''
                deactivate_date = attrs.get('avigilonDeactivatedate', '') or ''

            return {
                'id': raw.get('id', '') or attrs.get('cn', ''),
                'identity_id': identity_id,
                'internal_number': str(attrs.get('avigilonInternalnumber', '') or ''),
                'embossed_number': str(attrs.get('avigilonEmbossednumber', '') or ''),
                'pin': str(attrs.get('avigilonPIN', '') or ''),
                'status': status,
                'token_type': str(attrs.get('TokenTypeId') or attrs.get('avigilonTokenType', '0') or '0'),
                'level': str(attrs.get('avigilonTokenlevel', '0') or '0'),
                'issue_date': issue_date,
                'activate_date': activate_date,
                'deactivate_date': deactivate_date,
            }

        return {
            'id': raw.get('cn', '') or raw.get('id', ''),
            'identity_id': identity_id,
            'internal_number': str(raw.get('avigilonInternalnumber', '') or ''),
            'embossed_number': str(raw.get('avigilonEmbossednumber', '') or ''),
            'pin': str(raw.get('avigilonPIN', '') or ''),
            'status': str(raw.get('avigilonTokenstatus', '1') or '1'),
            'token_type': str(raw.get('avigilonTokenType', '0') or '0'),
            'level': str(raw.get('avigilonTokenlevel', '0') or '0'),
            'issue_date': raw.get('avigilonIssuedate', '') or '',
            'activate_date': raw.get('avigilonActivatedate', '') or '',
            'deactivate_date': raw.get('avigilonDeactivatedate', '') or '',
        }
