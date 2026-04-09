#!/usr/bin/env python3
"""
Fake Avigilon / Avigilon server for local development and testing.

Faithfully replicates the real Avigilon HTTP API behaviour as documented
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

Uses HTTPS with a self-signed certificate to match real Avigilon behaviour.
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
    """Populate initial test data matching real Avigilon responses."""
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
            'avigilonFname': p['fname'],
            'avigilonLname': p['lname'],
            'avigilonName': f"{p['lname']}, {p['fname']}",
            'avigilonIdstatus': p['status'],
            'avigilonLogin': p['login'],
            'avigilonidentityEmailaddress': p['email'],
            'avigilonidentityPhone': p['phone'],
            'avigilonidentityWorkphone': '',
            'avigilonidentityTitle': p['title'],
            'avigilonidentityDepartment': p['department'],
            'avigilonidentityDivision': '',
            'avigilonidentityAddress': '',
            'avigilonidentityCity': '',
            'avigilonidentityState': '',
            'avigilonidentityZipcode': '',
            'avigilonIssuedate': now_iso,
            'avigilonTyp': 'Employee' if p['login'] != 'admin' else '1',
            'avigilonidentityForcedPasswordChange': 'TRUE',
            'avigilonidentityMultifactorAuthentication': 'FALSE',
            'avigilonidentityPagetimeout': '600000',
            'createTimestamp': now_ldap,
            'modifyTimestamp': now_ldap,
            'structuralObjectClass': 'avigilonIdentity',
            'entryUUID': str(uuid.uuid4()),
            'hasSubordinates': 'TRUE',
            'avigilonidentityRoleDN': [f'cn={_new_id()},ou=roles,dc=avigilon'],
        }

        # Give active identities tokens — some with AccessGrid embossed number
        TOKENS[cn] = {}
        if p['status'] == '1' and p['login'] != 'admin':
            # First token: AccessGrid-marked
            tid1 = _new_id()
            card_num = str(10000 + i)
            TOKENS[cn][tid1] = {
                'cn': tid1,
                'avigilonInternalnumber': card_num,
                'avigilonEmbossednumber': 'AccessGrid',
                'avigilonPIN': '',
                'avigilonTokenstatus': '1',
                'avigilonTokenType': '0',
                'avigilonTokenlevel': '0',
                'avigilonDownload': 'TRUE',
                'avigilonTokenMobileAppType': '0',
                'avigilonTokenOrigoMobileIdType': '1',
                'avigilonTokenUnitofUpdatePeriod': '0',
                'avigilonTokennoexpire': 'FALSE',
                'avigilonIssuedate': now_ldap,
                'avigilonActivatedate': now_ldap,
                'avigilonDeactivatedate': (datetime.now(timezone.utc) + timedelta(days=365)).strftime('%Y%m%d%H%M%SZ'),
                'avigilonTokenEnableReValidation': 'FALSE',
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
                    'avigilonInternalnumber': str(20000 + i),
                    'avigilonEmbossednumber': str(20000 + i),
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
            'avigilonName': name,
            'avigiloncfmtFacilitycode': fc,
            'avigiloncfmtMaxdigits': bits,
            'avigiloncfmtFcodelen': '8',
            'avigiloncfmtCardlen': str(int(bits) - 10),
            'avigiloncfmtType': 'wiegand',
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
    return '<html><body><h1>Avigilon Login</h1><form method="POST" action="/sessions"><input name="login"><input name="password" type="password"><button>Login</button></form></body></html>'


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
                'avigilonFname': ident['avigilonFname'],
                'avigilonLname': ident['avigilonLname'],
                'avigilonName': ident['avigilonName'],
                'avigilonIdstatus': int(ident['avigilonIdstatus']),
                'avigilonIssuedate': ident['avigilonIssuedate'],
                'avigilonidentityPagetimeout': ident['avigilonidentityPagetimeout'],
                'structuralObjectClass': 'avigilonIdentity',
                'createTimestamp': ident['createTimestamp'],
                'modifyTimestamp': ident['modifyTimestamp'],
                'hasSubordinates': ident['hasSubordinates'] == 'TRUE',
                'dn': f"cn={cn},ou=identities,dc=avigilon",
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
                'avigilonDownload': tok['avigilonDownload'] == 'TRUE',
                'avigilonTokenlevel': int(tok['avigilonTokenlevel']),
                'avigilonInternalnumber': tok['avigilonInternalnumber'],
                'avigilonTokenMobileAppType': int(tok['avigilonTokenMobileAppType']),
                'avigilonTokenstatus': int(tok['avigilonTokenstatus']),
                'avigilonTokenType': int(tok['avigilonTokenType']),
                'avigilonTokenUnitofUpdatePeriod': int(tok['avigilonTokenUnitofUpdatePeriod']),
                'avigilonEmbossednumber': tok['avigilonEmbossednumber'],
                'avigilonTokennoexpire': tok['avigilonTokennoexpire'] == 'TRUE',
                'avigilonIssuedate': tok.get('formatted_issue_date', ''),
                'avigilonActivatedate': tok.get('formatted_activate_date', ''),
                'avigilonDeactivatedate': tok.get('formatted_deactivate_date', ''),
                'dn': f"cn={tid},ou=tokens,cn={cn},ou=identities,dc=avigilon",
                'avigilonName': None,
                'avigilonLastdoor': '',
            },
        })

    return jsonify({
        'id': cn,
        'type': 'Identity',
        'attributes': {
            'cn': cn,
            'avigilonFname': ident['avigilonFname'],
            'avigilonLname': ident['avigilonLname'],
            'avigilonName': ident['avigilonName'],
            'avigilonIdstatus': int(ident['avigilonIdstatus']),
            'avigilonidentityEmailaddress': ident.get('avigilonidentityEmailaddress', ''),
            'avigilonidentityPhone': ident.get('avigilonidentityPhone', ''),
            'avigilonidentityWorkphone': ident.get('avigilonidentityWorkphone', ''),
            'avigilonidentityTitle': ident.get('avigilonidentityTitle', ''),
            'avigilonidentityDepartment': ident.get('avigilonidentityDepartment', ''),
            'avigilonidentityDivision': ident.get('avigilonidentityDivision', ''),
            'avigilonidentityAddress': ident.get('avigilonidentityAddress', ''),
            'avigilonidentityCity': ident.get('avigilonidentityCity', ''),
            'avigilonidentityState': ident.get('avigilonidentityState', ''),
            'avigilonidentityZipcode': ident.get('avigilonidentityZipcode', ''),
            'avigilonidentityForcedPasswordChange': ident['avigilonidentityForcedPasswordChange'] == 'TRUE',
            'avigilonidentityMultifactorAuthentication': ident['avigilonidentityMultifactorAuthentication'] == 'TRUE',
            'avigilonidentityPagetimeout': ident['avigilonidentityPagetimeout'],
            'avigilonIssuedate': ident['avigilonIssuedate'],
            'avigilonTyp': ident.get('avigilonTyp', ''),
            'structuralObjectClass': 'avigilonIdentity',
            'createTimestamp': ident['createTimestamp'],
            'modifyTimestamp': ident['modifyTimestamp'],
            'hasSubordinates': ident['hasSubordinates'] == 'TRUE',
            'dn': f"cn={cn},ou=identities,dc=avigilon",
            'creatorsName': ['cn=0,ou=identities,dc=avigilon'],
            'modifiersName': ['cn=0,ou=identities,dc=avigilon'],
            'entryDN': [f'cn={cn},ou=identities,dc=avigilon'],
            'subschemaSubentry': ['cn=Subschema'],
            'avigilonidentityRoleDN': ident.get('avigilonidentityRoleDN', []),
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
        lines.append(f'<dn>cn={cn},ou=identities,dc=avigilon</dn>')
        lines.append(f'<cns type="array"><cn>{cn}</cn></cns>')
        lines.append(f'<createTimestamp>{ident["createTimestamp"]}</createTimestamp>')
        lines.append(f'<creatorsName>cn=0,ou=identities,dc=avigilon</creatorsName>')
        lines.append(f'<entryDN>cn={cn},ou=identities,dc=avigilon</entryDN>')
        lines.append(f'<entryUUID>{ident["entryUUID"]}</entryUUID>')
        lines.append(f'<hasSubordinates>{ident["hasSubordinates"]}</hasSubordinates>')
        lines.append(f'<modifyTimestamp>{ident["modifyTimestamp"]}</modifyTimestamp>')
        lines.append('<objectClasses type="array"><objectClass>avigilonIdentity</objectClass></objectClasses>')
        if ident['avigilonFname']:
            lines.append(f'<avigilonFname>{ident["avigilonFname"]}</avigilonFname>')
        lines.append(f'<avigilonIdstatus>{ident["avigilonIdstatus"]}</avigilonIdstatus>')
        lines.append(f'<avigilonIssuedate>{ident["avigilonIssuedate"]}</avigilonIssuedate>')
        if ident['avigilonLname']:
            lines.append(f'<avigilonLname>{ident["avigilonLname"]}</avigilonLname>')
        lines.append(f'<avigilonName>{ident["avigilonName"]}</avigilonName>')
        if ident.get('avigilonLogin'):
            lines.append(f'<avigilonLogin>{ident["avigilonLogin"]}</avigilonLogin>')
        lines.append('<avigilonidentityDarkModes type="array"><avigilonidentityDarkMode>default</avigilonidentityDarkMode></avigilonidentityDarkModes>')
        lines.append(f'<avigilonidentityForcedPasswordChange>{ident["avigilonidentityForcedPasswordChange"]}</avigilonidentityForcedPasswordChange>')
        lines.append(f'<avigilonidentityMultifactorAuthentication>{ident["avigilonidentityMultifactorAuthentication"]}</avigilonidentityMultifactorAuthentication>')
        lines.append(f'<avigilonidentityPagetimeout>{ident["avigilonidentityPagetimeout"]}</avigilonidentityPagetimeout>')
        role_dns = ident.get('avigilonidentityRoleDN', [])
        if role_dns:
            lines.append('<avigilonidentityRoleDNs type="array">')
            for rdn in role_dns:
                lines.append(f'<avigilonidentityRoleDN>{rdn}</avigilonidentityRoleDN>')
            lines.append('</avigilonidentityRoleDNs>')
        lines.append('<structuralObjectClass>avigilonIdentity</structuralObjectClass>')
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
                'avigilonInternalnumber': tok['avigilonInternalnumber'],
                'avigilonEmbossednumber': tok['avigilonEmbossednumber'],
                'avigilonTokenlevel': tok['avigilonTokenlevel'],
                'TokenTypeId': int(tok['avigilonTokenType']),
                'avigilonTokenType': tok['avigilonTokenType'],
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
        lines.append(f'<dn>cn={tid},ou=tokens,cn={cn},ou=identities,dc=avigilon</dn>')
        lines.append(f'<cns type="array"><cn>{tid}</cn></cns>')
        lines.append(f'<entryUUID>{uuid.uuid4()}</entryUUID>')
        lines.append('<objectClasses type="array"><objectClass>avigilonToken</objectClass></objectClasses>')
        lines.append(f'<avigilonActivatedate>{tok["avigilonActivatedate"]}</avigilonActivatedate>')
        lines.append(f'<avigilonDeactivatedate>{tok["avigilonDeactivatedate"]}</avigilonDeactivatedate>')
        lines.append(f'<avigilonDownload>{tok["avigilonDownload"]}</avigilonDownload>')
        lines.append(f'<avigilonEmbossednumber>{tok["avigilonEmbossednumber"]}</avigilonEmbossednumber>')
        lines.append(f'<avigilonInternalnumber>{tok["avigilonInternalnumber"]}</avigilonInternalnumber>')
        lines.append(f'<avigilonIssuedate>{tok["avigilonIssuedate"]}</avigilonIssuedate>')
        lines.append(f'<avigilonTokenMobileAppType>{tok["avigilonTokenMobileAppType"]}</avigilonTokenMobileAppType>')
        lines.append(f'<avigilonTokenOrigoMobileIdType>{tok.get("avigilonTokenOrigoMobileIdType", "1")}</avigilonTokenOrigoMobileIdType>')
        lines.append(f'<avigilonTokenType>{tok["avigilonTokenType"]}</avigilonTokenType>')
        lines.append(f'<avigilonTokenUnitofUpdatePeriod>{tok["avigilonTokenUnitofUpdatePeriod"]}</avigilonTokenUnitofUpdatePeriod>')
        lines.append(f'<avigilonTokenlevel>{tok["avigilonTokenlevel"]}</avigilonTokenlevel>')
        lines.append(f'<avigilonTokennoexpire>{tok["avigilonTokennoexpire"]}</avigilonTokennoexpire>')
        lines.append(f'<avigilonTokenstatus>{tok["avigilonTokenstatus"]}</avigilonTokenstatus>')
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
    lname = request.form.get('identity[avigilonLname]', '')
    fname = request.form.get('identity[avigilonFname]', '')
    email = request.form.get('identity[avigilonidentityEmailaddress]', '')
    phone = request.form.get('identity[avigilonidentityPhone]', '')
    workphone = request.form.get('identity[avigilonidentityWorkphone]', '')
    title = request.form.get('identity[avigilonidentityTitle]', '')
    department = request.form.get('identity[avigilonidentityDepartment]', '')
    division = request.form.get('identity[avigilonidentityDivision]', '')
    address = request.form.get('identity[avigilonidentityAddress]', '')
    city = request.form.get('identity[avigilonidentityCity]', '')
    state = request.form.get('identity[avigilonidentityState]', '')
    zipcode = request.form.get('identity[avigilonidentityZipcode]', '')
    status = request.form.get('identity[avigilonIdstatus]', '1')
    typ = request.form.get('identity[avigilonTyp]', 'Employee')

    IDENTITIES[cn] = {
        'cn': cn,
        'avigilonFname': fname,
        'avigilonLname': lname,
        'avigilonName': f"{lname}, {fname}" if fname and lname else lname or fname,
        'avigilonIdstatus': status,
        'avigilonLogin': '',
        'avigilonidentityEmailaddress': email,
        'avigilonidentityPhone': phone,
        'avigilonidentityWorkphone': workphone,
        'avigilonidentityTitle': title,
        'avigilonidentityDepartment': department,
        'avigilonidentityDivision': division,
        'avigilonidentityAddress': address,
        'avigilonidentityCity': city,
        'avigilonidentityState': state,
        'avigilonidentityZipcode': zipcode,
        'avigilonIssuedate': now_iso,
        'avigilonTyp': typ,
        'avigilonidentityForcedPasswordChange': request.form.get('identity[avigilonidentityForcedPasswordChange]', 'TRUE'),
        'avigilonidentityMultifactorAuthentication': 'FALSE',
        'avigilonidentityPagetimeout': request.form.get('identity[avigilonidentityPagetimeout]', '600000'),
        'createTimestamp': now_ldap,
        'modifyTimestamp': now_ldap,
        'structuralObjectClass': 'avigilonIdentity',
        'entryUUID': str(uuid.uuid4()),
        'hasSubordinates': 'FALSE',
        'avigilonidentityRoleDN': [],
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

    internal = request.form.get('token[avigilonInternalnumber]', '')
    embossed = request.form.get('token[avigilonEmbossednumber]', '')

    TOKENS.setdefault(cn, {})[tid] = {
        'cn': tid,
        'avigilonInternalnumber': internal,
        'avigilonEmbossednumber': embossed,
        'avigilonPIN': request.form.get('token[avigilonPIN]', ''),
        'avigilonTokenstatus': request.form.get('token[avigilonTokenstatus]', '1'),
        'avigilonTokenType': request.form.get('token[avigilonTokenType]', '0'),
        'avigilonTokenlevel': request.form.get('token[avigilonTokenlevel]', '0'),
        'avigilonDownload': request.form.get('token[avigilonDownload]', 'TRUE'),
        'avigilonTokenMobileAppType': request.form.get('token[avigilonTokenMobileAppType]', '0'),
        'avigilonTokenOrigoMobileIdType': request.form.get('token[avigilonTokenOrigoMobileIdType]', '1'),
        'avigilonTokenUnitofUpdatePeriod': request.form.get('token[avigilonTokenUnitofUpdatePeriod]', '0'),
        'avigilonTokennoexpire': request.form.get('token[avigilonTokennoexpire]', 'FALSE'),
        'avigilonIssuedate': now_ldap,
        'avigilonActivatedate': request.form.get('avigilonActivatedate', now_ldap),
        'avigilonDeactivatedate': request.form.get('avigilonDeactivatedate', (datetime.now(timezone.utc) + timedelta(days=365)).strftime('%Y%m%d%H%M%SZ')),
        'avigilonTokenEnableReValidation': 'FALSE',
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
        'avigilonTokenstatus', 'avigilonInternalnumber', 'avigilonEmbossednumber',
        'avigilonPIN', 'avigilonTokenType', 'avigilonTokenlevel', 'avigilonDownload',
        'avigilonTokenMobileAppType', 'avigilonTokenOrigoMobileIdType',
        'avigilonTokenUnitofUpdatePeriod', 'avigilonTokennoexpire',
    ]:
        form_key = f'token[{field}]'
        if form_key in request.form:
            tok[field] = request.form[form_key]

    # Update status string to match numeric
    status_map = {'1': 'Active', '2': 'Inactive', '3': 'Not yet active', '4': 'Expired'}
    tok['token_status'] = status_map.get(tok['avigilonTokenstatus'], 'Active')

    for date_field in ['avigilonIssuedate', 'avigilonActivatedate', 'avigilonDeactivatedate']:
        if date_field in request.form:
            tok[date_field] = request.form[date_field]

    tok['modifyTimestamp'] = _now_ldap()
    IDENTITIES[cn]['modifyTimestamp'] = _now_ldap()

    logger.info(f"Updated token: {tid} for identity {cn} (status={tok['avigilonTokenstatus']})")
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
        lines.append(f'<avigilonidentityPrimaryimage type="boolean">{str(photo.get("primary", True)).lower()}</avigilonidentityPrimaryimage>')
        lines.append(f'<avigilonidentityFilename>{photo.get("filename", "photo.jpeg")}</avigilonidentityFilename>')
        lines.append(f'<avigilonidentityContentype>image/jpeg</avigilonidentityContentype>')
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
                'avigilonName': fmt['avigilonName'],
                'avigiloncfmtFacilitycode': fmt['avigiloncfmtFacilitycode'],
                'avigiloncfmtMaxdigits': fmt['avigiloncfmtMaxdigits'],
                'avigiloncfmtFcodelen': fmt['avigiloncfmtFcodelen'],
                'avigiloncfmtCardlen': fmt['avigiloncfmtCardlen'],
                'avigiloncfmtType': fmt['avigiloncfmtType'],
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
    parser = argparse.ArgumentParser(description='Fake Avigilon / Avigilon Server')
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
