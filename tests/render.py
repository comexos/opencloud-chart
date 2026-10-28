"""Render checks: python3 tests/render.py (requires Helm and PyYAML)."""
from pathlib import Path
import os
import json
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = {'domain': 'example.org', 'admin': {'existingSecret': 'admin'}}


def render(values=None, succeeds=True, raw_defaults=False):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'values.yaml'
        settings = dict(values or {}) if raw_defaults else {**BASE, **(values or {})}
        path.write_text(yaml.safe_dump(settings))
        result = subprocess.run(['helm', 'template', 'opencloud', str(ROOT), '--namespace', 'cloud', '-f', str(path)], capture_output=True, text=True)
    if not succeeds:
        assert result.returncode != 0, 'Expected invalid values to be rejected'
        return result.stderr
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def find(docs, kind, name=None):
    return next(doc for doc in docs if doc['kind'] == kind and (name is None or doc['metadata']['name'] == name))


def workload(docs):
    return find(docs, 'StatefulSet', 'opencloud')


def container(docs):
    return workload(docs)['spec']['template']['spec']['containers'][0]


def env(docs):
    return {entry['name']: entry for entry in container(docs)['env']}


def files(docs):
    return find(docs, 'ConfigMap', 'opencloud-files')['data']


class ChartChecks(unittest.TestCase):
    def test_monitor_namespace_override_is_rejected(self):
        error = render({'metrics': {'enabled': True, 'serviceMonitor': {'enabled': True, 'namespace': 'monitoring'}}}, succeeds=False)
        self.assertIn('namespace', error)

    def test_defaults_require_url_and_admin(self):
        self.assertIn('host or domain is required', render(succeeds=False, raw_defaults=True))
        self.assertIn('admin.existingSecret or admin.password', render({'domain': 'example.org'}, succeeds=False, raw_defaults=True))

    def test_security_and_init(self):
        docs = render()
        pod = workload(docs)['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['securityContext']['runAsUser'], 1000)
        self.assertNotIn('enabled', pod['securityContext'])
        for c in pod['containers'] + pod['initContainers']:
            security = c['securityContext']
            self.assertFalse(security['allowPrivilegeEscalation'])
            self.assertTrue(security['readOnlyRootFilesystem'])
            self.assertEqual(security['capabilities'], {'drop': ['ALL']})
            self.assertIn('requests', c['resources'])
        init = pod['initContainers'][0]
        self.assertEqual(init['image'], pod['containers'][0]['image'])
        self.assertIn('opencloud init --quiet', init['command'][-1])
        self.assertIn('[ -s /etc/opencloud/opencloud.yaml ]', init['command'][-1])
        init_env = {e['name']: e for e in init['env']}
        self.assertEqual(init_env['IDM_ADMIN_PASSWORD']['valueFrom']['secretKeyRef'], {'name': 'admin', 'key': 'adminPassword'})
        self.assertEqual(workload(docs)['spec']['replicas'], 1)

    def test_url_and_probes(self):
        docs = render()
        variables = env(docs)
        self.assertEqual(variables['OC_URL']['value'], 'https://cloud.example.org')
        self.assertEqual(variables['PROXY_TLS']['value'], 'false')
        self.assertEqual(variables['PROXY_DEBUG_ADDR']['value'], '0.0.0.0:9205')
        c = container(docs)
        self.assertEqual(c['startupProbe']['httpGet'], {'path': '/healthz', 'port': 'debug'})
        self.assertEqual(c['readinessProbe']['httpGet'], {'path': '/readyz', 'port': 'debug'})
        self.assertEqual(env(render({'host': 'files.example.net'}))['OC_URL']['value'], 'https://files.example.net')
        self.assertEqual(env(render({'externalUrl': 'https://files.example.net:8443/'}))['OC_URL']['value'], 'https://files.example.net:8443')
        # The built-in idp exits on a non-https issuer.
        self.assertIn('https://', render({'externalUrl': 'http://files.example.net'}, succeeds=False))
        render({'externalUrl': 'http://files.example.net', 'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://idp.example.net/realms/a'}}})

    def test_persistence(self):
        docs = render()
        templates = {t['metadata']['name']: t for t in workload(docs)['spec']['volumeClaimTemplates']}
        self.assertEqual(set(templates), {'config', 'data'})
        self.assertEqual(templates['data']['spec']['resources']['requests']['storage'], '50Gi')
        mounts = {m['mountPath']: m['name'] for m in container(docs)['volumeMounts']}
        self.assertEqual(mounts['/etc/opencloud'], 'config')
        self.assertEqual(mounts['/var/lib/opencloud'], 'data')

        docs = render({'persistence': {'data': {'existingClaim': 'opencloud-data'}}})
        spec = workload(docs)['spec']
        self.assertEqual([t['metadata']['name'] for t in spec['volumeClaimTemplates']], ['config'])
        self.assertIn({'name': 'data', 'persistentVolumeClaim': {'claimName': 'opencloud-data'}}, spec['template']['spec']['volumes'])

        spec = workload(render({'persistence': {'enabled': False}}))['spec']
        self.assertNotIn('volumeClaimTemplates', spec)
        for name in ('config', 'data'):
            self.assertIn({'name': name, 'emptyDir': {}}, spec['template']['spec']['volumes'])

    def test_selectors_do_not_overlap(self):
        docs = render({'yjs': {'enabled': True}, 'radicale': {'enabled': True}, 'tika': {'enabled': True}, 'metrics': {'enabled': True},
                       'podLabels': {'app.kubernetes.io/component': 'wrong', 'team': 'files'}, 'additionalLabels': {'app.kubernetes.io/name': 'wrong'}})
        workloads = [doc for doc in docs if doc['kind'] in ('StatefulSet', 'Deployment')]
        self.assertEqual(len(workloads), 4)
        for item in workloads:
            selector = item['spec']['selector']['matchLabels']
            labels = item['spec']['template']['metadata']['labels']
            for key, value in selector.items():
                self.assertEqual(labels[key], value)
            others = [w for w in workloads if w is not item]
            for other in others:
                other_labels = other['spec']['template']['metadata']['labels']
                self.assertFalse(all(other_labels.get(k) == v for k, v in selector.items()), f"{item['metadata']['name']} selects {other['metadata']['name']}")
        self.assertEqual(workload(docs)['spec']['template']['metadata']['labels']['team'], 'files')
        for service in [doc for doc in docs if doc['kind'] == 'Service']:
            matched = [w for w in workloads if all(w['spec']['template']['metadata']['labels'].get(k) == v for k, v in service['spec']['selector'].items())]
            self.assertEqual(len(matched), 1, service['metadata']['name'])

    def test_builtin_identity(self):
        variables = env(render())
        self.assertNotIn('OC_EXCLUDE_RUN_SERVICES', variables)
        self.assertNotIn('OC_OIDC_ISSUER', variables)
        self.assertEqual(variables['IDM_CREATE_DEMO_USERS']['value'], 'false')

    def test_oidc_with_builtin_idm(self):
        values = {'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://keycloak.example.org/realms/openCloud/', 'audiences': ['web', 'desktop']}}}
        docs = render(values, raw_defaults=False)
        variables = env(docs)
        self.assertEqual(variables['OC_EXCLUDE_RUN_SERVICES']['value'], 'idp')
        self.assertEqual(variables['OC_OIDC_ISSUER']['value'], 'https://keycloak.example.org/realms/openCloud')
        self.assertEqual(variables['PROXY_AUTOPROVISION_ACCOUNTS']['value'], 'true')
        self.assertEqual(variables['PROXY_AUTOPROVISION_CLAIM_USERNAME']['value'], 'sub')
        self.assertEqual(variables['PROXY_OIDC_AUDIENCES']['value'], 'web,desktop')
        self.assertEqual(variables['OC_ADMIN_USER_ID']['value'], '')
        self.assertNotIn('OC_LDAP_URI', variables)
        csp = yaml.safe_load(files(docs)['csp.yaml'])['directives']
        for directive in ('connect-src', 'frame-src', 'script-src'):
            self.assertIn('https://keycloak.example.org/', csp[directive])
        # OIDC needs no local admin password.
        render({'domain': 'example.org', **values}, raw_defaults=True)
        render({'domain': 'example.org', 'identity': {'mode': 'oidc'}}, succeeds=False, raw_defaults=True)

    def test_oidc_with_external_ldap(self):
        ldap = {'uri': 'ldaps://ldap:636', 'bindDn': 'cn=admin,dc=x', 'existingSecret': 'ldap', 'userBaseDn': 'ou=users,dc=x', 'groupBaseDn': 'ou=groups,dc=x'}
        docs = render({'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://idp.example.org/realms/x'}, 'ldap': ldap}, 'excludeServices': ['idp', 'audit']})
        variables = env(docs)
        self.assertEqual(variables['OC_EXCLUDE_RUN_SERVICES']['value'], 'idp,audit,idm')
        self.assertEqual(variables['OC_LDAP_BIND_PASSWORD']['valueFrom']['secretKeyRef'], {'name': 'ldap', 'key': 'bindPassword'})
        self.assertEqual(variables['OC_LDAP_SERVER_WRITE_ENABLED']['value'], 'true')
        self.assertNotIn('GRAPH_LDAP_GROUP_CREATE_BASE_DN', variables)
        # Plain bind password goes through the managed Secret.
        docs = render({'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://idp.example.org/realms/x'}, 'ldap': {**ldap, 'existingSecret': '', 'bindPassword': 'pw'}}})
        self.assertEqual(env(docs)['OC_LDAP_BIND_PASSWORD']['valueFrom']['secretKeyRef'], {'name': 'opencloud-env', 'key': 'ldapBindPassword'})
        self.assertEqual(find(docs, 'Secret', 'opencloud-env')['stringData']['ldapBindPassword'], 'pw')
        for invalid in [
            {'identity': {'ldap': ldap}},  # builtin idp cannot use an external LDAP
            {'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://i/r'}, 'ldap': {**ldap, 'bindDn': ''}}},
            {'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://i/r'}, 'ldap': {**ldap, 'existingSecret': ''}}},
            {'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://i/r'}, 'ldap': {**ldap, 'writeEnabled': False}}},  # autoprovision needs writes
            {'identity': {'mode': 'bogus'}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)
        render({'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://i/r', 'autoprovision': False}, 'ldap': {**ldap, 'writeEnabled': False}}})

    def test_storage_drivers(self):
        self.assertEqual(env(render())['STORAGE_USERS_DRIVER']['value'], 'posix')
        self.assertEqual(env(render({'storage': {'driver': 'decomposed'}}))['STORAGE_USERS_DRIVER']['value'], 'decomposed')
        s3 = {'endpoint': 'https://s3.example.org', 'bucket': 'files', 'existingSecret': 's3'}
        variables = env(render({'storage': {'driver': 'decomposeds3', 's3': s3}}))
        self.assertEqual(variables['STORAGE_SYSTEM_DRIVER']['value'], 'decomposed')
        self.assertEqual(variables['STORAGE_USERS_DECOMPOSEDS3_BUCKET']['value'], 'files')
        self.assertEqual(variables['STORAGE_USERS_DECOMPOSEDS3_ACCESS_KEY']['valueFrom']['secretKeyRef'], {'name': 's3', 'key': 'accessKeyId'})
        docs = render({'storage': {'driver': 'decomposeds3', 's3': {**s3, 'existingSecret': '', 'accessKeyId': 'id', 'secretKey': 'key'}}})
        self.assertEqual(env(docs)['STORAGE_USERS_DECOMPOSEDS3_SECRET_KEY']['valueFrom']['secretKeyRef'], {'name': 'opencloud-env', 'key': 's3SecretKey'})
        self.assertEqual(find(docs, 'Secret', 'opencloud-env')['stringData'], {'s3AccessKeyId': 'id', 's3SecretKey': 'key'})
        for invalid in [
            {'storage': {'driver': 'decomposeds3'}},
            {'storage': {'driver': 'decomposeds3', 's3': {**s3, 'existingSecret': '', 'accessKeyId': 'id'}}},
            {'storage': {'driver': 'ocis'}},
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_feature_services(self):
        self.assertNotIn('OC_ADD_RUN_SERVICES', env(render()))
        docs = render({
            'additionalServices': ['audit'],
            'smtp': {'enabled': True, 'host': 'smtp', 'sender': 'OpenCloud <noreply@example.org>', 'username': 'u', 'existingSecret': 'smtp'},
            'collaboration': {'enabled': True, 'app': {'url': 'https://office.example.org/'}},
            'antivirus': {'enabled': True, 'scanner': 'icap', 'icapUrl': 'icap://icap:1344'},
        })
        variables = env(docs)
        self.assertEqual(variables['OC_ADD_RUN_SERVICES']['value'], 'audit,notifications,collaboration,antivirus')
        self.assertEqual(variables['NOTIFICATIONS_SMTP_PASSWORD']['valueFrom']['secretKeyRef'], {'name': 'smtp', 'key': 'password'})
        self.assertEqual(variables['COLLABORATION_WOPI_SRC']['value'], 'https://cloud.example.org')
        self.assertEqual(variables['COLLABORATION_APP_ADDR']['value'], 'https://office.example.org')
        self.assertEqual(variables['COLLABORATION_APP_ICON']['value'], 'https://office.example.org/favicon.ico')
        self.assertEqual(variables['FRONTEND_APP_HANDLER_SECURE_VIEW_APP_ADDR']['value'], 'eu.opencloud.api.collaboration')
        self.assertIn('aa97fe03-7980-45ac-9e50-b325749fd7e6', variables['GRAPH_AVAILABLE_ROLES']['value'])
        self.assertEqual(variables['POSTPROCESSING_STEPS']['value'], 'virusscan')
        self.assertEqual(variables['ANTIVIRUS_ICAP_URL']['value'], 'icap://icap:1344')
        self.assertNotIn('ANTIVIRUS_CLAMAV_SOCKET', variables)
        # The antivirus service refuses to start with any mode but partial or skip.
        self.assertEqual(variables['ANTIVIRUS_MAX_SCAN_SIZE_MODE']['value'], 'partial')
        csp = yaml.safe_load(files(docs)['csp.yaml'])['directives']
        self.assertIn('https://office.example.org/', csp['frame-src'])
        self.assertIn('https://office.example.org/', csp['img-src'])
        # Euro-Office: no secure view, proof keys off, custom registry mounted.
        docs = render({'collaboration': {'enabled': True, 'app': {'name': 'Euro-Office', 'product': 'OnlyOffice', 'url': 'https://eo.example.org', 'proofDisable': True}, 'appRegistry': 'app_registry:\n  mimetypes: []\n'}})
        variables = env(docs)
        self.assertNotIn('FRONTEND_APP_HANDLER_SECURE_VIEW_APP_ADDR', variables)
        self.assertEqual(variables['COLLABORATION_APP_PROOF_DISABLE']['value'], 'true')
        self.assertIn('app-registry.yaml', files(docs))
        self.assertIn('/etc/opencloud/app-registry.yaml', [m['mountPath'] for m in container(docs)['volumeMounts']])
        for invalid in [
            {'smtp': {'enabled': True, 'host': 'smtp'}},  # the notifications service exits without a sender
            {'smtp': {'enabled': True, 'host': 'smtp', 'sender': 'a@b.c', 'username': 'u'}},
            {'collaboration': {'enabled': True}},
            {'antivirus': {'enabled': True}},
            {'antivirus': {'enabled': True, 'scanner': 'icap'}},
            {'antivirus': {'enabled': True, 'clamavSocket': 'tcp://c:3310', 'maxScanSizeMode': 'truncate'}},  # documented upstream, rejected by the code
        ]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_extra_env_overrides_chart_env(self):
        docs = render({'extraEnv': {'PROXY_TLS': True, 'CUSTOM': 'x'}, 'extraEnvVars': [{'name': 'OC_LOG_LEVEL', 'valueFrom': {'secretKeyRef': {'name': 's', 'key': 'k'}}}]})
        entries = container(docs)['env']
        names = [entry['name'] for entry in entries]
        self.assertEqual(len(names), len(set(names)))
        variables = env(docs)
        self.assertEqual(variables['PROXY_TLS'], {'name': 'PROXY_TLS', 'value': 'true'})
        self.assertEqual(variables['CUSTOM']['value'], 'x')
        self.assertEqual(variables['OC_LOG_LEVEL'], {'name': 'OC_LOG_LEVEL', 'valueFrom': {'secretKeyRef': {'name': 's', 'key': 'k'}}})

    def test_extra_env_vars_take_precedence_and_keep_order(self):
        overrides = [
            {'name': 'OC_LOG_LEVEL', 'valueFrom': {'secretKeyRef': {'name': 's', 'key': 'k'}}},
            {'name': 'CUSTOM', 'value': 'chosen'},
            {'name': 'DEPENDENT', 'value': '$(CUSTOM)'},
        ]
        docs = render({'extraEnv': {'OC_LOG_LEVEL': 'debug', 'CUSTOM': 'ignored'}, 'extraEnvVars': overrides})
        entries = container(docs)['env']
        names = [entry['name'] for entry in entries]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(entries[-3:], overrides)

    def test_duplicate_extra_env_vars_rejected(self):
        error = render({'extraEnvVars': [{'name': 'CUSTOM', 'value': 'one'},
                                        {'name': 'CUSTOM', 'valueFrom': {'secretKeyRef': {'name': 's', 'key': 'k'}}}]}, succeeds=False)
        self.assertIn('extraEnvVars contains duplicate variable "CUSTOM"', error)

    def test_secrets_and_env_from(self):
        docs = render()
        self.assertFalse(any(doc['kind'] == 'Secret' for doc in docs))
        self.assertNotIn('envFrom', container(docs))
        docs = render({'admin': {'password': 'pw'}, 'extraSecretEnv': {'OC_SECRET': 's'}, 'existingSecret': 'more'})
        secret = find(docs, 'Secret', 'opencloud-env')
        self.assertEqual(secret['stringData'], {'adminPassword': 'pw', 'OC_SECRET': 's'})
        self.assertEqual(container(docs)['envFrom'], [{'secretRef': {'name': 'opencloud-env'}}, {'secretRef': {'name': 'more'}}])
        annotations = workload(docs)['spec']['template']['metadata']['annotations']
        self.assertNotEqual(annotations['checksum/secret'], workload(render())['spec']['template']['metadata']['annotations']['checksum/secret'])

    def test_config_files(self):
        docs = render({'yjs': {'enabled': False}})
        self.assertEqual(set(files(docs)), {'csp.yaml'})
        csp = yaml.safe_load(files(docs)['csp.yaml'])['directives']
        self.assertEqual(csp['default-src'], ["'none'"])
        self.assertEqual(env(docs)['PROXY_CSP_CONFIG_FILE_LOCATION']['value'], '/etc/opencloud/csp.yaml')
        docs = render({'csp': {'extraDirectives': {'frame-src': ['https://embed.diagrams.net/', 'https://maps.example.org/'], 'report-uri': ['https://r']}},
                       'passwordPolicy': {'bannedPasswords': ['password', 'secret']}, 'web': {'appsConfig': {'maps': {'config': {'folderViewEnabled': False}}}}})
        data = files(docs)
        csp = yaml.safe_load(data['csp.yaml'])['directives']
        self.assertEqual(csp['frame-src'].count('https://embed.diagrams.net/'), 1)
        self.assertIn('https://maps.example.org/', csp['frame-src'])
        self.assertEqual(csp['report-uri'], ['https://r'])
        self.assertEqual(data['banned-password-list.txt'].split(), ['password', 'secret'])
        self.assertEqual(env(docs)['OC_PASSWORD_POLICY_BANNED_PASSWORDS_LIST']['value'], '/etc/opencloud/banned-password-list.txt')
        self.assertEqual(yaml.safe_load(data['apps.yaml']), {'maps': {'config': {'folderViewEnabled': False}}})
        mounts = {m['mountPath']: m for m in container(docs)['volumeMounts'] if m['name'] == 'files'}
        self.assertEqual(set(mounts), {'/etc/opencloud/csp.yaml', '/etc/opencloud/proxy.yaml', '/etc/opencloud/banned-password-list.txt', '/etc/opencloud/apps.yaml'})
        self.assertTrue(all(m['readOnly'] for m in mounts.values()))
        self.assertEqual(files(render({'csp': {'override': 'directives: {}\n'}}))['csp.yaml'], 'directives: {}\n')

    def test_companions_and_proxy_routes(self):
        # yjs is on by default; Radicale and Tika are opt-in.
        docs = render()
        self.assertEqual([doc['metadata']['name'] for doc in docs if doc['kind'] == 'Deployment'], ['opencloud-yjs'])
        self.assertNotIn('opencloud-radicale', [doc['metadata']['name'] for doc in docs])
        self.assertEqual(env(docs)['WEB_OPTION_YJS_SERVER_URL']['value'], 'wss://cloud.example.org/yjs')
        docs = render({'yjs': {'enabled': False}})
        self.assertFalse(any(doc['kind'] == 'Deployment' for doc in docs))
        self.assertNotIn('WEB_OPTION_YJS_SERVER_URL', env(docs))
        self.assertNotIn('proxy.yaml', files(docs))
        docs = render({'yjs': {'enabled': True}, 'radicale': {'enabled': True}, 'tika': {'enabled': True},
                       'proxy': {'additionalRoutes': [{'endpoint': '/app/', 'backend': 'http://app:8080'}]}})
        routes = yaml.safe_load(files(docs)['proxy.yaml'])['additional_policies'][0]['routes']
        by_endpoint = {route['endpoint']: route for route in routes}
        self.assertEqual(by_endpoint['/yjs'], {'endpoint': '/yjs', 'backend': 'http://opencloud-yjs:1234', 'unprotected': True})
        for endpoint, script in [('/caldav/', '/caldav'), ('/.well-known/caldav', '/caldav'), ('/carddav/', '/carddav'), ('/.well-known/carddav', '/carddav')]:
            self.assertEqual(by_endpoint[endpoint]['backend'], 'http://opencloud-radicale:5232')
            self.assertEqual(by_endpoint[endpoint]['remote_user_header'], 'X-Remote-User')
            self.assertEqual(by_endpoint[endpoint]['additional_headers'], [{'X-Script-Name': script}])
        self.assertIn('/app/', by_endpoint)
        variables = env(docs)
        self.assertEqual(variables['WEB_OPTION_YJS_SERVER_URL']['value'], 'wss://cloud.example.org/yjs')
        self.assertEqual(variables['SEARCH_EXTRACTOR_TIKA_TIKA_URL']['value'], 'http://opencloud-tika:9998')
        self.assertEqual(variables['FRONTEND_FULL_TEXT_SEARCH_ENABLED']['value'], 'true')
        yjs = find(docs, 'Deployment', 'opencloud-yjs')['spec']['template']['spec']['containers'][0]
        self.assertEqual({e['name']: e['value'] for e in yjs['env']}['OPENCLOUD_URL'], 'http://opencloud:9200')
        radicale = find(docs, 'StatefulSet', 'opencloud-radicale')
        self.assertIn('type = http_x_remote_user', find(docs, 'ConfigMap', 'opencloud-radicale')['data']['config'])
        self.assertEqual(radicale['spec']['volumeClaimTemplates'][0]['spec']['resources']['requests']['storage'], '5Gi')
        for name, port in [('opencloud-yjs', 1234), ('opencloud-radicale', 5232), ('opencloud-tika', 9998)]:
            self.assertEqual(find(docs, 'Service', name)['spec']['ports'][0]['port'], port)
        # External Tika: no deployment, URL passed through.
        docs = render({'tika': {'externalUrl': 'http://tika.search:9998'}})
        self.assertNotIn('opencloud-tika', [doc['metadata']['name'] for doc in docs])
        self.assertEqual(env(docs)['SEARCH_EXTRACTOR_TIKA_TIKA_URL']['value'], 'http://tika.search:9998')
        render({'tika': {'enabled': True, 'externalUrl': 'http://tika:9998'}}, succeeds=False)

    def test_yjs_upgrade_preserves_single_server(self):
        spec = find(render(), 'Deployment', 'opencloud-yjs')['spec']
        self.assertEqual(spec['replicas'], 1)
        self.assertEqual(spec['strategy'], {'type': 'Recreate'})

    def test_radicale_only_accepts_its_opencloud_server(self):
        self.assertFalse(any(d['kind'] == 'NetworkPolicy' for d in render()))
        docs = render({'radicale': {'enabled': True}, 'tika': {'enabled': True},
                       'fullnameOverride': 'files',
                       'additionalLabels': {'app.kubernetes.io/component': 'override'}})
        policy = find(docs, 'NetworkPolicy', 'files-radicale')
        self.assertEqual(policy['metadata']['namespace'], 'cloud')
        spec = policy['spec']
        self.assertEqual(spec['policyTypes'], ['Ingress'])
        self.assertEqual(len(spec['ingress']), 1)
        rule = spec['ingress'][0]
        self.assertEqual(rule['ports'], [{'protocol': 'TCP', 'port': 5232}])
        self.assertEqual(len(rule['from']), 1)
        # No namespaceSelector or ipBlock: only pods in this namespace qualify.
        self.assertEqual(set(rule['from'][0]), {'podSelector'})
        allowed = rule['from'][0]['podSelector']['matchLabels']
        target = spec['podSelector']['matchLabels']
        for doc in docs:
            if doc['kind'] not in ('Deployment', 'StatefulSet'):
                continue
            labels = doc['spec']['template']['metadata']['labels']
            matches = lambda selector: all(labels.get(k) == v for k, v in selector.items())
            self.assertEqual(matches(allowed), doc['metadata']['name'] == 'files')
            self.assertEqual(matches(target), doc['metadata']['name'] == 'files-radicale')
        self.assertEqual(allowed['app.kubernetes.io/instance'], 'opencloud')

    def test_metrics(self):
        docs = render()
        self.assertFalse(any(doc['kind'] == 'ServiceMonitor' for doc in docs))
        self.assertNotIn('opencloud-metrics', [doc['metadata']['name'] for doc in docs if doc['kind'] == 'Service'])
        self.assertNotIn('PROXY_DEBUG_TOKEN', env(docs))
        docs = render({'metrics': {'enabled': True, 'token': {'value': 'tok'}, 'serviceMonitor': {'enabled': True, 'interval': '15s'}}})
        service = find(docs, 'Service', 'opencloud-metrics')
        monitor = find(docs, 'ServiceMonitor')
        self.assertEqual(monitor['metadata']['namespace'], 'cloud')
        self.assertEqual(service['spec']['ports'][0]['port'], 9205)
        for key, value in monitor['spec']['selector']['matchLabels'].items():
            self.assertEqual(service['metadata']['labels'][key], value)
        self.assertNotEqual(find(docs, 'Service', 'opencloud')['metadata']['labels']['app.kubernetes.io/component'], monitor['spec']['selector']['matchLabels']['app.kubernetes.io/component'])
        endpoint = monitor['spec']['endpoints'][0]
        self.assertEqual((endpoint['port'], endpoint['path'], endpoint['interval']), ('debug', '/metrics', '15s'))
        self.assertEqual(endpoint['authorization']['credentials'], {'name': 'opencloud-env', 'key': 'metricsToken'})
        self.assertEqual(env(docs)['PROXY_DEBUG_TOKEN']['valueFrom']['secretKeyRef'], {'name': 'opencloud-env', 'key': 'metricsToken'})
        render({'metrics': {'serviceMonitor': {'enabled': True}}}, succeeds=False)

    def test_routing(self):
        docs = render({'ingress': {'enabled': True, 'className': 'nginx', 'tls': [{'secretName': 'tls', 'hosts': ['cloud.example.org']}]}})
        ingress = find(docs, 'Ingress')
        rule = ingress['spec']['rules'][0]
        self.assertEqual(rule['host'], 'cloud.example.org')
        self.assertEqual(rule['http']['paths'][0]['backend']['service'], {'name': 'opencloud', 'port': {'name': 'http'}})
        docs = render({'gatewayAPI': {'httpRoute': {'enabled': True, 'parentRefs': [{'name': 'web', 'namespace': 'infra'}]}}})
        route = find(docs, 'HTTPRoute')
        self.assertEqual(route['spec']['hostnames'], ['cloud.example.org'])
        self.assertEqual(route['spec']['rules'][0]['backendRefs'][0], {'name': 'opencloud', 'port': 9200})
        render({'gatewayAPI': {'httpRoute': {'enabled': True}}}, succeeds=False)

    def test_dual_stack_service(self):
        docs = render({'service': {'ipFamilyPolicy': 'PreferDualStack', 'ipFamilies': ['IPv4', 'IPv6']}})
        spec = find(docs, 'Service', 'opencloud')['spec']
        self.assertEqual((spec['ipFamilyPolicy'], spec['ipFamilies']), ('PreferDualStack', ['IPv4', 'IPv6']))
        self.assertNotIn('ipFamilyPolicy', find(docs, 'Service', 'opencloud-headless')['spec'])
        for invalid in [{'service': {'ipFamilies': ['IPv4', 'IPv6']}}, {'service': {'ipFamilyPolicy': 'Bogus'}}]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_images_and_pull_secrets(self):
        docs = render({'global': {'imageRegistry': 'mirror.example', 'imagePullSecrets': ['registry']}, 'image': {'digest': 'sha256:' + 'a' * 64}, 'yjs': {'enabled': True}})
        pod = workload(docs)['spec']['template']['spec']
        self.assertEqual(pod['containers'][0]['image'], 'mirror.example/opencloudeu/opencloud-rolling@sha256:' + 'a' * 64)
        self.assertEqual(pod['imagePullSecrets'], [{'name': 'registry'}])
        self.assertTrue(find(docs, 'Deployment', 'opencloud-yjs')['spec']['template']['spec']['containers'][0]['image'].startswith('mirror.example/opencloudeu/yjs@sha256:'))
        self.assertEqual(container(render({'image': {'digest': '', 'tag': '7.2.4', 'repository': 'opencloudeu/opencloud'}}))['image'], 'docker.io/opencloudeu/opencloud:7.2.4')
        render({'image': {'digest': 'latest'}}, succeeds=False)

    def test_disabled_options(self):
        docs = render({'serviceAccount': {'create': False, 'name': 'existing'}, 'podSecurityContext': {'enabled': False}, 'containerSecurityContext': {'enabled': False},
                       'startupProbe': {'enabled': False}, 'livenessProbe': {'enabled': False}, 'readinessProbe': {'enabled': False},
                       'hostAliases': [{'ip': '10.0.0.1', 'hostnames': ['cloud.example.org']}]})
        pod = workload(docs)['spec']['template']['spec']
        self.assertEqual(pod['serviceAccountName'], 'existing')
        self.assertNotIn('ServiceAccount', [doc['kind'] for doc in docs])
        self.assertNotIn('securityContext', pod)
        self.assertEqual(pod['hostAliases'], [{'ip': '10.0.0.1', 'hostnames': ['cloud.example.org']}])
        for field in ['securityContext', 'startupProbe', 'livenessProbe', 'readinessProbe']:
            self.assertNotIn(field, pod['containers'][0])

    EXTERNAL = {
        'serviceSecrets': {'existingSecret': 'service'},
        'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://kc.example.org/realms/oc', 'autoprovision': False},
                     'ldap': {'uri': 'ldaps://ldap:636', 'bindDn': 'cn=b', 'existingSecret': 'ldap', 'userBaseDn': 'ou=u', 'groupBaseDn': 'ou=g', 'writeEnabled': False}},
    }

    def external(self, extra=None, succeeds=True):
        return render({'domain': 'example.org', **self.EXTERNAL, **(extra or {})}, succeeds=succeeds, raw_defaults=True)

    def test_external_service_secrets(self):
        docs = self.external()
        pod = workload(docs)['spec']['template']['spec']
        self.assertNotIn('initContainers', pod)
        variables = env(docs)
        expected = {
            'OC_JWT_SECRET': 'jwtSecret', 'OC_MACHINE_AUTH_API_KEY': 'machineAuthApiKey', 'OC_SYSTEM_USER_API_KEY': 'systemUserApiKey',
            'OC_SYSTEM_USER_ID': 'systemUserId', 'OC_TRANSFER_SECRET': 'transferSecret', 'OC_URL_SIGNING_SECRET': 'urlSigningSecret',
            'GRAPH_APPLICATION_ID': 'graphApplicationId', 'OC_SERVICE_ACCOUNT_ID': 'serviceAccountId', 'OC_SERVICE_ACCOUNT_SECRET': 'serviceAccountSecret',
            'COLLABORATION_WOPI_SECRET': 'wopiSecret',
            'STORAGE_USERS_MOUNT_ID': 'storageUsersMountId', 'GATEWAY_STORAGE_USERS_MOUNT_ID': 'storageUsersMountId',
        }
        for name, key in expected.items():
            self.assertEqual(variables[name]['valueFrom']['secretKeyRef'], {'name': 'service', 'key': key}, name)
        self.assertNotIn('IDM_ADMIN_PASSWORD', variables)
        # The keys match what scripts/service-secrets.py writes.
        script = subprocess.run(['python3', '-I', str(ROOT / 'scripts/service-secrets.py'), 'generate'], capture_output=True, text=True, check=True)
        self.assertEqual(set(yaml.safe_load(script.stdout)['stringData']), set(expected.values()))
        # Default layout keeps the config claim, so existing releases can switch without touching volumeClaimTemplates.
        self.assertEqual([t['metadata']['name'] for t in workload(docs)['spec']['volumeClaimTemplates']], ['config', 'data'])
        spec = workload(self.external({'persistence': {'config': {'enabled': False}}}))['spec']
        self.assertEqual([t['metadata']['name'] for t in spec['volumeClaimTemplates']], ['data'])
        self.assertIn({'name': 'config', 'emptyDir': {}}, spec['template']['spec']['volumes'])
        # Config files still mount read-only into the emptyDir.
        mounts = {m['mountPath'] for m in spec['template']['spec']['containers'][0]['volumeMounts']}
        self.assertTrue({'/etc/opencloud', '/etc/opencloud/csp.yaml', '/etc/opencloud/proxy.yaml'} <= mounts)
        # Extra init containers still render without the generated one.
        extra = workload(self.external({'extraInitContainers': [{'name': 'ext', 'image': 'busybox@sha256:' + 'b' * 64}]}))['spec']['template']['spec']
        self.assertEqual([c['name'] for c in extra['initContainers']], ['ext'])

    def test_external_service_secrets_rejected_combinations(self):
        for values in [
            {'domain': 'example.org', 'serviceSecrets': {'existingSecret': 's'}, 'admin': {'existingSecret': 'a'}},  # builtin identity
            {'domain': 'example.org', 'serviceSecrets': {'existingSecret': 's'}, 'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://i/r'}}},  # built-in idm
            {'domain': 'example.org', **self.EXTERNAL, 'admin': {'password': 'x'}},
            {'domain': 'example.org', 'admin': {'existingSecret': 'a'}, 'persistence': {'config': {'enabled': False}}},  # generated secrets need a PVC
        ]:
            with self.subTest(values=values):
                self.assertIn('serviceSecrets', render(values, succeeds=False, raw_defaults=True))

    SPLIT = {'topology': 'split', 'nats': {'address': 'nats.nats:4222', 'existingSecret': 'nats'}}

    def split(self, extra=None, succeeds=True):
        return self.external({**self.SPLIT, **(extra or {})}, succeeds)

    def test_split_layout(self):
        docs = self.split({'smtp': {'enabled': True, 'host': 'smtp', 'sender': 'a@b'}, 'extraEnv': {'OC_LOG_LEVEL': 'debug'}})
        storage, frontend = workload(docs), find(docs, 'Deployment', 'opencloud-frontend')
        roles = {}
        for name, w in (('storage', storage), ('frontend', frontend)):
            c = w['spec']['template']['spec']['containers'][0]
            roles[name] = ({e['name']: e for e in c['env']}, c, w['spec']['template']['spec'])
        storage_env, storage_c, storage_pod = roles['storage']
        frontend_env, frontend_c, frontend_pod = roles['frontend']
        # The storage pod runs only the services with state on the data volume; the frontend everything else.
        self.assertEqual(storage_env['OC_RUN_SERVICES']['value'], 'storage-users,storage-system,search,ocm,auth-guest')
        self.assertNotIn('OC_EXCLUDE_RUN_SERVICES', storage_env)
        self.assertEqual(set(frontend_env['OC_EXCLUDE_RUN_SERVICES']['value'].split(',')),
                         {'idp', 'idm', 'nats', 'storage-users', 'storage-system', 'search', 'ocm', 'auth-guest'})
        self.assertEqual(frontend_env['OC_ADD_RUN_SERVICES']['value'], 'notifications')
        self.assertNotIn('OC_RUN_SERVICES', frontend_env)
        for variables in (storage_env, frontend_env):
            self.assertEqual(variables['MICRO_REGISTRY']['value'], 'nats-js-kv')
            for name in ('MICRO_REGISTRY_ADDRESS', 'OC_EVENTS_ENDPOINT', 'OC_CACHE_STORE_NODES', 'OC_PERSISTENT_STORE_NODES'):
                self.assertEqual(variables[name]['value'], 'nats.nats:4222', name)
            for prefix in ('MICRO_REGISTRY_AUTH', 'OC_EVENTS_AUTH', 'OC_CACHE_AUTH', 'OC_PERSISTENT_STORE_AUTH'):
                self.assertEqual(variables[prefix + '_PASSWORD']['valueFrom']['secretKeyRef'], {'name': 'nats', 'key': 'password'})
            # Every service listens on the pod IP; the gateway's bind and client addresses are set separately.
            self.assertEqual(variables['GATEWAY_GRPC_ADDR']['value'], '0.0.0.0:9142')
            self.assertEqual(variables['STORAGE_USERS_GRPC_ADDR']['value'], '0.0.0.0:9157')
            self.assertNotIn('OC_GATEWAY_GRPC_ADDR', variables)
            self.assertEqual(variables['STORAGE_USERS_GATEWAY_GRPC_ADDR']['value'], 'eu.opencloud.api.gateway')
            self.assertEqual(variables['STORAGE_USERS_DATA_SERVER_URL']['value'], 'http://opencloud-headless.cloud.svc.cluster.local:9158/data')
            # The startup script formats the pod address for thumbnail URLs.
            self.assertEqual(variables['POD_IP']['valueFrom'], {'fieldRef': {'fieldPath': 'status.podIP'}})
            self.assertNotIn('THUMBNAILS_DATA_ENDPOINT', variables)
            self.assertNotIn('WEBDAV_THUMBNAILS_GENERATOR_URL', variables)
            self.assertEqual(variables['OC_LOG_LEVEL']['value'], 'debug')
            self.assertEqual(variables['OC_JWT_SECRET']['valueFrom']['secretKeyRef']['name'], 'service')
        # Probes use each role's debug port: the user storage's, or the proxy's.
        self.assertEqual({p['name']: p['containerPort'] for p in storage_c['ports']}, {'users-data': 9158, 'system-data': 9216, 'ocm-data': 9280, 'debug': 9159})
        self.assertEqual(storage_env['STORAGE_USERS_DEBUG_ADDR']['value'], '0.0.0.0:9159')
        self.assertEqual({p['name']: p['containerPort'] for p in frontend_c['ports']}, {'http': 9200, 'debug': 9205})
        # Only the storage pod has the data volume; the frontend keeps caches in emptyDirs.
        self.assertEqual([t['metadata']['name'] for t in storage['spec']['volumeClaimTemplates']], ['config', 'data'])
        self.assertTrue(all('persistentVolumeClaim' not in v for v in frontend_pod['volumes']))
        self.assertIn({'name': 'data', 'emptyDir': {}}, frontend_pod['volumes'])
        self.assertEqual(storage['spec']['replicas'], 1)
        self.assertEqual(frontend['spec']['template']['metadata']['labels']['app.kubernetes.io/component'], 'frontend')
        # The roles need each other at startup (the storage services connect to the gateway), so
        # neither waits for the other; the storage pod's DNS name exists before it is ready.
        self.assertNotIn('initContainers', storage_pod)
        self.assertNotIn('initContainers', frontend_pod)
        self.assertTrue(find(docs, 'Service', 'opencloud-headless')['spec']['publishNotReadyAddresses'])
        # Traffic, metrics and the companions' policy follow the proxy into the frontend.
        selector = lambda name: find(docs, 'Service', name)['spec']['selector']['app.kubernetes.io/component']
        self.assertEqual((selector('opencloud'), selector('opencloud-headless')), ('frontend', 'server'))
        self.assertEqual([p['port'] for p in find(docs, 'Service', 'opencloud-headless')['spec']['ports']], [9158, 9216, 9280])
        docs = self.split({'radicale': {'enabled': True}, 'metrics': {'enabled': True}})
        self.assertEqual(find(docs, 'Service', 'opencloud-metrics')['spec']['selector']['app.kubernetes.io/component'], 'frontend')
        peer = find(docs, 'NetworkPolicy')['spec']['ingress'][0]['from'][0]['podSelector']['matchLabels']
        self.assertEqual(peer['app.kubernetes.io/component'], 'frontend')
        # Without credentials NATS is reached anonymously.
        variables = {e['name'] for e in container(self.split({'nats': {'address': 'nats:4222'}}))['env']}
        self.assertFalse(any(name.endswith(('_AUTH_USERNAME', '_AUTH_PASSWORD')) for name in variables))

    def test_split_startup_addresses_and_overrides(self):
        for override in (False, True):
            extra = {'extraEnvVars': [{'name': 'GATEWAY_GRPC_ADDR', 'value': 'custom:9142'},
                                     {'name': 'WEBDAV_THUMBNAILS_GENERATOR_URL', 'value': 'https://custom'}]} if override else {}
            docs = self.split(extra)
            containers = [container(docs), find(docs, 'Deployment', 'opencloud-frontend')['spec']['template']['spec']['containers'][0]]
            for c in containers:
                for ip, host in [('10.0.0.1', '10.0.0.1'), ('fd00::12', '[fd00::12]')]:
                    with self.subTest(override=override, role=c['name'], ip=ip), tempfile.TemporaryDirectory() as directory:
                        fake = Path(directory) / 'opencloud'
                        fake.write_text('#!/usr/bin/env python3\nimport os, json\nprint(json.dumps(dict(os.environ)))\n')
                        fake.chmod(0o755)
                        env = {**os.environ, **{e['name']: e['value'] for e in c['env'] if 'value' in e},
                               'POD_IP': ip, 'PATH': directory + os.pathsep + os.environ['PATH']}
                        result = subprocess.run(c['command'], env=env, capture_output=True, text=True, check=True)
                        actual = json.loads(result.stdout)
                        self.assertEqual(actual['WEBDAV_THUMBNAILS_GENERATOR_URL'], 'https://custom' if override else f'http://{host}:9186')
                        self.assertEqual(actual['GATEWAY_GRPC_ADDR'], 'custom:9142' if override else (f'{host}:9142' if ':' in ip else '0.0.0.0:9142'))
                        self.assertEqual(actual['PROXY_HTTP_ADDR'], f'{host}:9200' if ':' in ip else '0.0.0.0:9200')
                        self.assertEqual(actual['STORAGE_USERS_DEBUG_ADDR'], f'{host}:9159' if ':' in ip else '0.0.0.0:9159')

    def test_split_cluster_domain(self):
        # The storage pod's data endpoints follow the cluster's DNS domain.
        variables = {e['name']: e.get('value') for e in container(self.split({'clusterDomain': 'k8s.example'}))['env']}
        frontend = find(self.split({'clusterDomain': 'k8s.example'}), 'Deployment', 'opencloud-frontend')
        frontend_env = {e['name']: e.get('value') for e in frontend['spec']['template']['spec']['containers'][0]['env']}
        for env_vars in (variables, frontend_env):
            self.assertEqual(env_vars['STORAGE_USERS_DATA_SERVER_URL'], 'http://opencloud-headless.cloud.svc.k8s.example:9158/data')
            self.assertEqual(env_vars['STORAGE_SYSTEM_DATA_SERVER_URL'], 'http://opencloud-headless.cloud.svc.k8s.example:9216/data')
            self.assertEqual(env_vars['OCM_OCM_STORAGE_DATA_SERVER_URL'], 'http://opencloud-headless.cloud.svc.k8s.example:9280/data')

    def test_split_metrics_cover_the_storage_pod(self):
        metrics = {'metrics': {'enabled': True, 'serviceMonitor': {'enabled': True}, 'token': {'value': 'secret'}}}
        docs = self.split(metrics)
        services = {d['metadata']['name']: d for d in docs if d['kind'] == 'Service' and 'metrics' in d['metadata']['name']}
        self.assertEqual(set(services), {'opencloud-metrics', 'opencloud-metrics-storage'})
        storage = services['opencloud-metrics-storage']
        self.assertEqual(storage['spec']['selector']['app.kubernetes.io/component'], 'server')
        self.assertEqual(storage['spec']['ports'], [{'name': 'debug', 'port': 9159, 'targetPort': 'debug'}])
        # The ServiceMonitor selects both Services by their shared component label.
        selector = find(docs, 'ServiceMonitor')['spec']['selector']['matchLabels']
        for service in services.values():
            self.assertTrue(all(service['metadata']['labels'].get(k) == v for k, v in selector.items()))
        # The storage pod's debug server protects /metrics with the same token as the proxy.
        storage_env = {e['name']: e for e in container(docs)['env']}
        self.assertEqual(storage_env['STORAGE_USERS_DEBUG_TOKEN']['valueFrom'], storage_env['PROXY_DEBUG_TOKEN']['valueFrom'])
        # Topology single exposes only the proxy's debug port.
        single = render(metrics)
        self.assertEqual([d['metadata']['name'] for d in single if d['kind'] == 'Service' and 'metrics' in d['metadata']['name']],
                         ['opencloud-metrics'])
        self.assertNotIn('STORAGE_USERS_DEBUG_TOKEN', env(single))

    SHARED = {'storagePods': {'replicas': 2}, 'persistence': {'data': {'shared': True, 'accessMode': 'ReadWriteMany', 'storageClass': 'nfs'}}}

    def test_split_shared_storage(self):
        docs = self.split(self.SHARED)
        storage, frontend = workload(docs), find(docs, 'Deployment', 'opencloud-frontend')
        self.assertEqual(storage['spec']['replicas'], 2)
        # One shared claim instead of a claim per pod, kept on uninstall.
        self.assertNotIn('data', [t['metadata']['name'] for t in storage['spec'].get('volumeClaimTemplates', [])])
        claim = find(docs, 'PersistentVolumeClaim', 'opencloud-data')
        self.assertEqual(claim['spec']['accessModes'], ['ReadWriteMany'])
        self.assertEqual(claim['spec']['storageClassName'], 'nfs')
        self.assertEqual(claim['metadata']['annotations']['helm.sh/resource-policy'], 'keep')
        volumes = {v['name']: v for v in storage['spec']['template']['spec']['volumes']}
        self.assertEqual(volumes['data'], {'name': 'data', 'persistentVolumeClaim': {'claimName': 'opencloud-data'}})
        # search, ocm and auth-guest run on the first pod only; the wrapper adds them there.
        c = storage['spec']['template']['spec']['containers'][0]
        variables = {e['name']: e.get('value') for e in c['env']}
        self.assertEqual(variables['OC_RUN_SERVICES'], 'storage-users,storage-system')
        self.assertEqual(variables['OC_SINGLETON_SERVICES'], 'search,ocm,auth-guest')
        self.assertNotIn('args', c)
        self.assertEqual(c['command'][:2], ['/bin/sh', '-ec'])
        self.assertIn('${HOSTNAME##*-}', c['command'][2])
        self.assertIn('exec opencloud server', c['command'][2])
        # The OCM data endpoint names the first pod; the storages stay load-balanced.
        frontend_env = {e['name']: e.get('value') for e in frontend['spec']['template']['spec']['containers'][0]['env']}
        for env_vars in (variables, frontend_env):
            self.assertEqual(env_vars['OCM_OCM_STORAGE_DATA_SERVER_URL'], 'http://opencloud-0.opencloud-headless.cloud.svc.cluster.local:9280/data')
            self.assertEqual(env_vars['STORAGE_USERS_DATA_SERVER_URL'], 'http://opencloud-headless.cloud.svc.cluster.local:9158/data')
        # An existing RWX claim is used as is, without a chart-managed one.
        docs = self.split({**self.SHARED, 'persistence': {'data': {'shared': True, 'accessMode': 'ReadWriteMany', 'existingClaim': 'nas'}}})
        self.assertFalse([d for d in docs if d['kind'] == 'PersistentVolumeClaim'])
        volumes = {v['name']: v for v in workload(docs)['spec']['template']['spec']['volumes']}
        self.assertEqual(volumes['data']['persistentVolumeClaim']['claimName'], 'nas')
        # Excluded singletons are not started at all.
        variables = {e['name']: e.get('value') for e in container(self.split({**self.SHARED, 'excludeServices': ['search']}))['env']}
        self.assertEqual(variables['OC_SINGLETON_SERVICES'], 'ocm,auth-guest')

    def test_split_single_storage_pod_unchanged(self):
        docs = self.split()
        c = container(docs)
        self.assertEqual(c['command'][:2], ['/bin/sh', '-ec'])
        self.assertNotIn('OC_SINGLETON_SERVICES', {e['name'] for e in c['env']})
        self.assertFalse([d for d in docs if d['kind'] == 'PersistentVolumeClaim'])

    def test_shared_storage_scales_without_layout_change(self):
        # In shared mode the replica count is the only difference, so a plain
        # helm upgrade scales the storage pods in both directions.
        workloads = {}
        for replicas in (1, 2, 3):
            values = {**self.SHARED, 'storagePods': {'replicas': replicas}}
            docs = self.split(values)
            spec = workload(docs)['spec']
            self.assertEqual(spec['replicas'], replicas)
            self.assertNotIn('data', [t['metadata']['name'] for t in spec.get('volumeClaimTemplates', [])])
            self.assertEqual(find(docs, 'PersistentVolumeClaim', 'opencloud-data')['spec']['accessModes'], ['ReadWriteMany'])
            workloads[replicas] = {k: v for k, v in spec.items() if k != 'replicas'}
        self.assertEqual(workloads[1], workloads[2])
        self.assertEqual(workloads[2], workloads[3])
        # With one pod in shared mode, the singletons still start there.
        variables = {e['name']: e.get('value') for e in container(self.split({**self.SHARED, 'storagePods': {'replicas': 1}}))['env']}
        self.assertEqual(variables['OC_SINGLETON_SERVICES'], 'search,ocm,auth-guest')

    def test_shared_storage_rejected_combinations(self):
        rwx = {'persistence': {'data': {'accessMode': 'ReadWriteMany'}}}
        for values, message in [
            ({'domain': 'example.org', **self.EXTERNAL, **self.SPLIT, **rwx, 'storagePods': {'replicas': 2}}, 'requires persistence.data.shared'),
            ({'domain': 'example.org', **self.EXTERNAL, **self.SPLIT, 'persistence': {'data': {'shared': True}}}, 'accessMode ReadWriteMany'),
            ({'domain': 'example.org', **self.EXTERNAL, 'storagePods': {'replicas': 2}}, 'requires topology split'),
            ({'domain': 'example.org', **self.EXTERNAL, 'persistence': {'data': {'shared': True, 'accessMode': 'ReadWriteMany'}}}, 'requires topology split'),
        ]:
            with self.subTest(message=message):
                self.assertIn(message, render(values, succeeds=False, raw_defaults=True))
        render({'storagePods': {'replicas': 0}}, succeeds=False)

    def test_split_excluded_services(self):
        docs = self.split({'excludeServices': ['search']})
        self.assertEqual(env(docs)['OC_RUN_SERVICES']['value'], 'storage-users,storage-system,ocm,auth-guest')
        frontend = find(docs, 'Deployment', 'opencloud-frontend')['spec']['template']['spec']['containers'][0]
        self.assertIn('search', {e['name']: e['value'] for e in frontend['env'] if 'value' in e}['OC_EXCLUDE_RUN_SERVICES'].split(','))

    def test_split_frontend_scaling(self):
        docs = self.split()
        self.assertEqual(find(docs, 'Deployment', 'opencloud-frontend')['spec']['replicas'], 2)
        self.assertEqual(find(docs, 'PodDisruptionBudget')['spec']['minAvailable'], 1)
        self.assertFalse([d for d in docs if d['kind'] == 'HorizontalPodAutoscaler'])
        # A budget for a single pod would block node drains.
        self.assertFalse([d for d in self.split({'frontend': {'replicas': 1}}) if d['kind'] == 'PodDisruptionBudget'])
        docs = self.split({'frontend': {'autoscaling': {'enabled': True, 'minReplicas': 3, 'maxReplicas': 9, 'targetCPUUtilizationPercentage': 70}}})
        self.assertNotIn('replicas', find(docs, 'Deployment', 'opencloud-frontend')['spec'])
        hpa = find(docs, 'HorizontalPodAutoscaler')['spec']
        self.assertEqual((hpa['minReplicas'], hpa['maxReplicas'], hpa['scaleTargetRef']['name']), (3, 9, 'opencloud-frontend'))
        self.assertEqual(hpa['metrics'][0]['resource']['target']['averageUtilization'], 70)
        self.assertFalse([d for d in self.split({'frontend': {'autoscaling': {'enabled': True, 'minReplicas': 1}}}) if d['kind'] == 'PodDisruptionBudget'])

    def test_split_frontend_pod_settings(self):
        docs = self.split({'podLabels': {'team': 'a', 'tier': 'x'}, 'nodeSelector': {'disk': 'ssd'},
                           'frontend': {'podLabels': {'tier': 'web'}, 'tolerations': [{'key': 'web', 'operator': 'Exists'}],
                                        'resources': {'requests': {'cpu': '1', 'memory': '1Gi'}}}})
        pod = find(docs, 'Deployment', 'opencloud-frontend')['spec']['template']
        self.assertEqual((pod['metadata']['labels']['team'], pod['metadata']['labels']['tier']), ('a', 'web'))
        self.assertNotIn('nodeSelector', pod['spec'])
        self.assertEqual(pod['spec']['tolerations'], [{'key': 'web', 'operator': 'Exists'}])
        self.assertEqual(pod['spec']['containers'][0]['resources'], {'requests': {'cpu': '1', 'memory': '1Gi'}})
        storage = workload(docs)['spec']['template']['spec']
        self.assertEqual(storage['nodeSelector'], {'disk': 'ssd'})
        self.assertNotIn('tolerations', storage)

    def test_split_rejected_combinations(self):
        for values, message in [
            ({'domain': 'example.org', 'admin': {'existingSecret': 'a'}, **self.SPLIT}, 'identity.mode oidc'),
            ({'domain': 'example.org', **self.EXTERNAL, 'serviceSecrets': {'existingSecret': ''}, **self.SPLIT}, 'serviceSecrets.existingSecret'),
            ({'domain': 'example.org', **self.EXTERNAL, 'topology': 'split'}, 'nats.address'),
            ({'domain': 'example.org', **self.EXTERNAL, **self.SPLIT, 'persistence': {'enabled': False}}, 'persistence.enabled'),
            ({'domain': 'example.org', **self.EXTERNAL, **self.SPLIT, 'frontend': {'autoscaling': {'enabled': True, 'minReplicas': 5, 'maxReplicas': 3}}}, 'minReplicas'),
            ({'domain': 'example.org', **self.EXTERNAL, **self.SPLIT, 'frontend': {'autoscaling': {'enabled': True}, 'resources': {'requests': {'cpu': None, 'memory': '1Gi'}}}}, 'requests.cpu'),
            ({'domain': 'example.org', **self.EXTERNAL, 'nats': {'address': 'nats:4222'}}, 'topology single'),
        ]:
            with self.subTest(message=message):
                self.assertIn(message, render(values, succeeds=False, raw_defaults=True))
        for invalid in [{'topology': 'cluster'}, {'nats': {'address': 'nats://nats:4222'}}, {'frontend': {'replicas': 0}}]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)

    def test_oidc_role_mapping(self):
        mapping = [{'role_name': 'admin', 'claim_value': 'oc-admins'}, {'role_name': 'user', 'claim_value': 'oc-users'}]
        docs = render({'yjs': {'enabled': False}, 'identity': {'mode': 'oidc', 'oidc': {'issuer': 'https://i/r', 'roleAssignment': {'mapping': mapping}}}})
        proxy = yaml.safe_load(files(docs)['proxy.yaml'])
        self.assertEqual(proxy, {'role_assignment': {'oidc_role_mapper': {'role_mapping': mapping}}})
        # Ignored, and no proxy.yaml, with the built-in idp.
        self.assertNotIn('proxy.yaml', files(render({'yjs': {'enabled': False}, 'identity': {'oidc': {'roleAssignment': {'mapping': mapping}}}})))
        render({'identity': {'oidc': {'roleAssignment': {'mapping': [{'role_name': 'root', 'claim_value': 'x'}]}}}}, succeeds=False)

    def test_schema_rejects_unknown_and_invalid(self):
        for invalid in [{'bogus': True}, {'storage': {'bogus': 1}}, {'logLevel': 'loud'}, {'startupProbe': {'successThreshold': 2}},
                        {'smtp': {'port': 0}}, {'persistence': {'data': {'size': 'big'}}}, {'externalUrl': 'cloud.example.org'}]:
            with self.subTest(values=invalid):
                render(invalid, succeeds=False)


    def test_lifecycle_hooks(self):
        self.assertNotIn('lifecycle', workload(render())['spec']['template']['spec']['containers'][0])
        hook = {'preStop': {'exec': {'command': ['sleep', '5']}}}
        container = workload(render({'lifecycleHooks': hook}))['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['lifecycle'], hook)

    def test_opencloud_9_config_update_and_guest_links(self):
        # The init container updates an existing config for a new version, on a copy in /tmp.
        init = workload(render())['spec']['template']['spec']['initContainers'][0]
        script = init['command'][2]
        self.assertIn('env -u IDM_ADMIN_PASSWORD -u ADMIN_PASSWORD \\\n    opencloud init --diff --quiet --config-path "$work"', script)
        # The admin password stays set for the first init, which --diff would reject.
        init = workload(render({'admin': {'password': 'secret'}}))['spec']['template']['spec']['initContainers'][0]
        self.assertIn('IDM_ADMIN_PASSWORD', [e['name'] for e in init['env']])
        self.assertIn('patch /etc/opencloud/opencloud.yaml', script)
        self.assertIn('opencloud init --quiet', script)
        # Guest links are off by default and need SMTP.
        self.assertEqual(env(render())['OC_ENABLE_GUEST_LINKS']['value'], 'false')
        self.assertIn('requires smtp.enabled', render({'sharing': {'guestLinks': True}}, succeeds=False))
        smtp = {'enabled': True, 'host': 'smtp.example.org', 'sender': 'cloud@example.org'}
        self.assertEqual(env(render({'sharing': {'guestLinks': True}, 'smtp': smtp}))['OC_ENABLE_GUEST_LINKS']['value'], 'true')
    UNZIP = {'name': 'unzip', 'image': {'registry': 'docker.io', 'repository': 'opencloudeu/web-extensions',
                                        'tag': 'unzip-2.2.1', 'digest': 'sha256:' + 'b' * 64}}

    def assert_web_extensions(self, pod, names):
        """The pod copies the named extensions into the web-extensions volume and serves them from there."""
        inits = [c for c in pod.get('initContainers', []) if c['name'].startswith('extension-')]
        self.assertEqual([c['name'] for c in inits], ['extension-' + n for n in names])
        c = pod['containers'][0]
        variables = {e['name']: e for e in c['env']}
        mounts = {m['name']: m for m in c['volumeMounts']}
        volumes = {v['name']: v for v in pod['volumes']}
        if not names:
            self.assertNotIn('WEB_ASSET_APPS_PATH', variables)
            self.assertNotIn('web-extensions', mounts)
            self.assertNotIn('web-extensions', volumes)
            return inits
        self.assertEqual(variables['WEB_ASSET_APPS_PATH']['value'], '/var/lib/opencloud-web-extensions')
        self.assertEqual(mounts['web-extensions'], {'name': 'web-extensions', 'mountPath': '/var/lib/opencloud-web-extensions', 'readOnly': True})
        self.assertEqual(volumes['web-extensions'], {'name': 'web-extensions', 'emptyDir': {}})
        for init, name in zip(inits, names):
            self.assertEqual(init['volumeMounts'], [{'name': 'web-extensions', 'mountPath': '/extensions'}])
            self.assertEqual(init['securityContext'], pod['containers'][0]['securityContext'])
            self.assertIn(f'[ ! -f /web/apps/{name}/manifest.json ]', init['command'][2])
            self.assertIn(f'cp -R /web/apps/{name} /extensions/', init['command'][2])
            self.assertEqual(init['command'][:2], ['/bin/sh', '-ec'])
        return inits

    def test_web_extensions_single(self):
        # Without extensions nothing changes, so a manually filled apps directory keeps working.
        self.assert_web_extensions(workload(render())['spec']['template']['spec'], [])
        jsonviewer = {'name': 'json-viewer', 'image': {'repository': 'opencloudeu/web-extensions', 'tag': 'json-viewer-1.0.0',
                                                       'pullPolicy': 'Always', 'pullSecrets': ['extensions']}}
        docs = render({'web': {'extensions': [self.UNZIP, jsonviewer], 'appsConfig': {'unzip': {'config': {}}}},
                       'global': {'imageRegistry': 'mirror.example', 'imagePullSecrets': ['registry']}})
        pod = workload(docs)['spec']['template']['spec']
        unzip, viewer = self.assert_web_extensions(pod, ['unzip', 'json-viewer'])
        # Digest before tag; the global registry applies as for every other image.
        self.assertEqual(unzip['image'], 'mirror.example/opencloudeu/web-extensions@sha256:' + 'b' * 64)
        self.assertEqual(unzip['imagePullPolicy'], 'IfNotPresent')
        self.assertEqual(viewer['image'], 'mirror.example/opencloudeu/web-extensions:json-viewer-1.0.0')
        self.assertEqual(viewer['imagePullPolicy'], 'Always')
        self.assertEqual(pod['imagePullSecrets'], [{'name': 'registry'}, {'name': 'extensions'}])
        # appsConfig stays independent of the installed files.
        self.assertIn('unzip', yaml.safe_load(files(docs)['apps.yaml']))
        # An explicit WEB_ASSET_APPS_PATH wins, like every other chart variable.
        docs = render({'web': {'extensions': [self.UNZIP]}, 'extraEnv': {'WEB_ASSET_APPS_PATH': '/custom'}})
        self.assertEqual(env(docs)['WEB_ASSET_APPS_PATH']['value'], '/custom')

    def test_web_extensions_split(self):
        docs = self.split({'web': {'extensions': [self.UNZIP]}})
        frontend = find(docs, 'Deployment', 'opencloud-frontend')['spec']['template']['spec']
        storage = workload(docs)['spec']['template']['spec']
        self.assert_web_extensions(frontend, ['unzip'])
        # The storage pods do not run web: no init containers, volume or variable.
        self.assert_web_extensions(storage, [])

    def test_web_extensions_rejected(self):
        self.assertIn('requires the web service', render({'web': {'extensions': [self.UNZIP]}, 'excludeServices': ['web']}, succeeds=False))
        self.assertIn('requires the web service', self.split({'web': {'extensions': [self.UNZIP]}, 'excludeServices': ['web']}, succeeds=False))
        self.assertIn('is used twice', render({'web': {'extensions': [self.UNZIP, self.UNZIP]}}, succeeds=False))
        for name in ('Unzip', '../unzip', 'unzip/js', '-unzip', 'a' * 51, ''):
            render({'web': {'extensions': [{**self.UNZIP, 'name': name}]}}, succeeds=False)
        self.assertIn('needs image.tag or image.digest', render({'web': {'extensions': [{'name': 'unzip', 'image': {'repository': 'x/y'}}]}}, succeeds=False))
        render({'web': {'extensions': [{**self.UNZIP, 'path': '/web/apps/unzip'}]}}, succeeds=False)
        render({'web': {'extensions': [{'name': 'unzip', 'image': {'tag': 'unzip-2.2.1'}}]}}, succeeds=False)


if __name__ == '__main__':
    unittest.main()
