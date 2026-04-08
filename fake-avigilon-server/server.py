#!/usr/bin/env python3
"""
Fake Avigilon / Plasec server for local development and testing.

Faithfully replicates the real Plasec HTTP API behaviour as documented
in accessgrid-avigilon-doc.txt, including:

  - POST /sessions              → login, sets _session_id + XSRF-TOKEN cookies
  - GET  /identities.json       → paginated JSON:API identity list
  - GET  /identities/{cn}.json  → single identity detail (with tokens embedded)
  - GET  /identities.xml        → XML identity search
  - GET  /identities/{cn}/tokens.json → token list (JSON:API)
  - GET  /identities/{cn}/tokens.xml  → token list (XML)
  - POST /identities            → create identity (302 redirect)
  - POST /identities/{cn}/tokens      → create token (302 redirect)
  - POST /identities/{cn}/tokens/{tcn} → update/delete token (302 redirect)
  - GET  /identities/{cn}/photos.xml  → photo list (XML)
  - GET  /card_formats.json     → card format list
  - GET  /sessions/new          → login page (HTML)

Uses HTTPS with a self-signed certificate to match real Plasec behaviour.
Stores data in memory — resets on restart.

Usage:
  python server.py                     # HTTPS on port 443 (needs sudo)
  python server.py --port 8443         # HTTPS on port 8443
  python server.py --no-ssl --port 8080  # HTTP only (for simpler testing)
"""

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import ssl
import sys
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, make_response, redirect, Response

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
logger = logging.getLogger('fake-avigilon')

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory data store
# ---------------------------------------------------------------------------

SESSIONS = {}  # session_id -> {login, csrf_token, created_at}


def _new_id():
    return secrets.token_hex(8)


def _now_ldap():
    return datetime.now(timezone.utc).strftime('%Y%m%d%H%M%SZ')


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _one_year_later_iso():
    return (datetime.now(timezone.utc) + timedelta(days=365)).strftime('%Y-%m-%dT%H:%M:%S.000Z')


# Seed data — realistic identities with tokens
IDENTITIES = {}
TOKENS = {}  # keyed by identity_id -> {token_id: token_data}
CARD_FORMATS = {}
PHOTOS = {}  # keyed by identity_id -> [photo_data]


def _seed_data():
    """Populate initial test data matching real Plasec responses."""
    people = [
        {'fname': 'System', 'lname': 'Administrator', 'login': 'admin',
         'email': 'admin@accessgrid.com', 'phone': '', 'title': 'Administrator',
         'department': 'IT', 'status': '1'},
        {'fname': 'LUIS ALBERTO', 'lname': 'AQUISE FLORES', 'login': '',
         'email': 'laquise@company.com', 'phone': '555-0101', 'title': 'Security Officer',
         'department': 'Security', 'status': '1'},
        {'fname': 'MARITE', 'lname': 'ARAGAKI', 'login': '',
         'email': 'maragaki@company.com', 'phone': '555-0102', 'title': 'Receptionist',
         'department': 'Front Desk', 'status': '1'},
        {'fname': 'FORTUNATO', 'lname': 'BRESCIA MOREYRA', 'login': '',
         'email': 'fbrescia@company.com', 'phone': '555-0103', 'title': 'Director',
         'department': 'Operations', 'status': '1'},
        {'fname': 'PIA', 'lname': 'BURASCHI', 'login': '',
         'email': 'pburaschi@company.com', 'phone': '555-0104', 'title': 'Manager',
         'department': 'HR', 'status': '1'},
        {'fname': 'GONZALO', 'lname': 'BARRERA', 'login': '',
         'email': 'gbarrera@company.com', 'phone': '555-0105', 'title': 'Analyst',
         'department': 'Finance', 'status': '1'},
        {'fname': 'JESUS LUIS', 'lname': 'BERROSPI AYALA', 'login': '',
         'email': 'jberrospi@company.com', 'phone': '555-0106', 'title': 'Engineer',
         'department': 'Engineering', 'status': '1'},
        {'fname': 'CELIA', 'lname': 'CABALLERO', 'login': '',
         'email': 'ccaballero@company.com', 'phone': '555-0107', 'title': 'Coordinator',
         'department': 'Events', 'status': '1'},
        {'fname': 'ALEXANDER', 'lname': 'ANCCA', 'login': '',
         'email': '', 'phone': '555-0108', 'title': '', 'department': '',
         'status': '1'},
        {'fname': 'PEDRO', 'lname': 'BRESCIA MOREYRA', 'login': '',
         'email': 'pbrescia@company.com', 'phone': '', 'title': 'VP',
         'department': 'Operations', 'status': '2'},  # Inactive
    ]

    for i, p in enumerate(people):
        cn = _new_id()
        if p['login'] == 'admin':
            cn = '0'  # admin is always cn=0

        now_ldap = _now_ldap()
        now_iso = _now_iso()

        IDENTITIES[cn] = {
            'cn': cn,
            'plasecFname': p['fname'],
            'plasecLname': p['lname'],
            'plasecName': f"{p['lname']}, {p['fname']}",
            'plasecIdstatus': p['status'],
            'plasecLogin': p['login'],
            'plasecidentityEmailaddress': p['email'],
            'plasecidentityPhone': p['phone'],
            'plasecidentityWorkphone': '',
            'plasecidentityTitle': p['title'],
            'plasecidentityDepartment': p['department'],
            'plasecidentityDivision': '',
            'plasecidentityAddress': '',
            'plasecidentityCity': '',
            'plasecidentityState': '',
            'plasecidentityZipcode': '',
            'plasecIssuedate': now_iso,
            'plasecTyp': 'Employee' if p['login'] != 'admin' else '1',
            'plasecidentityForcedPasswordChange': 'TRUE',
            'plasecidentityMultifactorAuthentication': 'FALSE',
            'plasecidentityPagetimeout': '600000',
            'createTimestamp': now_ldap,
            'modifyTimestamp': now_ldap,
            'structuralObjectClass': 'plasecIdentity',
            'entryUUID': str(uuid.uuid4()),
            'hasSubordinates': 'TRUE',
            'plasecidentityRoleDN': [f'cn={_new_id()},ou=roles,dc=plasec'],
        }

        # Give active identities tokens — some with AccessGrid embossed number
        TOKENS[cn] = {}
        if p['status'] == '1' and p['login'] != 'admin':
            # First token: AccessGrid-marked
            tid1 = _new_id()
            card_num = str(10000 + i)
            TOKENS[cn][tid1] = {
                'cn': tid1,
                'plasecInternalnumber': card_num,
                'plasecEmbossednumber': 'AccessGrid',
                'plasecPIN': '',
                'plasecTokenstatus': '1',
                'plasecTokenType': '0',
                'plasecTokenlevel': '0',
                'plasecDownload': 'TRUE',
                'plasecTokenMobileAppType': '0',
                'plasecTokenOrigoMobileIdType': '1',
                'plasecTokenUnitofUpdatePeriod': '0',
                'plasecTokennoexpire': 'FALSE',
                'plasecIssuedate': now_ldap,
                'plasecActivatedate': now_ldap,
                'plasecDeactivatedate': (datetime.now(timezone.utc) + timedelta(days=365)).strftime('%Y%m%d%H%M%SZ'),
                'plasecTokenEnableReValidation': 'FALSE',
                'token_status': 'Active',
                'formatted_issue_date': now_iso,
                'formatted_activate_date': now_iso,
                'formatted_deactivate_date': _one_year_later_iso(),
            }

            # Some identities get a second non-AccessGrid token
            if i % 3 == 0:
                tid2 = _new_id()
                TOKENS[cn][tid2] = {
                    **TOKENS[cn][tid1],
                    'cn': tid2,
                    'plasecInternalnumber': str(20000 + i),
                    'plasecEmbossednumber': str(20000 + i),
                    'token_status': 'Active',
                }

    # Card formats
    for name, fc, bits in [
        ('26-bit Wiegand', '100', '26'),
        ('34-bit HID', '200', '34'),
        ('48-bit Corporate', '300', '48'),
    ]:
        fid = _new_id()
        CARD_FORMATS[fid] = {
            'cn': fid,
            'plasecName': name,
            'plaseccfmtFacilitycode': fc,
            'plaseccfmtMaxdigits': bits,
            'plaseccfmtFcodelen': '8',
            'plaseccfmtCardlen': str(int(bits) - 10),
            'plaseccfmtType': 'wiegand',
        }

    logger.info(f"Seeded {len(IDENTITIES)} identities, "
                f"{sum(len(t) for t in TOKENS.values())} tokens, "
                f"{len(CARD_FORMATS)} card formats")


# ---------------------------------------------------------------------------
# Session / auth helpers
# ---------------------------------------------------------------------------

def _get_session():
    sid = request.cookies.get('_session_id', '')
    return SESSIONS.get(sid)


def _require_session():
    sess = _get_session()
    if not sess:
        return redirect('/sessions/new', code=302)
    return sess


def _check_csrf():
    """Verify X-CSRF-Token header matches session (for write ops)."""
    sess = _get_session()
    if not sess:
        return False
    token = request.headers.get('X-CSRF-Token', '')
    return token == sess.get('csrf_token', '')


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# --- Login ---

@app.route('/sessions/new', methods=['GET'])
def sessions_new():
    return '<html><body><h1>Plasec Login</h1><form method="POST" action="/sessions"><input name="login"><input name="password" type="password"><button>Login</button></form></body></html>'


@app.route('/sessions', methods=['POST'])
def sessions_create():
    # Accept both JSON and form-encoded
    if request.is_json:
        data = request.get_json(force=True)
    else:
        data = request.form

    login = data.get('login', '')
    password = data.get('password', '')

    # Accept any credentials for the fake server
    if not login:
        return jsonify({'error': 'login required'}), 401

    session_id = secrets.token_hex(16)
    csrf_token = secrets.token_urlsafe(64)

    SESSIONS[session_id] = {
        'login': login,
        'csrf_token': csrf_token,
        'created_at': _now_iso(),
    }

    resp = make_response(jsonify({
        'success': True,
        'login': login,
        'message': f'Welcome, {login}',
    }))
    resp.set_cookie('_session_id', session_id, httponly=True, path='/')
    resp.set_cookie('XSRF-TOKEN', csrf_token, path='/')
    resp.headers['X-Request-Id'] = str(uuid.uuid4())

    logger.info(f"Login: {login} → session {session_id[:8]}...")
    return resp


# --- Identities (JSON) ---

@app.route('/identities.json', methods=['GET'])
def identities_json():
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('perpage', 100))

    all_ids = sorted(IDENTITIES.keys())
    total = len(all_ids)
    start = (page - 1) * per_page
    end = start + per_page
    page_ids = all_ids[start:end]

    data = []
    for cn in page_ids:
        ident = IDENTITIES[cn]
        data.append({
            'id': cn,
            'type': 'Identity',
            'attributes': {
                'cn': cn,
                'plasecFname': ident['plasecFname'],
                'plasecLname': ident['plasecLname'],
                'plasecName': ident['plasecName'],
                'plasecIdstatus': int(ident['plasecIdstatus']),
                'plasecIssuedate': ident['plasecIssuedate'],
                'plasecidentityPagetimeout': ident['plasecidentityPagetimeout'],
                'structuralObjectClass': 'plasecIdentity',
                'createTimestamp': ident['createTimestamp'],
                'modifyTimestamp': ident['modifyTimestamp'],
                'hasSubordinates': ident['hasSubordinates'] == 'TRUE',
                'dn': f"cn={cn},ou=identities,dc=plasec",
            },
        })

    return jsonify({
        'data': data,
        'meta': {
            'recordsTotal': total,
            'recordsFiltered': total,
        },
    })


# --- Identity detail (JSON) ---

@app.route('/identities/<cn>.json', methods=['GET'])
def identity_detail_json(cn):
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    ident = IDENTITIES.get(cn)
    if not ident:
        return jsonify({'error': 'not found'}), 404

    # Build tokens array
    tokens_list = []
    for tid, tok in TOKENS.get(cn, {}).items():
        tokens_list.append({
            'id': tid,
            'type': 'Token',
            'attributes': {
                'cn': tid,
                'plasecDownload': tok['plasecDownload'] == 'TRUE',
                'plasecTokenlevel': int(tok['plasecTokenlevel']),
                'plasecInternalnumber': tok['plasecInternalnumber'],
                'plasecTokenMobileAppType': int(tok['plasecTokenMobileAppType']),
                'plasecTokenstatus': int(tok['plasecTokenstatus']),
                'plasecTokenType': int(tok['plasecTokenType']),
                'plasecTokenUnitofUpdatePeriod': int(tok['plasecTokenUnitofUpdatePeriod']),
                'plasecEmbossednumber': tok['plasecEmbossednumber'],
                'plasecTokennoexpire': tok['plasecTokennoexpire'] == 'TRUE',
                'plasecIssuedate': tok.get('formatted_issue_date', ''),
                'plasecActivatedate': tok.get('formatted_activate_date', ''),
                'plasecDeactivatedate': tok.get('formatted_deactivate_date', ''),
                'dn': f"cn={tid},ou=tokens,cn={cn},ou=identities,dc=plasec",
                'plasecName': None,
                'plasecLastdoor': '',
            },
        })

    return jsonify({
        'id': cn,
        'type': 'Identity',
        'attributes': {
            'cn': cn,
            'plasecFname': ident['plasecFname'],
            'plasecLname': ident['plasecLname'],
            'plasecName': ident['plasecName'],
            'plasecIdstatus': int(ident['plasecIdstatus']),
            'plasecidentityEmailaddress': ident.get('plasecidentityEmailaddress', ''),
            'plasecidentityPhone': ident.get('plasecidentityPhone', ''),
            'plasecidentityWorkphone': ident.get('plasecidentityWorkphone', ''),
            'plasecidentityTitle': ident.get('plasecidentityTitle', ''),
            'plasecidentityDepartment': ident.get('plasecidentityDepartment', ''),
            'plasecidentityDivision': ident.get('plasecidentityDivision', ''),
            'plasecidentityAddress': ident.get('plasecidentityAddress', ''),
            'plasecidentityCity': ident.get('plasecidentityCity', ''),
            'plasecidentityState': ident.get('plasecidentityState', ''),
            'plasecidentityZipcode': ident.get('plasecidentityZipcode', ''),
            'plasecidentityForcedPasswordChange': ident['plasecidentityForcedPasswordChange'] == 'TRUE',
            'plasecidentityMultifactorAuthentication': ident['plasecidentityMultifactorAuthentication'] == 'TRUE',
            'plasecidentityPagetimeout': ident['plasecidentityPagetimeout'],
            'plasecIssuedate': ident['plasecIssuedate'],
            'plasecTyp': ident.get('plasecTyp', ''),
            'structuralObjectClass': 'plasecIdentity',
            'createTimestamp': ident['createTimestamp'],
            'modifyTimestamp': ident['modifyTimestamp'],
            'hasSubordinates': ident['hasSubordinates'] == 'TRUE',
            'dn': f"cn={cn},ou=identities,dc=plasec",
            'creatorsName': ['cn=0,ou=identities,dc=plasec'],
            'modifiersName': ['cn=0,ou=identities,dc=plasec'],
            'entryDN': [f'cn={cn},ou=identities,dc=plasec'],
            'subschemaSubentry': ['cn=Subschema'],
            'plasecidentityRoleDN': ident.get('plasecidentityRoleDN', []),
        },
        'tokens': tokens_list,
    })


# --- Identities (XML) ---

@app.route('/identities.xml', methods=['GET'])
def identities_xml():
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    lines = ['<identities type="array">']
    for cn, ident in sorted(IDENTITIES.items()):
        lines.append('<identity>')
        lines.append(f'<dn>cn={cn},ou=identities,dc=plasec</dn>')
        lines.append(f'<cns type="array"><cn>{cn}</cn></cns>')
        lines.append(f'<createTimestamp>{ident["createTimestamp"]}</createTimestamp>')
        lines.append(f'<creatorsName>cn=0,ou=identities,dc=plasec</creatorsName>')
        lines.append(f'<entryDN>cn={cn},ou=identities,dc=plasec</entryDN>')
        lines.append(f'<entryUUID>{ident["entryUUID"]}</entryUUID>')
        lines.append(f'<hasSubordinates>{ident["hasSubordinates"]}</hasSubordinates>')
        lines.append(f'<modifyTimestamp>{ident["modifyTimestamp"]}</modifyTimestamp>')
        lines.append('<objectClasses type="array"><objectClass>plasecIdentity</objectClass></objectClasses>')
        if ident['plasecFname']:
            lines.append(f'<plasecFname>{ident["plasecFname"]}</plasecFname>')
        lines.append(f'<plasecIdstatus>{ident["plasecIdstatus"]}</plasecIdstatus>')
        lines.append(f'<plasecIssuedate>{ident["plasecIssuedate"]}</plasecIssuedate>')
        if ident['plasecLname']:
            lines.append(f'<plasecLname>{ident["plasecLname"]}</plasecLname>')
        lines.append(f'<plasecName>{ident["plasecName"]}</plasecName>')
        if ident.get('plasecLogin'):
            lines.append(f'<plasecLogin>{ident["plasecLogin"]}</plasecLogin>')
        lines.append('<plasecidentityDarkModes type="array"><plasecidentityDarkMode>default</plasecidentityDarkMode></plasecidentityDarkModes>')
        lines.append(f'<plasecidentityForcedPasswordChange>{ident["plasecidentityForcedPasswordChange"]}</plasecidentityForcedPasswordChange>')
        lines.append(f'<plasecidentityMultifactorAuthentication>{ident["plasecidentityMultifactorAuthentication"]}</plasecidentityMultifactorAuthentication>')
        lines.append(f'<plasecidentityPagetimeout>{ident["plasecidentityPagetimeout"]}</plasecidentityPagetimeout>')
        role_dns = ident.get('plasecidentityRoleDN', [])
        if role_dns:
            lines.append('<plasecidentityRoleDNs type="array">')
            for rdn in role_dns:
                lines.append(f'<plasecidentityRoleDN>{rdn}</plasecidentityRoleDN>')
            lines.append('</plasecidentityRoleDNs>')
        lines.append('<structuralObjectClass>plasecIdentity</structuralObjectClass>')
        lines.append('<subschemaSubentry>cn=Subschema</subschemaSubentry>')
        lines.append('</identity>')
    lines.append('</identities>')

    return Response('\n'.join(lines), mimetype='application/xml')


# --- Tokens (JSON) ---

@app.route('/identities/<cn>/tokens.json', methods=['GET'])
def tokens_json(cn):
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    if cn not in IDENTITIES:
        return jsonify({'error': 'identity not found'}), 404

    tokens_data = []
    for tid, tok in TOKENS.get(cn, {}).items():
        tokens_data.append({
            'id': tid,
            'type': 'Token',
            'attributes': {
                'cn': tid,
                'plasecInternalnumber': tok['plasecInternalnumber'],
                'plasecEmbossednumber': tok['plasecEmbossednumber'],
                'plasecTokenlevel': tok['plasecTokenlevel'],
                'TokenTypeId': int(tok['plasecTokenType']),
                'plasecTokenType': tok['plasecTokenType'],
                'extended_attributes': {
                    'token_status': tok.get('token_status', 'Active'),
                    'formatted_issue_date': tok.get('formatted_issue_date', ''),
                    'formatted_activate_date': tok.get('formatted_activate_date', ''),
                    'formatted_deactivate_date': tok.get('formatted_deactivate_date', ''),
                },
            },
        })

    return jsonify({
        'tokens': tokens_data,
        'recordsTotal': len(tokens_data),
        'recordsFiltered': None,
    })


# --- Tokens (XML) ---

@app.route('/identities/<cn>/tokens.xml', methods=['GET'])
def tokens_xml(cn):
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    lines = ['<tokens type="array">']
    for tid, tok in TOKENS.get(cn, {}).items():
        lines.append('<token>')
        lines.append(f'<dn>cn={tid},ou=tokens,cn={cn},ou=identities,dc=plasec</dn>')
        lines.append(f'<cns type="array"><cn>{tid}</cn></cns>')
        lines.append(f'<entryUUID>{uuid.uuid4()}</entryUUID>')
        lines.append('<objectClasses type="array"><objectClass>plasecToken</objectClass></objectClasses>')
        lines.append(f'<plasecActivatedate>{tok["plasecActivatedate"]}</plasecActivatedate>')
        lines.append(f'<plasecDeactivatedate>{tok["plasecDeactivatedate"]}</plasecDeactivatedate>')
        lines.append(f'<plasecDownload>{tok["plasecDownload"]}</plasecDownload>')
        lines.append(f'<plasecEmbossednumber>{tok["plasecEmbossednumber"]}</plasecEmbossednumber>')
        lines.append(f'<plasecInternalnumber>{tok["plasecInternalnumber"]}</plasecInternalnumber>')
        lines.append(f'<plasecIssuedate>{tok["plasecIssuedate"]}</plasecIssuedate>')
        lines.append(f'<plasecTokenMobileAppType>{tok["plasecTokenMobileAppType"]}</plasecTokenMobileAppType>')
        lines.append(f'<plasecTokenOrigoMobileIdType>{tok.get("plasecTokenOrigoMobileIdType", "1")}</plasecTokenOrigoMobileIdType>')
        lines.append(f'<plasecTokenType>{tok["plasecTokenType"]}</plasecTokenType>')
        lines.append(f'<plasecTokenUnitofUpdatePeriod>{tok["plasecTokenUnitofUpdatePeriod"]}</plasecTokenUnitofUpdatePeriod>')
        lines.append(f'<plasecTokenlevel>{tok["plasecTokenlevel"]}</plasecTokenlevel>')
        lines.append(f'<plasecTokennoexpire>{tok["plasecTokennoexpire"]}</plasecTokennoexpire>')
        lines.append(f'<plasecTokenstatus>{tok["plasecTokenstatus"]}</plasecTokenstatus>')
        lines.append('</token>')
    lines.append('</tokens>')

    return Response('\n'.join(lines), mimetype='application/xml')


# --- Create identity ---

@app.route('/identities', methods=['POST'])
def create_identity():
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    cn = _new_id()
    now_ldap = _now_ldap()
    now_iso = _now_iso()

    # Parse form data (same field names as the real API)
    lname = request.form.get('identity[plasecLname]', '')
    fname = request.form.get('identity[plasecFname]', '')
    email = request.form.get('identity[plasecidentityEmailaddress]', '')
    phone = request.form.get('identity[plasecidentityPhone]', '')
    workphone = request.form.get('identity[plasecidentityWorkphone]', '')
    title = request.form.get('identity[plasecidentityTitle]', '')
    department = request.form.get('identity[plasecidentityDepartment]', '')
    division = request.form.get('identity[plasecidentityDivision]', '')
    address = request.form.get('identity[plasecidentityAddress]', '')
    city = request.form.get('identity[plasecidentityCity]', '')
    state = request.form.get('identity[plasecidentityState]', '')
    zipcode = request.form.get('identity[plasecidentityZipcode]', '')
    status = request.form.get('identity[plasecIdstatus]', '1')
    typ = request.form.get('identity[plasecTyp]', 'Employee')

    IDENTITIES[cn] = {
        'cn': cn,
        'plasecFname': fname,
        'plasecLname': lname,
        'plasecName': f"{lname}, {fname}" if fname and lname else lname or fname,
        'plasecIdstatus': status,
        'plasecLogin': '',
        'plasecidentityEmailaddress': email,
        'plasecidentityPhone': phone,
        'plasecidentityWorkphone': workphone,
        'plasecidentityTitle': title,
        'plasecidentityDepartment': department,
        'plasecidentityDivision': division,
        'plasecidentityAddress': address,
        'plasecidentityCity': city,
        'plasecidentityState': state,
        'plasecidentityZipcode': zipcode,
        'plasecIssuedate': now_iso,
        'plasecTyp': typ,
        'plasecidentityForcedPasswordChange': request.form.get('identity[plasecidentityForcedPasswordChange]', 'TRUE'),
        'plasecidentityMultifactorAuthentication': 'FALSE',
        'plasecidentityPagetimeout': request.form.get('identity[plasecidentityPagetimeout]', '600000'),
        'createTimestamp': now_ldap,
        'modifyTimestamp': now_ldap,
        'structuralObjectClass': 'plasecIdentity',
        'entryUUID': str(uuid.uuid4()),
        'hasSubordinates': 'FALSE',
        'plasecidentityRoleDN': [],
    }
    TOKENS[cn] = {}

    logger.info(f"Created identity: {cn} ({fname} {lname})")
    return redirect(f'/identities/{cn}', code=302)


# --- Create token ---

@app.route('/identities/<cn>/tokens', methods=['POST'])
def create_token(cn):
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    if cn not in IDENTITIES:
        return jsonify({'error': 'identity not found'}), 404

    tid = _new_id()
    now_ldap = _now_ldap()
    now_iso = _now_iso()

    internal = request.form.get('token[plasecInternalnumber]', '')
    embossed = request.form.get('token[plasecEmbossednumber]', '')

    TOKENS.setdefault(cn, {})[tid] = {
        'cn': tid,
        'plasecInternalnumber': internal,
        'plasecEmbossednumber': embossed,
        'plasecPIN': request.form.get('token[plasecPIN]', ''),
        'plasecTokenstatus': request.form.get('token[plasecTokenstatus]', '1'),
        'plasecTokenType': request.form.get('token[plasecTokenType]', '0'),
        'plasecTokenlevel': request.form.get('token[plasecTokenlevel]', '0'),
        'plasecDownload': request.form.get('token[plasecDownload]', 'TRUE'),
        'plasecTokenMobileAppType': request.form.get('token[plasecTokenMobileAppType]', '0'),
        'plasecTokenOrigoMobileIdType': request.form.get('token[plasecTokenOrigoMobileIdType]', '1'),
        'plasecTokenUnitofUpdatePeriod': request.form.get('token[plasecTokenUnitofUpdatePeriod]', '0'),
        'plasecTokennoexpire': request.form.get('token[plasecTokennoexpire]', 'FALSE'),
        'plasecIssuedate': now_ldap,
        'plasecActivatedate': request.form.get('plasecActivatedate', now_ldap),
        'plasecDeactivatedate': request.form.get('plasecDeactivatedate', (datetime.now(timezone.utc) + timedelta(days=365)).strftime('%Y%m%d%H%M%SZ')),
        'plasecTokenEnableReValidation': 'FALSE',
        'token_status': 'Active',
        'formatted_issue_date': now_iso,
        'formatted_activate_date': now_iso,
        'formatted_deactivate_date': _one_year_later_iso(),
    }

    IDENTITIES[cn]['hasSubordinates'] = 'TRUE'
    logger.info(f"Created token: {tid} for identity {cn} (internal={internal}, embossed={embossed})")
    return redirect(f'/identities/{cn}/tokens/{tid}', code=302)


# --- Update / Delete token ---

@app.route('/identities/<cn>/tokens/<tid>', methods=['POST'])
def update_or_delete_token(cn, tid):
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    if cn not in IDENTITIES or tid not in TOKENS.get(cn, {}):
        return jsonify({'error': 'not found'}), 404

    method = request.form.get('_method', '').lower()

    if method == 'delete':
        del TOKENS[cn][tid]
        if not TOKENS[cn]:
            IDENTITIES[cn]['hasSubordinates'] = 'FALSE'
        logger.info(f"Deleted token: {tid} from identity {cn}")
        return redirect(f'/identities/{cn}/tokens', code=302)

    # Update (method=put)
    tok = TOKENS[cn][tid]
    for field in [
        'plasecTokenstatus', 'plasecInternalnumber', 'plasecEmbossednumber',
        'plasecPIN', 'plasecTokenType', 'plasecTokenlevel', 'plasecDownload',
        'plasecTokenMobileAppType', 'plasecTokenOrigoMobileIdType',
        'plasecTokenUnitofUpdatePeriod', 'plasecTokennoexpire',
    ]:
        form_key = f'token[{field}]'
        if form_key in request.form:
            tok[field] = request.form[form_key]

    # Update status string to match numeric
    status_map = {'1': 'Active', '2': 'Inactive', '3': 'Not yet active', '4': 'Expired'}
    tok['token_status'] = status_map.get(tok['plasecTokenstatus'], 'Active')

    for date_field in ['plasecIssuedate', 'plasecActivatedate', 'plasecDeactivatedate']:
        if date_field in request.form:
            tok[date_field] = request.form[date_field]

    tok['modifyTimestamp'] = _now_ldap()
    IDENTITIES[cn]['modifyTimestamp'] = _now_ldap()

    logger.info(f"Updated token: {tid} for identity {cn} (status={tok['plasecTokenstatus']})")
    return redirect(f'/identities/{cn}/tokens/{tid}', code=302)


# --- Photos ---

@app.route('/identities/<cn>/photos.xml', methods=['GET'])
def photos_xml(cn):
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    # Return empty photo list (no photos seeded)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<objects type="array">']
    for photo in PHOTOS.get(cn, []):
        lines.append('<object>')
        lines.append(f'<cn>{photo.get("cn", "0")}</cn>')
        lines.append(f'<plasecidentityPrimaryimage type="boolean">{str(photo.get("primary", True)).lower()}</plasecidentityPrimaryimage>')
        lines.append(f'<plasecidentityFilename>{photo.get("filename", "photo.jpeg")}</plasecidentityFilename>')
        lines.append(f'<plasecidentityContentype>image/jpeg</plasecidentityContentype>')
        lines.append('</object>')
    lines.append('</objects>')
    return Response('\n'.join(lines), mimetype='application/xml')


# --- Card formats ---

@app.route('/card_formats.json', methods=['GET'])
def card_formats_json():
    sess = _require_session()
    if not isinstance(sess, dict):
        return sess

    data = []
    for fid, fmt in CARD_FORMATS.items():
        data.append({
            'id': fid,
            'type': 'CardFormat',
            'attributes': {
                'cn': fid,
                'plasecName': fmt['plasecName'],
                'plaseccfmtFacilitycode': fmt['plaseccfmtFacilitycode'],
                'plaseccfmtMaxdigits': fmt['plaseccfmtMaxdigits'],
                'plaseccfmtFcodelen': fmt['plaseccfmtFcodelen'],
                'plaseccfmtCardlen': fmt['plaseccfmtCardlen'],
                'plaseccfmtType': fmt['plaseccfmtType'],
            },
        })

    return jsonify({'data': data})


# ---------------------------------------------------------------------------
# SSL certificate generation
# ---------------------------------------------------------------------------

def generate_self_signed_cert(cert_dir):
    """Generate a self-signed cert + key for HTTPS using Python's cryptography library.

    Works on all platforms (no openssl CLI dependency).
    """
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    cert_path = os.path.join(cert_dir, 'cert.pem')
    key_path = os.path.join(cert_dir, 'key.pem')

    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    logger.info("Generating self-signed certificate...")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'fake-avigilon'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'AccessGrid'),
        x509.NameAttribute(NameOID.COUNTRY_NAME, 'US'),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName('localhost'),
                x509.IPAddress(ipaddress.IPv4Address('127.0.0.1')),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    with open(key_path, 'wb') as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    with open(cert_path, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    logger.info(f"Certificate written to {cert_path}")
    return cert_path, key_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Fake Avigilon / Plasec Server')
    parser.add_argument('--port', type=int, default=8443, help='Port to listen on (default: 8443)')
    parser.add_argument('--no-ssl', action='store_true', help='Run without SSL (HTTP only)')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    args = parser.parse_args()

    _seed_data()

    if args.no_ssl:
        logger.info(f"Starting HTTP server on {args.host}:{args.port}")
        app.run(host=args.host, port=args.port, debug=True)
    else:
        cert_dir = os.path.join(tempfile.gettempdir(), 'fake-avigilon-certs')
        os.makedirs(cert_dir, exist_ok=True)
        cert_path, key_path = generate_self_signed_cert(cert_dir)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)

        logger.info(f"Starting HTTPS server on {args.host}:{args.port}")
        logger.info(f"Login with any username/password (e.g. admin / anything)")
        logger.info(f"  curl -k https://localhost:{args.port}/sessions -d '{{\"login\":\"admin\",\"password\":\"test\"}}'")
        app.run(host=args.host, port=args.port, debug=True, ssl_context=(cert_path, key_path))


if __name__ == '__main__':
    main()
