{{/* Delegate shared chart conventions to Bitnami Common. */}}
{{- define "opencloud.name" -}}
{{- include "common.names.name" . -}}
{{- end -}}

{{- define "opencloud.fullname" -}}
{{- include "common.names.fullname" . -}}
{{- end -}}

{{/* Every workload has its own component in its selector, so no selector
     or Service of one component matches another component's pods. */}}
{{- define "opencloud.componentFullname" -}}
{{- printf "%s-%s" (include "opencloud.fullname" .context) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "opencloud.componentLabels" -}}
{{- include "common.labels.standard" (dict "customLabels" (merge (dict "app.kubernetes.io/component" .component) (omit .context.Values.additionalLabels "app.kubernetes.io/name" "app.kubernetes.io/instance" "app.kubernetes.io/component")) "context" .context) -}}
{{- end -}}

{{- define "opencloud.componentSelectorLabels" -}}
{{ include "common.labels.matchLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Pod template labels: selector labels always win over podLabels and additionalLabels. */}}
{{- define "opencloud.componentPodLabels" -}}
{{- $custom := merge (dict "app.kubernetes.io/component" .component) (omit (.podLabels | default dict) "app.kubernetes.io/name" "app.kubernetes.io/instance" "app.kubernetes.io/component") (omit .context.Values.additionalLabels "app.kubernetes.io/name" "app.kubernetes.io/instance" "app.kubernetes.io/component") -}}
{{- include "common.labels.standard" (dict "customLabels" $custom "context" .context) -}}
{{- end -}}

{{- define "opencloud.labels" -}}
{{- include "opencloud.componentLabels" (dict "component" "server" "context" .) -}}
{{- end -}}

{{- define "opencloud.selectorLabels" -}}
{{- include "opencloud.componentSelectorLabels" (dict "component" "server" "context" .) -}}
{{- end -}}

{{- define "opencloud.annotations" -}}
{{- merge (.specific | default dict) .context.Values.additionalAnnotations | toYaml -}}
{{- end -}}

{{- define "opencloud.image" -}}
{{- include "common.images.image" (dict "imageRoot" .Values.image "global" .Values.global "chart" .Chart) -}}
{{- end -}}

{{- define "opencloud.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "opencloud.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Public hostname: host, or cloud.<domain>. */}}
{{- define "opencloud.host" -}}
{{- if .Values.host -}}
{{- .Values.host -}}
{{- else if .Values.domain -}}
{{- printf "cloud.%s" .Values.domain -}}
{{- else -}}
{{- fail "host or domain is required: OpenCloud needs its public URL" -}}
{{- end -}}
{{- end -}}

{{/* OC_URL without a trailing slash. */}}
{{- define "opencloud.url" -}}
{{- if .Values.externalUrl -}}
{{- trimSuffix "/" .Values.externalUrl -}}
{{- else -}}
{{- printf "https://%s" (include "opencloud.host" .) -}}
{{- end -}}
{{- end -}}

{{/* scheme://host[:port] of a URL, for CSP sources. */}}
{{- define "opencloud.origin" -}}
{{- $u := urlParse . -}}
{{- printf "%s://%s" $u.scheme $u.host -}}
{{- end -}}

{{- define "opencloud.debugPort" -}}9205{{- end -}}

{{- define "opencloud.oidc" -}}
{{- if eq .Values.identity.mode "oidc" -}}true{{- end -}}
{{- end -}}

{{- define "opencloud.externalLdap" -}}
{{- if and (eq .Values.identity.mode "oidc") .Values.identity.ldap.uri -}}true{{- end -}}
{{- end -}}

{{- define "opencloud.tikaUrl" -}}
{{- if .Values.tika.externalUrl -}}
{{- .Values.tika.externalUrl -}}
{{- else if .Values.tika.enabled -}}
{{- printf "http://%s:9998" (include "opencloud.componentFullname" (dict "component" "tika" "context" .)) -}}
{{- end -}}
{{- end -}}

{{- define "opencloud.addServices" -}}
{{- $services := .Values.additionalServices | default list -}}
{{- if .Values.smtp.enabled -}}{{- $services = append $services "notifications" -}}{{- end -}}
{{- if .Values.collaboration.enabled -}}{{- $services = append $services "collaboration" -}}{{- end -}}
{{- if .Values.antivirus.enabled -}}{{- $services = append $services "antivirus" -}}{{- end -}}
{{- $services | uniq | join "," -}}
{{- end -}}

{{- define "opencloud.excludeServices" -}}
{{- $services := .Values.excludeServices | default list -}}
{{- if eq (include "opencloud.oidc" .) "true" -}}{{- $services = append $services "idp" -}}{{- end -}}
{{- if eq (include "opencloud.externalLdap" .) "true" -}}{{- $services = append $services "idm" -}}{{- end -}}
{{- $services | uniq | join "," -}}
{{- end -}}

{{/* The proxy routes the chart adds for its companions, plus user routes. */}}
{{- define "opencloud.proxyRoutes" -}}
{{- $routes := list -}}
{{- if .Values.yjs.enabled -}}
{{- $routes = append $routes (dict "endpoint" "/yjs" "backend" (printf "http://%s:1234" (include "opencloud.componentFullname" (dict "component" "yjs" "context" .))) "unprotected" true) -}}
{{- end -}}
{{- if .Values.radicale.enabled -}}
{{- $backend := printf "http://%s:5232" (include "opencloud.componentFullname" (dict "component" "radicale" "context" .)) -}}
{{- range $endpoint, $script := dict "/caldav/" "/caldav" "/.well-known/caldav" "/caldav" "/carddav/" "/carddav" "/.well-known/carddav" "/carddav" -}}
{{- $routes = append $routes (dict "endpoint" $endpoint "backend" $backend "remote_user_header" "X-Remote-User" "skip_x_access_token" true "additional_headers" (list (dict "X-Script-Name" $script))) -}}
{{- end -}}
{{- end -}}
{{- concat $routes (.Values.proxy.additionalRoutes | default list) | toYaml -}}
{{- end -}}

{{/* csp.yaml: a restrictive base policy plus the office server and IdP
     origins the chart knows about, plus csp.extraDirectives. */}}
{{- define "opencloud.csp" -}}
{{- $d := dict
  "child-src" (list "'self'")
  "connect-src" (list "'self'" "blob:" "https://raw.githubusercontent.com/opencloud-eu/awesome-apps/" "https://update.opencloud.eu/")
  "default-src" (list "'none'")
  "font-src" (list "'self'")
  "frame-ancestors" (list "'self'")
  "frame-src" (list "'self'" "blob:" "https://embed.diagrams.net/")
  "img-src" (list "'self'" "data:" "blob:" "https://raw.githubusercontent.com/opencloud-eu/awesome-apps/")
  "manifest-src" (list "'self'")
  "media-src" (list "'self'")
  "object-src" (list "'self'" "blob:")
  "script-src" (list "'self'" "'unsafe-inline'")
  "style-src" (list "'self'" "'unsafe-inline'" "blob:")
  "worker-src" (list "'self'" "blob:")
-}}
{{- if and .Values.collaboration.enabled .Values.collaboration.app.url -}}
{{- $office := printf "%s/" (include "opencloud.origin" .Values.collaboration.app.url) -}}
{{- $_ := set $d "frame-src" (append (index $d "frame-src") $office) -}}
{{- $_ := set $d "img-src" (append (index $d "img-src") $office) -}}
{{- end -}}
{{- if and (eq (include "opencloud.oidc" .) "true") .Values.identity.oidc.issuer -}}
{{- $idp := printf "%s/" (include "opencloud.origin" .Values.identity.oidc.issuer) -}}
{{- range list "connect-src" "frame-src" "script-src" -}}
{{- $_ := set $d . (append (index $d .) $idp) -}}
{{- end -}}
{{- end -}}
{{- range $directive, $sources := .Values.csp.extraDirectives -}}
{{- $_ := set $d $directive (concat (get $d $directive | default list) $sources) -}}
{{- end -}}
{{- range $directive, $sources := $d -}}
{{- $_ := set $d $directive (uniq $sources) -}}
{{- end -}}
{{- dict "directives" $d | toYaml -}}
{{- end -}}

{{/* proxy.yaml content: companion and user routes, plus a custom OIDC role
     mapping, which OpenCloud only reads from its config file. Empty when
     neither is configured. */}}
{{- define "opencloud.proxyConfig" -}}
{{- $config := dict -}}
{{- $routes := include "opencloud.proxyRoutes" . | fromYamlArray -}}
{{- if $routes -}}
{{- $_ := set $config "additional_policies" (list (dict "name" "default" "routes" $routes)) -}}
{{- end -}}
{{- if and (eq (include "opencloud.oidc" .) "true") .Values.identity.oidc.roleAssignment.mapping -}}
{{- $_ := set $config "role_assignment" (dict "oidc_role_mapper" (dict "role_mapping" .Values.identity.oidc.roleAssignment.mapping)) -}}
{{- end -}}
{{- if $config -}}{{- $config | toYaml -}}{{- end -}}
{{- end -}}

{{- define "opencloud.externalServiceSecrets" -}}
{{- if .Values.serviceSecrets.existingSecret -}}true{{- end -}}
{{- end -}}

{{/* /etc/opencloud is a PVC only when it must keep generated secrets. */}}
{{- define "opencloud.configPersistent" -}}
{{- if and .Values.persistence.enabled .Values.persistence.config.enabled -}}true{{- end -}}
{{- end -}}

{{/* Secret key -> environment variables. The service account ID also fills
     SETTINGS_SERVICE_ACCOUNT_IDS, which falls back to OC_SERVICE_ACCOUNT_ID. */}}
{{- define "opencloud.serviceSecretEnv" -}}
jwtSecret: [OC_JWT_SECRET]
machineAuthApiKey: [OC_MACHINE_AUTH_API_KEY]
systemUserApiKey: [OC_SYSTEM_USER_API_KEY]
systemUserId: [OC_SYSTEM_USER_ID]
transferSecret: [OC_TRANSFER_SECRET]
urlSigningSecret: [OC_URL_SIGNING_SECRET]
graphApplicationId: [GRAPH_APPLICATION_ID]
serviceAccountId: [OC_SERVICE_ACCOUNT_ID]
serviceAccountSecret: [OC_SERVICE_ACCOUNT_SECRET]
wopiSecret: [COLLABORATION_WOPI_SECRET]
thumbnailsTransferToken: [THUMBNAILS_TRANSFER_TOKEN]
storageUsersMountId: [STORAGE_USERS_MOUNT_ID, GATEWAY_STORAGE_USERS_MOUNT_ID]
{{- end -}}

{{/* Files of the <fullname>-files ConfigMap, each mounted into /etc/opencloud. */}}
{{- define "opencloud.configFiles" -}}
{{- $files := list "csp.yaml" -}}
{{- if include "opencloud.proxyConfig" . | fromYaml -}}{{- $files = append $files "proxy.yaml" -}}{{- end -}}
{{- if .Values.passwordPolicy.bannedPasswords -}}{{- $files = append $files "banned-password-list.txt" -}}{{- end -}}
{{- if .Values.collaboration.appRegistry -}}{{- $files = append $files "app-registry.yaml" -}}{{- end -}}
{{- if .Values.web.appsConfig -}}{{- $files = append $files "apps.yaml" -}}{{- end -}}
{{- $files | toYaml -}}
{{- end -}}

{{- define "opencloud.radicaleConfig" -}}
{{- if .Values.radicale.config -}}
{{- .Values.radicale.config -}}
{{- else -}}
[server]
hosts = 0.0.0.0:5232

[auth]
# The OpenCloud proxy authenticates the user and passes it in X-Remote-User.
type = http_x_remote_user

[storage]
filesystem_folder = /var/lib/radicale/collections
predefined_collections = {
    "def-addressbook": {"D:displayname": "Personal Address Book", "tag": "VADDRESSBOOK"},
    "def-calendar": {"C:supported-calendar-component-set": "VEVENT,VJOURNAL,VTODO", "D:displayname": "Personal Calendar", "tag": "VCALENDAR"}
  }

[web]
type = none
{{- end -}}
{{- end -}}

{{/* True when a chart-managed credentials Secret is needed for any plain-value fallback. */}}
{{- define "opencloud.needsManagedSecret" -}}
{{- if or
  (and (not .Values.admin.existingSecret) .Values.admin.password)
  (and (eq .Values.storage.driver "decomposeds3") (not .Values.storage.s3.existingSecret) .Values.storage.s3.accessKey .Values.storage.s3.secretKey)
  (and .Values.smtp.enabled (not .Values.smtp.existingSecret) .Values.smtp.password)
  (and (eq (include "opencloud.externalLdap" .) "true") (not .Values.identity.ldap.existingSecret) .Values.identity.ldap.bindPassword)
  (and (not .Values.metrics.token.existingSecret) .Values.metrics.token.value)
  .Values.extraSecretEnv
-}}
true
{{- end -}}
{{- end -}}

{{- define "opencloud.secretRef" -}}
{{- if .existingSecret -}}
name: {{ .existingSecret | quote }}
key: {{ .existingKey | quote }}
{{- else -}}
name: {{ include "opencloud.fullname" .context }}-env
key: {{ .managedKey }}
{{- end -}}
{{- end -}}

{{- define "opencloud.validateValues" -}}
{{- $_ := include "opencloud.url" . -}}
{{- if not (has .Values.identity.mode (list "builtin" "oidc")) -}}
{{- fail "identity.mode must be builtin or oidc" -}}
{{- end -}}
{{- if and (eq .Values.identity.mode "builtin") (not (hasPrefix "https://" (include "opencloud.url" .))) -}}
{{- fail "identity.mode builtin requires an https:// externalUrl: the built-in idp refuses other issuers" -}}
{{- end -}}
{{- if and (eq .Values.identity.mode "builtin") (not .Values.admin.existingSecret) (not .Values.admin.password) -}}
{{- fail "identity.mode builtin requires admin.existingSecret or admin.password for the initial administrator" -}}
{{- end -}}
{{- if eq .Values.identity.mode "oidc" -}}
{{- if not .Values.identity.oidc.issuer -}}
{{- fail "identity.oidc.issuer is required when identity.mode=oidc" -}}
{{- end -}}
{{- if .Values.identity.ldap.uri -}}
{{- if or (not .Values.identity.ldap.bindDn) (not .Values.identity.ldap.userBaseDn) (not .Values.identity.ldap.groupBaseDn) -}}
{{- fail "identity.ldap.uri requires identity.ldap.bindDn, userBaseDn and groupBaseDn" -}}
{{- end -}}
{{- if and (not .Values.identity.ldap.existingSecret) (not .Values.identity.ldap.bindPassword) -}}
{{- fail "identity.ldap.uri requires identity.ldap.existingSecret or identity.ldap.bindPassword" -}}
{{- end -}}
{{- if and .Values.identity.oidc.autoprovision (not .Values.identity.ldap.writeEnabled) -}}
{{- fail "identity.oidc.autoprovision requires a writable LDAP: set identity.ldap.writeEnabled or disable autoprovisioning" -}}
{{- end -}}
{{- end -}}
{{- else if .Values.identity.ldap.uri -}}
{{- fail "identity.ldap.uri requires identity.mode=oidc; the built-in idp only works with the built-in idm" -}}
{{- end -}}
{{- if not (has .Values.storage.driver (list "posix" "decomposed" "decomposeds3")) -}}
{{- fail "storage.driver must be posix, decomposed or decomposeds3" -}}
{{- end -}}
{{- if eq .Values.storage.driver "decomposeds3" -}}
{{- if or (not .Values.storage.s3.endpoint) (not .Values.storage.s3.bucket) -}}
{{- fail "storage.driver decomposeds3 requires storage.s3.endpoint and storage.s3.bucket" -}}
{{- end -}}
{{- if and (not .Values.storage.s3.existingSecret) (or (not .Values.storage.s3.accessKey) (not .Values.storage.s3.secretKey)) -}}
{{- fail "storage.s3 requires existingSecret, or both accessKey and secretKey" -}}
{{- end -}}
{{- end -}}
{{- if .Values.smtp.enabled -}}
{{- if or (not .Values.smtp.host) (not .Values.smtp.sender) -}}
{{- fail "smtp.enabled requires smtp.host and smtp.sender" -}}
{{- end -}}
{{- if and .Values.smtp.username (not .Values.smtp.existingSecret) (not .Values.smtp.password) -}}
{{- fail "smtp.username requires smtp.existingSecret or smtp.password" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.collaboration.enabled (not .Values.collaboration.app.url) -}}
{{- fail "collaboration.enabled requires collaboration.app.url, the public URL of the office server" -}}
{{- end -}}
{{- if .Values.antivirus.enabled -}}
{{- if and (eq .Values.antivirus.scanner "clamav") (not .Values.antivirus.clamavSocket) -}}
{{- fail "antivirus.scanner clamav requires antivirus.clamavSocket, e.g. tcp://clamav:3310" -}}
{{- end -}}
{{- if and (eq .Values.antivirus.scanner "icap") (not .Values.antivirus.icapUrl) -}}
{{- fail "antivirus.scanner icap requires antivirus.icapUrl" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.tika.enabled .Values.tika.externalUrl -}}
{{- fail "tika.enabled deploys Tika; leave tika.externalUrl empty, or disable tika.enabled to use an external server" -}}
{{- end -}}
{{- if eq (include "opencloud.externalServiceSecrets" .) "true" -}}
{{- if or (ne .Values.identity.mode "oidc") (not .Values.identity.ldap.uri) -}}
{{- fail "serviceSecrets.existingSecret is supported only with identity.mode oidc and an external identity.ldap.uri" -}}
{{- end -}}
{{- if or .Values.admin.existingSecret .Values.admin.password -}}
{{- fail "admin.* is not used with serviceSecrets.existingSecret: administrators come from the IdP's role claim" -}}
{{- end -}}
{{- else if and .Values.persistence.enabled (not .Values.persistence.config.enabled) -}}
{{- fail "persistence.config.enabled=false requires serviceSecrets.existingSecret; otherwise the secrets generated by opencloud init are lost on every restart" -}}
{{- end -}}
{{- if and .Values.metrics.serviceMonitor.enabled (not .Values.metrics.enabled) -}}
{{- fail "metrics.serviceMonitor.enabled requires metrics.enabled" -}}
{{- end -}}
{{- if and .Values.gatewayAPI.httpRoute.enabled (not .Values.gatewayAPI.httpRoute.parentRefs) -}}
{{- fail "gatewayAPI.httpRoute.parentRefs must reference an existing Gateway" -}}
{{- end -}}
{{- if and (gt (len .Values.service.ipFamilies) 1) (not (has .Values.service.ipFamilyPolicy (list "PreferDualStack" "RequireDualStack"))) -}}
{{- fail "service.ipFamilies with two entries requires service.ipFamilyPolicy PreferDualStack or RequireDualStack" -}}
{{- end -}}
{{- end -}}
