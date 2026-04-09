"""
Localhost HTTP server that proxies requests from the Chrome extension
to the Plasec/Avigilon server. Handles SSL bypass and XML parsing.

All endpoints return JSON. The Chrome extension never sees XML or
deals with self-signed certificates.
"""

import logging
import time
from typing import Optional

from flask import Flask, g, jsonify, request
from flask_cors import CORS

from .plasec_client import PlaSecClient, PlaSecAuthError
from .config import load_config, save_config
from .constants import BRIDGE_PORT

logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=['chrome-extension://*'])


@app.before_request
def _log_request():
    g.start_time = time.time()
    body = ''
    if request.content_length and request.content_length > 0:
        body = request.get_data(as_text=True)
        if len(body) > 300:
            body = body[:300] + '...'
        body = f' body={body}'
    logger.info(f"→ {request.method} {request.path}{body}")


@app.after_request
def _log_response(response):
    elapsed = (time.time() - g.get('start_time', time.time())) * 1000
    body = ''
    if response.content_type and 'json' in response.content_type:
        resp_data = response.get_data(as_text=True)
        if len(resp_data) > 300:
            resp_data = resp_data[:300] + '...'
        body = f' {resp_data}'
    logger.info(f"← {response.status_code} {request.path} ({elapsed:.0f}ms){body}")
    return response

# Shared client instance — created on first use or config change
_client: Optional[PlaSecClient] = None


def _get_client() -> PlaSecClient:
    """Get or create the Plasec client from saved config."""
    global _client
    if _client is not None:
        return _client

    config = load_config()
    plasec = config.get('plasec', {})
    host = plasec.get('host', '')
    username = plasec.get('username', '')
    password = plasec.get('password', '')

    if not host or not username:
        raise ValueError("Plasec not configured — set host/username/password via /api/config")

    _client = PlaSecClient(host, username, password)
    return _client


def _reset_client():
    """Force re-creation of the client (after config change)."""
    global _client
    _client = None


# ------------------------------------------------------------------
# Health / status
# ------------------------------------------------------------------

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'avigilon-bridge'})


@app.route('/api/status', methods=['GET'])
def status():
    config = load_config()
    has_plasec = bool(config.get('plasec', {}).get('host'))
    has_ag = bool(config.get('accessgrid', {}).get('account_id'))
    return jsonify({
        'configured': has_plasec,
        'accessgrid_configured': has_ag,
        'plasec_host': config.get('plasec', {}).get('host', ''),
    })


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

@app.route('/api/config', methods=['GET'])
def get_config():
    config = load_config()
    safe = {
        'plasec': {
            'host': config.get('plasec', {}).get('host', ''),
            'username': config.get('plasec', {}).get('username', ''),
            'has_password': bool(config.get('plasec', {}).get('password')),
        },
        'accessgrid': {
            'account_id': config.get('accessgrid', {}).get('account_id', ''),
            'template_id': config.get('accessgrid', {}).get('template_id', ''),
            'has_secret': bool(config.get('accessgrid', {}).get('api_secret')),
        },
    }
    return jsonify(safe)


@app.route('/api/config', methods=['POST'])
def set_config():
    data = request.get_json(force=True)
    config = load_config()

    if 'plasec' in data:
        config.setdefault('plasec', {})
        for key in ('host', 'username', 'password'):
            if key in data['plasec']:
                config['plasec'][key] = data['plasec'][key]

    if 'accessgrid' in data:
        config.setdefault('accessgrid', {})
        for key in ('account_id', 'api_secret', 'template_id'):
            if key in data['accessgrid']:
                config['accessgrid'][key] = data['accessgrid'][key]

    save_config(config)
    _reset_client()
    return jsonify({'status': 'ok'})


# ------------------------------------------------------------------
# Plasec proxy endpoints
# ------------------------------------------------------------------

@app.route('/api/plasec/test', methods=['POST'])
def plasec_test():
    try:
        client = _get_client()
        ok = client.test_connection()
        return jsonify({'connected': ok})
    except Exception as e:
        return jsonify({'connected': False, 'error': str(e)}), 200


@app.route('/api/plasec/identities', methods=['GET'])
def plasec_identities():
    try:
        client = _get_client()
        identities = client.get_all_identities()
        return jsonify({'identities': identities, 'count': len(identities)})
    except Exception as e:
        logger.error(f"Failed to fetch identities: {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/plasec/identities/xml', methods=['GET'])
def plasec_identities_xml():
    """Fetch identities via XML endpoint (used as fallback)."""
    try:
        client = _get_client()
        identities = client.get_identities_xml()
        return jsonify({'identities': identities, 'count': len(identities)})
    except Exception as e:
        logger.error(f"Failed to fetch identities (XML): {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/plasec/identities/<identity_id>', methods=['GET'])
def plasec_identity(identity_id):
    try:
        client = _get_client()
        identity = client.get_identity(identity_id)
        if identity:
            return jsonify(identity)
        return jsonify({'error': 'not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/plasec/identities/<identity_id>/tokens', methods=['GET'])
def plasec_tokens(identity_id):
    try:
        client = _get_client()
        use_xml = request.args.get('format') == 'xml'
        if use_xml:
            tokens = client.get_identity_tokens_xml(identity_id)
        else:
            tokens = client.get_identity_tokens(identity_id)
        return jsonify({'tokens': tokens, 'count': len(tokens)})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/plasec/identities', methods=['POST'])
def plasec_create_identity():
    try:
        client = _get_client()
        data = request.get_json(force=True)
        new_id = client.create_identity(data)
        if new_id:
            return jsonify({'identity_id': new_id})
        return jsonify({'error': 'creation failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/plasec/identities/<identity_id>/tokens', methods=['POST'])
def plasec_create_token(identity_id):
    try:
        client = _get_client()
        data = request.get_json(force=True)
        token_id = client.create_token(identity_id, data)
        if token_id:
            return jsonify({'token_id': token_id})
        return jsonify({'error': 'creation failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/plasec/identities/<identity_id>/tokens/<token_id>/status', methods=['PUT'])
def plasec_update_token_status(identity_id, token_id):
    try:
        client = _get_client()
        data = request.get_json(force=True)
        ok = client.update_token_status(
            identity_id, token_id,
            data['status'],
            data.get('current_token_data'),
        )
        return jsonify({'updated': ok})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/plasec/identities/<identity_id>/tokens/<token_id>', methods=['DELETE'])
def plasec_delete_token(identity_id, token_id):
    try:
        client = _get_client()
        ok = client.delete_token(identity_id, token_id)
        return jsonify({'deleted': ok})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/plasec/card_formats', methods=['GET'])
def plasec_card_formats():
    try:
        client = _get_client()
        formats = client.get_card_formats()
        return jsonify({'card_formats': formats})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


# ------------------------------------------------------------------
# Error handlers
# ------------------------------------------------------------------

@app.errorhandler(PlaSecAuthError)
def handle_auth_error(e):
    return jsonify({'error': 'Authentication failed', 'detail': str(e)}), 401


@app.errorhandler(ValueError)
def handle_value_error(e):
    return jsonify({'error': str(e)}), 400


@app.errorhandler(Exception)
def handle_generic_error(e):
    logger.error(f"Unhandled error: {e}", exc_info=True)
    return jsonify({'error': 'Internal server error', 'detail': str(e)}), 500


def run_server(port: int = BRIDGE_PORT):
    """Start the Flask server (called from main.py in a thread)."""
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
