#!/usr/bin/env python3
"""Print a Kubernetes Secret with OpenCloud's service secrets and IDs, for
serviceSecrets.existingSecret.

Run it ONCE per installation and store the result (encrypted, e.g. with SOPS
or Sealed Secrets). Never regenerate it for an existing installation: the IDs
are referenced by stored data.

  generate                 new random values, for a fresh installation
  from-config FILE         copy the values of an initialised installation from
                           its /etc/opencloud/opencloud.yaml (migration)

Examples:
  scripts/service-secrets.py generate --name opencloud-service-secrets > secret.yaml
  kubectl -n opencloud exec opencloud-0 -c opencloud -- cat /etc/opencloud/opencloud.yaml \\
    | scripts/service-secrets.py from-config - --name opencloud-service-secrets > secret.yaml
"""
import argparse
import json
import secrets
import sys
import uuid

# Secret key -> location in opencloud.yaml. Every listed location must hold the
# same value; opencloud init writes the shared service account into each service.
SERVICES_WITH_ACCOUNT = ['graph', 'proxy', 'frontend', 'ocm', 'search', 'sharing', 'storage_users',
                         'notifications', 'userlog', 'auth_service', 'clientlog', 'activitylog']
LOCATIONS = {
    'jwtSecret': [('token_manager', 'jwt_secret')],
    'machineAuthApiKey': [('machine_auth_api_key',)],
    'systemUserApiKey': [('system_user_api_key',)],
    'systemUserId': [('system_user_id',)],
    'transferSecret': [('transfer_secret',)],
    'urlSigningSecret': [('url_signing_secret',)],
    'graphApplicationId': [('graph', 'application', 'id')],
    'serviceAccountId': [(s, 'service_account', 'service_account_id') for s in SERVICES_WITH_ACCOUNT],
    'serviceAccountSecret': [(s, 'service_account', 'service_account_secret') for s in SERVICES_WITH_ACCOUNT],
    'wopiSecret': [('collaboration', 'wopi', 'secret')],
    'thumbnailsTransferToken': [('thumbnails', 'thumbnail', 'transfer_secret')],
    'storageUsersMountId': [('storage_users', 'mount_id'), ('gateway', 'storage_registry', 'storage_users_mount_id')],
}
IDS = {'systemUserId', 'graphApplicationId', 'serviceAccountId', 'storageUsersMountId'}


def generate():
    # secrets.token_urlsafe uses the OS CSPRNG; uuid4 also draws from os.urandom.
    return {key: str(uuid.uuid4()) if key in IDS else secrets.token_urlsafe(32) for key in LOCATIONS}


def from_config(text):
    import yaml  # PyYAML, as used by the chart's tests
    config = yaml.safe_load(text) or {}
    values, problems = {}, []
    for key, paths in LOCATIONS.items():
        found = set()
        for path in paths:
            node = config
            for part in path:
                node = node.get(part) if isinstance(node, dict) else None
            if node not in (None, ''):
                found.add(str(node))
        if len(found) != 1:
            problems.append(f'{key}: expected one value at {", ".join(".".join(p) for p in paths)}, found {len(found)}')
        else:
            values[key] = found.pop()
    ids = config.get('settings', {}).get('service_account_ids') or []
    if values.get('serviceAccountId') and ids and ids != [values['serviceAccountId']]:
        problems.append('settings.service_account_ids differs from the service account ID; migrate manually')
    if problems:
        sys.exit('Refusing to write an incomplete Secret:\n  ' + '\n  '.join(problems))
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['generate', 'from-config'])
    parser.add_argument('file', nargs='?', help='opencloud.yaml for from-config, or - for stdin')
    parser.add_argument('--name', default='opencloud-service-secrets')
    parser.add_argument('--namespace')
    args = parser.parse_args()
    if args.mode == 'generate':
        values = generate()
    else:
        if not args.file:
            parser.error('from-config needs the opencloud.yaml path or -')
        values = from_config(sys.stdin.read() if args.file == '-' else open(args.file).read())
    metadata = {'name': args.name}
    if args.namespace:
        metadata['namespace'] = args.namespace
    # JSON is valid YAML and quotes every value safely.
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': metadata, 'type': 'Opaque', 'stringData': values}
    print(json.dumps(secret, indent=2))
    print(f'Wrote Secret {args.name}: store it encrypted and never regenerate it.', file=sys.stderr)


if __name__ == '__main__':
    main()
