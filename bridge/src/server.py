"""
Localhost HTTP server that proxies requests from the Chrome extension
to the Avigilon server. Handles SSL bypass and XML parsing.

All endpoints return JSON. The Chrome extension never sees XML or
deals with self-signed certificates.
"""

import logging
import time
from typing import Optional

from flask import Flask, g, jsonify, request
from flask_cors import CORS

from .avigilon_client import AvigilonClient, AvigilonAuthError
from .config import load_config, save_config
from .constants import BRIDGE_PORT

logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


@app.before_request
def _log_request():
    g.start_time = time.time()
    try:
        body = ''
        if request.content_length and request.content_length > 0:
            body = request.get_data(as_text=True)
            if len(body) > 300:
                body = body[:300] + '...'
            body = f' body={body}'
        logger.info(f"→ {request.method} {request.path}{body}")
    except Exception as e:
        logger.warning(f"Failed to log request: {e}")


@app.after_request
def _log_response(response):
    try:
        elapsed = (time.time() - g.get('start_time', time.time())) * 1000
        body = ''
        if response.content_type and 'json' in response.content_type:
            resp_data = response.get_data(as_text=True)
            if len(resp_data) > 300:
                resp_data = resp_data[:300] + '...'
            body = f' {resp_data}'
        logger.info(f"← {response.status_code} {request.path} ({elapsed:.0f}ms){body}")
    except Exception as e:
        logger.warning(f"Failed to log response: {e}")
    return response

# Shared client instance — created on first use or config change
_client: Optional[AvigilonClient] = None


def _get_client() -> AvigilonClient:
    """Get or create the Avigilon client from saved config."""
    global _client
    if _client is not None:
        return _client

    config = load_config()
    avigilon = config.get('avigilon', {})
    host = avigilon.get('host', '')
    username = avigilon.get('username', '')
    password = avigilon.get('password', '')

    if not host or not username:
        raise ValueError("Avigilon not configured — set host/username/password via /api/config")

    _client = AvigilonClient(host, username, password)
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
    has_avigilon = bool(config.get('avigilon', {}).get('host'))
    has_ag = bool(config.get('accessgrid', {}).get('account_id'))
    return jsonify({
        'configured': has_avigilon,
        'accessgrid_configured': has_ag,
        'avigilon_host': config.get('avigilon', {}).get('host', ''),
    })


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

@app.route('/api/config', methods=['GET'])
def get_config():
    config = load_config()
    safe = {
        'avigilon': {
            'host': config.get('avigilon', {}).get('host', ''),
            'username': config.get('avigilon', {}).get('username', ''),
            'has_password': bool(config.get('avigilon', {}).get('password')),
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

    if 'avigilon' in data:
        config.setdefault('avigilon', {})
        for key in ('host', 'username', 'password'):
            if key in data['avigilon']:
                config['avigilon'][key] = data['avigilon'][key]

    if 'accessgrid' in data:
        config.setdefault('accessgrid', {})
        for key in ('account_id', 'api_secret', 'template_id'):
            if key in data['accessgrid']:
                config['accessgrid'][key] = data['accessgrid'][key]

    save_config(config)
    _reset_client()
    return jsonify({'status': 'ok'})


# ------------------------------------------------------------------
# Avigilon proxy endpoints
# ------------------------------------------------------------------

@app.route('/api/avigilon/test', methods=['POST'])
def avigilon_test():
    try:
        client = _get_client()
        ok = client.test_connection()
        return jsonify({'connected': ok})
    except Exception as e:
        return jsonify({'connected': False, 'error': str(e)}), 200


@app.route('/api/avigilon/identities', methods=['GET'])
def avigilon_identities():
    try:
        client = _get_client()
        logger.info("Fetching all identities from Avigilon...")
        identities = client.get_all_identities()
        logger.info(f"Fetched {len(identities)} identities from Avigilon")
        return jsonify({'identities': identities, 'count': len(identities)})
    except Exception as e:
        logger.error(f"Failed to fetch identities: {e}", exc_info=True)
        return jsonify({'error': str(e), 'type': type(e).__name__}), 502


@app.route('/api/avigilon/identities/xml', methods=['GET'])
def avigilon_identities_xml():
    """Fetch identities via XML endpoint (used as fallback)."""
    try:
        client = _get_client()
        identities = client.get_identities_xml()
        return jsonify({'identities': identities, 'count': len(identities)})
    except Exception as e:
        logger.error(f"Failed to fetch identities (XML): {e}")
        return jsonify({'error': str(e)}), 502


@app.route('/api/avigilon/identities/<identity_id>', methods=['GET'])
def avigilon_identity(identity_id):
    try:
        client = _get_client()
        identity = client.get_identity(identity_id)
        if identity:
            return jsonify(identity)
        return jsonify({'error': 'not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/avigilon/identities/<identity_id>/tokens', methods=['GET'])
def avigilon_tokens(identity_id):
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


@app.route('/api/avigilon/identities', methods=['POST'])
def avigilon_create_identity():
    try:
        client = _get_client()
        data = request.get_json(force=True)
        new_id = client.create_identity(data)
        if new_id:
            return jsonify({'identity_id': new_id})
        return jsonify({'error': 'creation failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/avigilon/identities/<identity_id>/tokens', methods=['POST'])
def avigilon_create_token(identity_id):
    try:
        client = _get_client()
        data = request.get_json(force=True)
        token_id = client.create_token(identity_id, data)
        if token_id:
            return jsonify({'token_id': token_id})
        return jsonify({'error': 'creation failed'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/avigilon/identities/<identity_id>/tokens/<token_id>/status', methods=['PUT'])
def avigilon_update_token_status(identity_id, token_id):
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


@app.route('/api/avigilon/identities/<identity_id>/tokens/<token_id>', methods=['DELETE'])
def avigilon_delete_token(identity_id, token_id):
    try:
        client = _get_client()
        ok = client.delete_token(identity_id, token_id)
        return jsonify({'deleted': ok})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/avigilon/card_formats', methods=['GET'])
def avigilon_card_formats():
    try:
        client = _get_client()
        formats = client.get_card_formats()
        return jsonify({'card_formats': formats})
    except Exception as e:
        return jsonify({'error': str(e)}), 502


# ------------------------------------------------------------------
# Error handlers
# ------------------------------------------------------------------

@app.errorhandler(AvigilonAuthError)
def handle_auth_error(e):
    return jsonify({'error': 'Authentication failed', 'detail': str(e)}), 401


@app.errorhandler(404)
def handle_not_found(e):
    logger.warning(f"404: {request.method} {request.url} — no matching route")
    return jsonify({'error': f'Not found: {request.method} {request.path}'}), 404


@app.errorhandler(ValueError)
def handle_value_error(e):
    return jsonify({'error': str(e)}), 400


@app.errorhandler(Exception)
def handle_generic_error(e):
    import traceback
    tb = traceback.format_exc()
    logger.error(f"Unhandled error on {request.method} {request.path}: {type(e).__name__}: {e}\n{tb}")
    return jsonify({
        'error': str(e),
        'type': type(e).__name__,
        'detail': tb,
    }), 500


def run_server(port: int = BRIDGE_PORT):
    """Start the Flask server (called from main.py in a thread)."""
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)
