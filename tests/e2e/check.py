"""End-to-end checks against a running release, called by tests/e2e/run.sh.

  check.py write MARKER   log in as alice and bob, check roles and LDAP groups,
                          upload MARKER.txt as alice, check companion routes
  check.py read MARKER    log in again and download MARKER.txt unchanged

Environment: OC (OpenCloud base URL), KC (Keycloak base URL). Logins use the
resource-owner password grant of the test realm's web client, so the access
tokens are real Keycloak tokens carrying the uuid and roles claims.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

OC, KC = os.environ['OC'].rstrip('/'), os.environ['KC'].rstrip('/')
USERS = {'alice': 'alice-pass', 'bob': 'bob-pass'}
failures = []


def request(method, url, token=None, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def check(name, condition, detail=''):
    print(f"{'ok  ' if condition else 'FAIL'} {name}{'' if condition else ': ' + str(detail)[:300]}")
    if not condition:
        failures.append(name)


def login(user):
    body = urllib.parse.urlencode({'grant_type': 'password', 'client_id': 'web', 'scope': 'openid',
                                   'username': user, 'password': USERS[user]}).encode()
    status, raw = request('POST', f'{KC}/realms/openCloud/protocol/openid-connect/token', data=body,
                          headers={'Content-Type': 'application/x-www-form-urlencoded'})
    if status != 200:
        sys.exit(f'Keycloak login for {user} failed: {status} {raw[:200]}')
    token = json.loads(raw)['access_token']
    claims = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '=='))
    return token, claims


def graph(token, path):
    status, raw = request('GET', f'{OC}/graph/v1.0/{path}', token)
    return status, (json.loads(raw) if raw and status == 200 else raw)


def identity_checks():
    tokens = {}
    status, raw = request('GET', f'{OC}/graph/v1.0/me')
    check('anonymous request is rejected', status == 401, status)
    status, _ = request('GET', f'{OC}/graph/v1.0/me', 'not-a-token')
    check('invalid token is rejected', status == 401, status)
    for user in USERS:
        token, claims = login(user)
        tokens[user] = token
        check(f'{user}: token has a uuid claim', bool(claims.get('uuid')), claims)
        status, me = graph(token, 'me?$expand=memberOf,appRoleAssignments')
        check(f'{user}: signed in to OpenCloud', status == 200, me)
        if status != 200:
            continue
        check(f'{user}: OpenCloud user ID is the LDAP entryUUID from the uuid claim', me.get('id') == claims.get('uuid'), (me.get('id'), claims.get('uuid')))
        check(f'{user}: display name from LDAP', me.get('displayName') == {'alice': 'Alice Admin', 'bob': 'Bob User'}[user], me.get('displayName'))
        groups = {g.get('displayName') for g in me.get('memberOf', [])}
        check(f'{user}: LDAP group membership', 'engineering' in groups, groups)
        me[f'_claims'] = claims
        tokens[user + '_me'] = me
    # Roles: map the assigned app role IDs to their names.
    status, apps = graph(tokens['alice'], 'applications')
    check('applications are listed', status == 200, apps)
    names = {r['id']: r['displayName'] for app in (apps.get('value', []) if status == 200 else []) for r in app.get('appRoles', [])}
    for user, expected in [('alice', 'Admin'), ('bob', 'User')]:
        me = tokens.get(user + '_me', {})
        roles = {names.get(a.get('appRoleId')) for a in me.get('appRoleAssignments', [])}
        check(f'{user}: role {expected} from the roles claim {me.get("_claims", {}).get("roles")}', roles == {expected}, roles)
    status, groups = graph(tokens['alice'], 'groups')
    check('admin lists LDAP groups', status == 200 and 'engineering' in {g.get('displayName') for g in groups.get('value', [])}, groups)
    status, _ = graph(tokens['bob'], 'users')
    check('non-admin cannot list all users', status in (401, 403), status)
    return tokens


def write(marker):
    tokens = identity_checks()
    content = f'opencloud e2e {marker}\n'.encode()
    status, raw = request('PUT', f'{OC}/remote.php/webdav/{marker}.txt', tokens['alice'], data=content)
    check(f'upload {marker}.txt', status in (201, 204), (status, raw[:200]))
    status, raw = request('GET', f'{OC}/remote.php/webdav/{marker}.txt', tokens['alice'])
    check(f'download {marker}.txt', status == 200 and raw == content, (status, raw[:200]))
    status, raw = request('GET', f'{OC}/remote.php/webdav/{marker}.txt', tokens['bob'])
    check("bob cannot read alice's file", status == 404, status)
    status, _ = request('PROPFIND', f'{OC}/caldav/', tokens['bob'], headers={'Depth': '1'})
    check('CalDAV through the proxy (Radicale)', status == 207, status)
    status, _ = request('GET', f'{OC}/yjs/healthz/ready')
    check('yjs through the proxy', status == 200, status)
    status, raw = request('GET', f'{OC}/config.json')
    check('web config advertises yjs', status == 200 and b'/yjs' in raw, raw[:200])


def read(marker):
    tokens = identity_checks()
    status, raw = request('GET', f'{OC}/remote.php/webdav/{marker}.txt', tokens['alice'])
    check(f'{marker}.txt survived', status == 200 and raw == f'opencloud e2e {marker}\n'.encode(), (status, raw[:200]))


if __name__ == '__main__':
    {'write': write, 'read': read}[sys.argv[1]](sys.argv[2])
    if failures:
        sys.exit(f'{len(failures)} check(s) failed')
