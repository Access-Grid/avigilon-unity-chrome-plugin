"""
Tests for AvigilonClient — normalization, XML parsing, and API interaction.
"""

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from src.avigilon_client import AvigilonClient


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def client():
    """Create a client without hitting any network."""
    with patch.object(AvigilonClient, 'login', return_value=True):
        c = AvigilonClient('10.100.9.39', 'admin', 'password')
        c._logged_in = True
        return c


# ------------------------------------------------------------------
# Identity normalization
# ------------------------------------------------------------------

class TestNormalizeIdentity:

    def test_json_api_shape(self, client):
        raw = {
            'id': '414c536238ab4c6c',
            'type': 'Identity',
            'attributes': {
                'avigilonFname': 'John',
                'avigilonLname': 'Doe',
                'avigilonName': 'Doe, John',
                'avigilonIdstatus': 1,
                'avigilonidentityEmailaddress': 'john@example.com',
                'avigilonidentityPhone': '555-1234',
                'avigilonidentityTitle': 'Manager',
                'avigilonidentityDepartment': 'HR',
            },
        }
        result = client._normalize_identity(raw)
        assert result['id'] == '414c536238ab4c6c'
        assert result['first_name'] == 'John'
        assert result['last_name'] == 'Doe'
        assert result['full_name'] == 'John Doe'
        assert result['email'] == 'john@example.com'
        assert result['phone'] == '555-1234'
        assert result['status'] == '1'
        assert result['title'] == 'Manager'

    def test_legacy_flat_shape(self, client):
        raw = {
            'cn': 'abc123',
            'avigilonFname': 'Jane',
            'avigilonLname': 'Smith',
            'avigilonIdstatus': '2',
        }
        result = client._normalize_identity(raw)
        assert result['id'] == 'abc123'
        assert result['first_name'] == 'Jane'
        assert result['last_name'] == 'Smith'
        assert result['status'] == '2'

    def test_name_from_avigilon_name(self, client):
        raw = {
            'cn': 'xyz',
            'avigilonName': 'BRESCIA MOREYRA, FORTUNATO',
            'avigilonIdstatus': '1',
        }
        result = client._normalize_identity(raw)
        assert result['first_name'] == 'FORTUNATO'
        assert result['last_name'] == 'BRESCIA MOREYRA'
        assert result['full_name'] == 'FORTUNATO BRESCIA MOREYRA'

    def test_status_string_normalization(self, client):
        assert client._normalize_identity_status('Active') == '1'
        assert client._normalize_identity_status('Inactive') == '2'
        assert client._normalize_identity_status('Not yet active') == '3'
        assert client._normalize_identity_status('Expired') == '4'
        assert client._normalize_identity_status('1') == '1'
        assert client._normalize_identity_status('unknown') == '1'


# ------------------------------------------------------------------
# Token normalization
# ------------------------------------------------------------------

class TestNormalizeToken:

    def test_json_api_with_extended_attributes(self, client):
        raw = {
            'id': '7480262f02d94970',
            'type': 'Token',
            'attributes': {
                'cn': '7480262f02d94970',
                'avigilonInternalnumber': '42069',
                'avigilonEmbossednumber': 'AccessGrid',
                'extended_attributes': {
                    'token_status': 'Active',
                    'formatted_issue_date': '2026-02-27',
                    'formatted_activate_date': '2026-02-27',
                    'formatted_deactivate_date': '2027-02-27',
                },
            },
        }
        result = client._normalize_token(raw, 'identity123')
        assert result['id'] == '7480262f02d94970'
        assert result['identity_id'] == 'identity123'
        assert result['internal_number'] == '42069'
        assert result['embossed_number'] == 'AccessGrid'
        assert result['status'] == '1'

    def test_json_api_without_extended(self, client):
        raw = {
            'id': 'tok1',
            'attributes': {
                'avigilonTokenstatus': 2,
                'avigilonInternalnumber': '100',
                'avigilonEmbossednumber': '200',
            },
        }
        result = client._normalize_token(raw, 'id1')
        assert result['status'] == '2'
        assert result['internal_number'] == '100'

    def test_legacy_flat_token(self, client):
        raw = {
            'cn': 'tok2',
            'avigilonInternalnumber': '300',
            'avigilonEmbossednumber': 'AccessGrid',
            'avigilonTokenstatus': '1',
        }
        result = client._normalize_token(raw)
        assert result['id'] == 'tok2'
        assert result['status'] == '1'
        assert result['embossed_number'] == 'AccessGrid'


# ------------------------------------------------------------------
# XML parsing
# ------------------------------------------------------------------

class TestXMLParsing:

    def test_parse_identities_xml(self, client):
        xml = """<identities type="array">
            <identity>
                <cns type="array"><cn>abc123</cn></cns>
                <avigilonFname>JOHN</avigilonFname>
                <avigilonLname>DOE</avigilonLname>
                <avigilonIdstatus>1</avigilonIdstatus>
                <avigilonName>DOE, JOHN</avigilonName>
            </identity>
            <identity>
                <cns type="array"><cn>def456</cn></cns>
                <avigilonName>SMITH, JANE</avigilonName>
                <avigilonIdstatus>2</avigilonIdstatus>
            </identity>
        </identities>"""

        results = client._parse_identities_xml(xml)
        assert len(results) == 2

        assert results[0]['id'] == 'abc123'
        assert results[0]['first_name'] == 'JOHN'
        assert results[0]['last_name'] == 'DOE'
        assert results[0]['status'] == '1'

        assert results[1]['id'] == 'def456'
        assert results[1]['first_name'] == 'JANE'
        assert results[1]['last_name'] == 'SMITH'
        assert results[1]['status'] == '2'

    def test_parse_tokens_xml(self, client):
        xml = """<tokens type="array">
            <token>
                <cns type="array"><cn>tok1</cn></cns>
                <avigilonInternalnumber>54321</avigilonInternalnumber>
                <avigilonEmbossednumber>AccessGrid</avigilonEmbossednumber>
                <avigilonTokenstatus>1</avigilonTokenstatus>
                <avigilonActivatedate>20260227212244Z</avigilonActivatedate>
                <avigilonDeactivatedate>20270327211824Z</avigilonDeactivatedate>
            </token>
        </tokens>"""

        results = client._parse_tokens_xml(xml, 'identity1')
        assert len(results) == 1
        assert results[0]['id'] == 'tok1'
        assert results[0]['identity_id'] == 'identity1'
        assert results[0]['internal_number'] == '54321'
        assert results[0]['embossed_number'] == 'AccessGrid'
        assert results[0]['status'] == '1'

    def test_parse_empty_xml(self, client):
        xml = '<identities type="array"></identities>'
        assert client._parse_identities_xml(xml) == []


# ------------------------------------------------------------------
# Card format normalization
# ------------------------------------------------------------------

class TestCardFormat:

    def test_json_api_shape(self, client):
        raw = {
            'id': 'fmt1',
            'attributes': {
                'avigilonName': '26-bit Wiegand',
                'avigiloncfmtFacilitycode': '100',
                'avigiloncfmtMaxdigits': '26',
                'avigiloncfmtFcodelen': '8',
                'avigiloncfmtCardlen': '16',
                'avigiloncfmtType': 'wiegand',
            },
        }
        result = client._normalize_card_format(raw)
        assert result['id'] == 'fmt1'
        assert result['name'] == '26-bit Wiegand'
        assert result['facility_code'] == '100'

    def test_flat_shape(self, client):
        raw = {
            'cn': 'fmt2',
            'avigilonName': 'Custom',
            'avigiloncfmtFacilitycode': '200',
        }
        result = client._normalize_card_format(raw)
        assert result['id'] == 'fmt2'
        assert result['facility_code'] == '200'
