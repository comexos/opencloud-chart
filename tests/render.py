"""Render checks: python3 tests/render.py (requires Helm and PyYAML)."""
from pathlib import Path
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
        self.assertEqual(variables['STORAGE_USERS_DECOMPOSEDS3_ACCESS_KEY']['valueFrom']['secretKeyRef'], {'name': 's3', 'key': 'accessKey'})
        docs = render({'storage': {'driver': 'decomposeds3', 's3': {**s3, 'existingSecret': '', 'accessKey': 'id', 'secretKey': 'key'}}})
        self.assertEqual(env(docs)['STORAGE_USERS_DECOMPOSEDS3_SECRET_KEY']['valueFrom']['secretKeyRef'], {'name': 'opencloud-env', 'key': 's3SecretKey'})
        self.assertEqual(find(docs, 'Secret', 'opencloud-env')['stringData'], {'s3AccessKey': 'id', 's3SecretKey': 'key'})
        for invalid in [
            {'storage': {'driver': 'decomposeds3'}},
            {'storage': {'driver': 'decomposeds3', 's3': {**s3, 'existingSecret': '', 'accessKey': 'id'}}},
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
            {'antivirus': {'enabled': True, 'clamavSocket': 'tcp://c:3310', 'maxScanSizeMode': 'partial'}},
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

    def external(self, extra=None):
        return render({'domain': 'example.org', **self.EXTERNAL, **(extra or {})}, raw_defaults=True)

    def test_external_service_secrets(self):
        docs = self.external()
        pod = workload(docs)['spec']['template']['spec']
        self.assertNotIn('initContainers', pod)
        variables = env(docs)
        expected = {
            'OC_JWT_SECRET': 'jwtSecret', 'OC_MACHINE_AUTH_API_KEY': 'machineAuthApiKey', 'OC_SYSTEM_USER_API_KEY': 'systemUserApiKey',
            'OC_SYSTEM_USER_ID': 'systemUserId', 'OC_TRANSFER_SECRET': 'transferSecret', 'OC_URL_SIGNING_SECRET': 'urlSigningSecret',
            'GRAPH_APPLICATION_ID': 'graphApplicationId', 'OC_SERVICE_ACCOUNT_ID': 'serviceAccountId', 'OC_SERVICE_ACCOUNT_SECRET': 'serviceAccountSecret',
            'COLLABORATION_WOPI_SECRET': 'wopiSecret', 'THUMBNAILS_TRANSFER_TOKEN': 'thumbnailsTransferToken',
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


if __name__ == '__main__':
    unittest.main()
