"""
Tests for the bridge HTTP server endpoints.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from src.server import app


@pytest.fixture
def test_client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


class TestHealthEndpoint:

    def test_health_returns_ok(self, test_client):
        resp = test_client.get('/api/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'
        assert data['service'] == 'avigilon-bridge'


class TestStatusEndpoint:

    @patch('src.server.load_config')
    def test_status_unconfigured(self, mock_config, test_client):
        mock_config.return_value = {}
        resp = test_client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['configured'] is False
        assert data['accessgrid_configured'] is False

    @patch('src.server.load_config')
    def test_status_configured(self, mock_config, test_client):
        mock_config.return_value = {
            'plasec': {'host': '10.0.0.1', 'username': 'admin', 'password': 'pass'},
            'accessgrid': {'account_id': 'acc123'},
        }
        resp = test_client.get('/api/status')
        data = resp.get_json()
        assert data['configured'] is True
        assert data['accessgrid_configured'] is True
        assert data['plasec_host'] == '10.0.0.1'


class TestConfigEndpoints:

    @patch('src.server.load_config')
    def test_get_config_masks_secrets(self, mock_config, test_client):
        mock_config.return_value = {
            'plasec': {'host': '10.0.0.1', 'username': 'admin', 'password': 'secret'},
            'accessgrid': {'account_id': 'acc1', 'api_secret': 'supersecret', 'template_id': 't1'},
        }
        resp = test_client.get('/api/config')
        data = resp.get_json()
        assert data['plasec']['host'] == '10.0.0.1'
        assert data['plasec']['has_password'] is True
        assert 'password' not in data['plasec']
        assert data['accessgrid']['has_secret'] is True
        assert 'api_secret' not in data['accessgrid']

    @patch('src.server.save_config')
    @patch('src.server.load_config', return_value={})
    @patch('src.server._reset_client')
    def test_set_config(self, mock_reset, mock_load, mock_save, test_client):
        resp = test_client.post('/api/config', json={
            'plasec': {'host': '10.0.0.2', 'username': 'admin', 'password': 'pw'},
            'accessgrid': {'account_id': 'a1', 'api_secret': 's1', 'template_id': 't1'},
        })
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'ok'
        mock_save.assert_called_once()
        mock_reset.assert_called_once()


class TestPlasecProxyEndpoints:

    @patch('src.server._get_client')
    def test_test_connection_success(self, mock_get, test_client):
        mock_client = MagicMock()
        mock_client.test_connection.return_value = True
        mock_get.return_value = mock_client

        resp = test_client.post('/api/plasec/test')
        assert resp.status_code == 200
        assert resp.get_json()['connected'] is True

    @patch('src.server._get_client')
    def test_test_connection_failure(self, mock_get, test_client):
        mock_client = MagicMock()
        mock_client.test_connection.return_value = False
        mock_get.return_value = mock_client

        resp = test_client.post('/api/plasec/test')
        assert resp.get_json()['connected'] is False

    @patch('src.server._get_client')
    def test_get_identities(self, mock_get, test_client):
        mock_client = MagicMock()
        mock_client.get_all_identities.return_value = [
            {'id': 'abc', 'full_name': 'John Doe', 'status': '1'},
            {'id': 'def', 'full_name': 'Jane Smith', 'status': '1'},
        ]
        mock_get.return_value = mock_client

        resp = test_client.get('/api/plasec/identities')
        data = resp.get_json()
        assert data['count'] == 2
        assert data['identities'][0]['full_name'] == 'John Doe'

    @patch('src.server._get_client')
    def test_get_identity_detail(self, mock_get, test_client):
        mock_client = MagicMock()
        mock_client.get_identity.return_value = {
            'id': 'abc', 'full_name': 'John Doe', 'email': 'john@example.com',
        }
        mock_get.return_value = mock_client

        resp = test_client.get('/api/plasec/identities/abc')
        data = resp.get_json()
        assert data['id'] == 'abc'
        assert data['email'] == 'john@example.com'

    @patch('src.server._get_client')
    def test_get_identity_not_found(self, mock_get, test_client):
        mock_client = MagicMock()
        mock_client.get_identity.return_value = None
        mock_get.return_value = mock_client

        resp = test_client.get('/api/plasec/identities/missing')
        assert resp.status_code == 404

    @patch('src.server._get_client')
    def test_get_tokens(self, mock_get, test_client):
        mock_client = MagicMock()
        mock_client.get_identity_tokens.return_value = [
            {'id': 'tok1', 'embossed_number': 'AccessGrid', 'status': '1'},
        ]
        mock_get.return_value = mock_client

        resp = test_client.get('/api/plasec/identities/abc/tokens')
        data = resp.get_json()
        assert data['count'] == 1
        assert data['tokens'][0]['embossed_number'] == 'AccessGrid'

    @patch('src.server._get_client')
    def test_update_token_status(self, mock_get, test_client):
        mock_client = MagicMock()
        mock_client.update_token_status.return_value = True
        mock_get.return_value = mock_client

        resp = test_client.put(
            '/api/plasec/identities/abc/tokens/tok1/status',
            json={'status': '2'},
        )
        assert resp.get_json()['updated'] is True

    @patch('src.server._get_client')
    def test_get_card_formats(self, mock_get, test_client):
        mock_client = MagicMock()
        mock_client.get_card_formats.return_value = [
            {'id': 'f1', 'name': '26-bit', 'facility_code': '100'},
        ]
        mock_get.return_value = mock_client

        resp = test_client.get('/api/plasec/card_formats')
        data = resp.get_json()
        assert len(data['card_formats']) == 1

    @patch('src.server._get_client', side_effect=ValueError("Plasec not configured"))
    def test_unconfigured_returns_error(self, mock_get, test_client):
        resp = test_client.get('/api/plasec/identities')
        assert resp.status_code == 502
        assert 'not configured' in resp.get_json()['error'].lower()
