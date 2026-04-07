"""
HTTP API client for Plasec / Avigilon Unity access control system.

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
    PLASEC_TOKEN_STATUS_ACTIVE,
    PLASEC_TOKEN_TYPE_STANDARD,
)

logger = logging.getLogger(__name__)


class PlaSecAuthError(Exception):
    pass


class PlaSecAPIError(Exception):
    pass


class PlaSecClient:
    """
    Session-based HTTP client for Plasec / Avigilon Unity.
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

    @property
    def csrf_token(self) -> str:
        return self.session.cookies.get('XSRF-TOKEN', '')

    def login(self) -> bool:
        try:
            resp = self.session.post(
                f"{self.base_url}/sessions",
                data={'login': self.username, 'password': self.password},
                allow_redirects=True,
                verify=self.verify_ssl,
                timeout=HTTP_TIMEOUT,
            )
            if self.session.cookies.get('_session_id'):
                self._logged_in = True
                logger.info("Logged in to Plasec")
                return True
            if resp.status_code == 404:
                logger.error(f"Plasec login: 404 — verify {self.base_url} is correct")
            else:
                logger.error(f"Plasec login: no session cookie (status {resp.status_code})")
        except requests.RequestException as e:
            logger.error(f"Plasec login failed: {e}")
        return False

    def _ensure_authenticated(self):
        if not self._logged_in:
            if not self.login():
                raise PlaSecAuthError("Cannot authenticate with Plasec server")

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
            logger.warning("Plasec session expired — re-authenticating")
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
                params={'page': page, 'perpage': per_page, 'sort_by': 'plasecName', 'order': 'ascend'},
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
            'identity[plasecLname]': data.get('last_name', ''),
            'identity[plasecFname]': data.get('first_name', ''),
            'identity[plasecidentityEmailaddress]': data.get('email', ''),
            'identity[plasecidentityPhone]': data.get('phone', ''),
            'identity[plasecidentityWorkphone]': data.get('work_phone', ''),
            'identity[plasecidentityTitle]': data.get('title', ''),
            'identity[plasecidentityDepartment]': data.get('department', ''),
            'identity[plasecIdstatus]': '1',
            'identity[plasecidentityPagetimeout]': '600000',
            'identity[plasecidentityForcedPasswordChange]': 'TRUE',
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
            'token[plasecInternalnumber]': token_data.get('internal_number', ''),
            'token[plasecEmbossednumber]': token_data.get('embossed_number', ''),
            'token[plasecPIN]': token_data.get('pin', ''),
            'token[plasecTokenType]': token_data.get('token_type', PLASEC_TOKEN_TYPE_STANDARD),
            'token[plasecTokenlevel]': token_data.get('level', '0'),
            'token[plasecTokenstatus]': token_data.get('status', PLASEC_TOKEN_STATUS_ACTIVE),
            'token[plasecDownload]': 'TRUE',
            'token[plasecTokenMobileAppType]': '0',
            'token[plasecTokenOrigoMobileIdType]': '0',
            'token[plasecTokenUnitofUpdatePeriod]': '0',
            'token[plasecTokennoexpire]': 'FALSE',
            'plasecIssuedate': token_data.get('issue_date', ''),
            'plasecActivatedate': token_data.get('activate_date', ''),
            'plasecDeactivatedate': token_data.get('deactivate_date', ''),
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
        plasec_status: str, current_token_data: Optional[Dict] = None,
    ) -> bool:
        td = current_token_data or {}
        form = {
            'utf8': '\u2713',
            '_method': 'put',
            'authenticity_token': self.csrf_token,
            'token[plasecTokenstatus]': plasec_status,
            'token[plasecInternalnumber]': td.get('internal_number', ''),
            'token[plasecEmbossednumber]': td.get('embossed_number', ''),
            'token[plasecPIN]': td.get('pin', ''),
            'token[plasecTokenType]': td.get('token_type', '0'),
            'token[plasecTokenlevel]': td.get('level', '0'),
            'token[plasecTokenMobileAppType]': '0',
            'token[plasecTokenOrigoMobileIdType]': '0',
            'token[plasecDownload]': 'TRUE',
            'token[plasecTokenUnitofUpdatePeriod]': '0',
            'token[plasecTokennoexpire]': 'FALSE',
            'plasecIssuedate': td.get('issue_date', ''),
            'plasecActivatedate': td.get('activate_date', ''),
            'plasecDeactivatedate': td.get('deactivate_date', ''),
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
        try:
            if not self._logged_in and not self.login():
                return False
            resp = self._request(
                'GET', '/identities.json',
                params={'page': 1, 'perpage': 1},
                headers={'Accept': 'application/json'},
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Connection test failed: {e}")
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
            fname_elem = identity_elem.find('plasecFname')
            lname_elem = identity_elem.find('plasecLname')
            name_elem = identity_elem.find('plasecName')
            status_elem = identity_elem.find('plasecIdstatus')

            first_name = fname_elem.text if fname_elem is not None else ''
            last_name = lname_elem.text if lname_elem is not None else ''
            plasec_name = name_elem.text if name_elem is not None else ''

            if not first_name and not last_name and plasec_name:
                parts = [p.strip() for p in plasec_name.split(',')]
                last_name = parts[0] if parts else ''
                first_name = parts[1] if len(parts) > 1 else ''

            full_name = f"{first_name} {last_name}".strip() or plasec_name
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
                'internal_number': _text('plasecInternalnumber'),
                'embossed_number': _text('plasecEmbossednumber'),
                'pin': _text('plasecPIN'),
                'status': _text('plasecTokenstatus') or '1',
                'token_type': _text('plasecTokenType') or '0',
                'level': _text('plasecTokenlevel') or '0',
                'issue_date': _text('plasecIssuedate'),
                'activate_date': _text('plasecActivatedate'),
                'deactivate_date': _text('plasecDeactivatedate'),
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
            'name': str(attrs.get('plasecName', '') or ''),
            'facility_code': str(attrs.get('plaseccfmtFacilitycode', '') or ''),
            'total_bits': str(attrs.get('plaseccfmtMaxdigits', '') or ''),
            'fc_bits': str(attrs.get('plaseccfmtFcodelen', '') or ''),
            'cn_bits': str(attrs.get('plaseccfmtCardlen', '') or ''),
            'format_type': str(attrs.get('plaseccfmtType', '') or ''),
        }

    def _normalize_identity(self, raw: Dict) -> Dict:
        if 'attributes' in raw:
            attrs = raw.get('attributes', {})
            identity_id = raw.get('id', '') or attrs.get('cn', '')
            first_name = attrs.get('plasecFname', '') or ''
            last_name = attrs.get('plasecLname', '') or ''
            plasec_name = attrs.get('plasecName', '') or ''

            if not first_name and not last_name and plasec_name:
                parts = [p.strip() for p in plasec_name.split(',')]
                last_name = parts[0] if parts else ''
                first_name = parts[1] if len(parts) > 1 else ''

            full_name = f"{first_name} {last_name}".strip() or plasec_name
            raw_status = str(attrs.get('plasecIdstatus', '') or '')

            return {
                'id': identity_id,
                'first_name': first_name,
                'last_name': last_name,
                'full_name': full_name,
                'email': attrs.get('plasecidentityEmailaddress', '') or '',
                'phone': attrs.get('plasecidentityPhone', '') or '',
                'work_phone': attrs.get('plasecidentityWorkphone', '') or '',
                'status': self._normalize_identity_status(raw_status),
                'title': attrs.get('plasecidentityTitle', '') or '',
                'department': attrs.get('plasecidentityDepartment', '') or '',
            }

        identity_id = raw.get('cn', '') or raw.get('id', '')
        first_name = raw.get('plasecFname', '') or ''
        last_name = raw.get('plasecLname', '') or ''
        plasec_name = raw.get('plasecName', '') or ''

        if not first_name and not last_name and plasec_name:
            parts = [p.strip() for p in plasec_name.split(',')]
            last_name = parts[0] if parts else ''
            first_name = parts[1] if len(parts) > 1 else ''

        full_name = f"{first_name} {last_name}".strip() or plasec_name
        raw_status = str(raw.get('plasecIdstatus', '') or raw.get('status', ''))

        return {
            'id': identity_id,
            'first_name': first_name,
            'last_name': last_name,
            'full_name': full_name,
            'email': raw.get('plasecidentityEmailaddress', '') or '',
            'phone': raw.get('plasecidentityPhone', '') or '',
            'work_phone': raw.get('plasecidentityWorkphone', '') or '',
            'status': self._normalize_identity_status(raw_status),
            'title': raw.get('plasecidentityTitle', '') or '',
            'department': raw.get('plasecidentityDepartment', '') or '',
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
                raw_status = str(attrs.get('plasecTokenstatus', '1') or '1')
                status = self._normalize_identity_status(raw_status)
                issue_date = attrs.get('plasecIssuedate', '') or ''
                activate_date = attrs.get('plasecActivatedate', '') or ''
                deactivate_date = attrs.get('plasecDeactivatedate', '') or ''

            return {
                'id': raw.get('id', '') or attrs.get('cn', ''),
                'identity_id': identity_id,
                'internal_number': str(attrs.get('plasecInternalnumber', '') or ''),
                'embossed_number': str(attrs.get('plasecEmbossednumber', '') or ''),
                'pin': str(attrs.get('plasecPIN', '') or ''),
                'status': status,
                'token_type': str(attrs.get('TokenTypeId') or attrs.get('plasecTokenType', '0') or '0'),
                'level': str(attrs.get('plasecTokenlevel', '0') or '0'),
                'issue_date': issue_date,
                'activate_date': activate_date,
                'deactivate_date': deactivate_date,
            }

        return {
            'id': raw.get('cn', '') or raw.get('id', ''),
            'identity_id': identity_id,
            'internal_number': str(raw.get('plasecInternalnumber', '') or ''),
            'embossed_number': str(raw.get('plasecEmbossednumber', '') or ''),
            'pin': str(raw.get('plasecPIN', '') or ''),
            'status': str(raw.get('plasecTokenstatus', '1') or '1'),
            'token_type': str(raw.get('plasecTokenType', '0') or '0'),
            'level': str(raw.get('plasecTokenlevel', '0') or '0'),
            'issue_date': raw.get('plasecIssuedate', '') or '',
            'activate_date': raw.get('plasecActivatedate', '') or '',
            'deactivate_date': raw.get('plasecDeactivatedate', '') or '',
        }
