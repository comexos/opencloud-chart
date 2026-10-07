{{/* Environment of the OpenCloud container as a YAML list. extraEnv and
     extraEnvVars entries replace chart-set variables of the same name in
     the StatefulSet, since Kubernetes keeps only one entry per name. */}}
{{- define "opencloud.env" -}}
{{- $oidc := .Values.identity.oidc }}
{{- $ldap := .Values.identity.ldap }}
- name: OC_URL
  value: {{ include "opencloud.url" . | quote }}
- name: OC_LOG_LEVEL
  value: {{ .Values.logLevel | quote }}
- name: OC_LOG_PRETTY
  value: {{ .Values.logPretty | quote }}
- name: OC_LOG_COLOR
  value: {{ .Values.logPretty | quote }}
- name: OC_INSECURE
  value: {{ .Values.insecure | quote }}
{{- with include "opencloud.addServices" . }}
- name: OC_ADD_RUN_SERVICES
  value: {{ . | quote }}
{{- end }}
{{- with include "opencloud.excludeServices" . }}
- name: OC_EXCLUDE_RUN_SERVICES
  value: {{ . | quote }}
{{- end }}
# TLS terminates at the Ingress or Gateway.
- name: PROXY_TLS
  value: "false"
- name: PROXY_HTTP_ADDR
  value: "0.0.0.0:9200"
# Health probes and metrics.
- name: PROXY_DEBUG_ADDR
  value: "0.0.0.0:{{ include "opencloud.debugPort" . }}"
{{- if or .Values.metrics.token.existingSecret .Values.metrics.token.value }}
- name: PROXY_DEBUG_TOKEN
  valueFrom:
    secretKeyRef:
      {{- include "opencloud.secretRef" (dict "existingSecret" .Values.metrics.token.existingSecret "existingKey" .Values.metrics.token.secretKey "managedKey" "metricsToken" "context" .) | nindent 18 }}
{{- end }}
- name: PROXY_ENABLE_BASIC_AUTH
  value: {{ .Values.basicAuth | quote }}
- name: PROXY_CSP_CONFIG_FILE_LOCATION
  value: /etc/opencloud/csp.yaml
- name: FRONTEND_CHECK_FOR_UPDATES
  value: {{ .Values.checkForUpdates | quote }}
- name: FRONTEND_ARCHIVER_MAX_SIZE
  value: {{ .Values.archiverMaxSize | quote }}
{{- with .Values.defaultLanguage }}
- name: OC_DEFAULT_LANGUAGE
  value: {{ . | quote }}
{{- end }}
- name: OC_SHARING_PUBLIC_SHARE_MUST_HAVE_PASSWORD
  value: {{ .Values.sharing.publicShareMustHavePassword | quote }}
- name: OC_SHARING_PUBLIC_WRITEABLE_SHARE_MUST_HAVE_PASSWORD
  value: {{ .Values.sharing.publicWriteableShareMustHavePassword | quote }}
- name: OC_PASSWORD_POLICY_DISABLED
  value: {{ .Values.passwordPolicy.disabled | quote }}
- name: OC_PASSWORD_POLICY_MIN_CHARACTERS
  value: {{ .Values.passwordPolicy.minCharacters | quote }}
- name: OC_PASSWORD_POLICY_MIN_LOWERCASE_CHARACTERS
  value: {{ .Values.passwordPolicy.minLowercaseCharacters | quote }}
- name: OC_PASSWORD_POLICY_MIN_UPPERCASE_CHARACTERS
  value: {{ .Values.passwordPolicy.minUppercaseCharacters | quote }}
- name: OC_PASSWORD_POLICY_MIN_DIGITS
  value: {{ .Values.passwordPolicy.minDigits | quote }}
- name: OC_PASSWORD_POLICY_MIN_SPECIAL_CHARACTERS
  value: {{ .Values.passwordPolicy.minSpecialCharacters | quote }}
{{- if .Values.passwordPolicy.bannedPasswords }}
- name: OC_PASSWORD_POLICY_BANNED_PASSWORDS_LIST
  value: /etc/opencloud/banned-password-list.txt
{{- end }}
{{- if or .Values.admin.existingSecret .Values.admin.password }}
- name: IDM_ADMIN_PASSWORD
  valueFrom:
    secretKeyRef:
      {{- include "opencloud.secretRef" (dict "existingSecret" .Values.admin.existingSecret "existingKey" .Values.admin.passwordSecretKey "managedKey" "adminPassword" "context" .) | nindent 18 }}
{{- end }}
- name: IDM_CREATE_DEMO_USERS
  value: {{ .Values.demoUsers | quote }}
{{- if eq (include "opencloud.externalServiceSecrets" .) "true" }}
# Service secrets and IDs from the existing Secret instead of opencloud init.
{{- range $key, $names := include "opencloud.serviceSecretEnv" . | fromYaml }}
{{- range $names }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.serviceSecrets.existingSecret | quote }}
      key: {{ $key }}
{{- end }}
{{- end }}
{{- end }}
{{- if eq .Values.identity.mode "oidc" }}
# External OpenID Connect provider.
- name: OC_OIDC_ISSUER
  value: {{ trimSuffix "/" $oidc.issuer | quote }}
- name: PROXY_OIDC_REWRITE_WELLKNOWN
  value: "true"
- name: WEB_OIDC_CLIENT_ID
  value: {{ $oidc.webClientId | quote }}
- name: WEBFINGER_WEB_OIDC_CLIENT_ID
  value: {{ $oidc.webClientId | quote }}
- name: WEB_OIDC_SCOPE
  value: {{ $oidc.webScopes | quote }}
- name: WEBFINGER_WEB_OIDC_CLIENT_SCOPES
  value: {{ $oidc.webScopes | quote }}
{{- with $oidc.accountUrl }}
- name: WEB_OPTION_ACCOUNT_EDIT_LINK_HREF
  value: {{ . | quote }}
{{- end }}
{{- with $oidc.audiences }}
- name: PROXY_OIDC_AUDIENCES
  value: {{ join "," . | quote }}
{{- end }}
- name: PROXY_USER_OIDC_CLAIM
  value: {{ $oidc.userClaim | quote }}
- name: PROXY_USER_CS3_CLAIM
  value: {{ $oidc.userCs3Claim | quote }}
- name: PROXY_AUTOPROVISION_ACCOUNTS
  value: {{ $oidc.autoprovision | quote }}
{{- if $oidc.autoprovision }}
- name: PROXY_AUTOPROVISION_CLAIM_USERNAME
  value: {{ $oidc.autoprovisionClaimUsername | quote }}
{{- end }}
- name: PROXY_ROLE_ASSIGNMENT_DRIVER
  value: {{ $oidc.roleAssignment.driver | quote }}
- name: PROXY_ROLE_ASSIGNMENT_OIDC_CLAIM
  value: {{ $oidc.roleAssignment.claim | quote }}
- name: GRAPH_ASSIGN_DEFAULT_USER_ROLE
  value: {{ $oidc.assignDefaultUserRole | quote }}
# Administrators come from the IdP's role claim, not a local account.
- name: OC_ADMIN_USER_ID
  value: ""
- name: SETTINGS_SETUP_DEFAULT_ASSIGNMENTS
  value: "false"
- name: GRAPH_USERNAME_MATCH
  value: "none"
# Profile attributes are owned by the IdP.
- name: FRONTEND_READONLY_USER_ATTRIBUTES
  value: "user.onPremisesSamAccountName,user.displayName,user.mail,user.passwordProfile,user.accountEnabled,user.appRoleAssignments"
{{- end }}
{{- if eq (include "opencloud.externalLdap" .) "true" }}
# External LDAP replaces the built-in idm.
- name: OC_LDAP_URI
  value: {{ $ldap.uri | quote }}
- name: OC_LDAP_INSECURE
  value: {{ $ldap.insecure | quote }}
- name: OC_LDAP_BIND_DN
  value: {{ $ldap.bindDn | quote }}
- name: OC_LDAP_BIND_PASSWORD
  valueFrom:
    secretKeyRef:
      {{- include "opencloud.secretRef" (dict "existingSecret" $ldap.existingSecret "existingKey" $ldap.bindPasswordSecretKey "managedKey" "ldapBindPassword" "context" .) | nindent 18 }}
- name: OC_LDAP_USER_BASE_DN
  value: {{ $ldap.userBaseDn | quote }}
- name: OC_LDAP_GROUP_BASE_DN
  value: {{ $ldap.groupBaseDn | quote }}
- name: OC_LDAP_USER_FILTER
  value: {{ $ldap.userFilter | quote }}
{{- with $ldap.groupFilter }}
- name: OC_LDAP_GROUP_FILTER
  value: {{ . | quote }}
{{- end }}
- name: OC_LDAP_USER_SCHEMA_ID
  value: {{ $ldap.userSchemaId | quote }}
- name: OC_LDAP_GROUP_SCHEMA_ID
  value: {{ $ldap.groupSchemaId | quote }}
- name: OC_LDAP_DISABLE_USER_MECHANISM
  value: {{ $ldap.disableUserMechanism | quote }}
- name: OC_LDAP_SERVER_WRITE_ENABLED
  value: {{ $ldap.writeEnabled | quote }}
- name: GRAPH_LDAP_SERVER_UUID
  value: {{ $ldap.serverUuid | quote }}
- name: GRAPH_LDAP_REFINT_ENABLED
  value: {{ $ldap.refintEnabled | quote }}
{{- with $ldap.groupCreateBaseDn }}
- name: GRAPH_LDAP_GROUP_CREATE_BASE_DN
  value: {{ . | quote }}
{{- end }}
{{- end }}
- name: STORAGE_USERS_DRIVER
  value: {{ .Values.storage.driver | quote }}
{{- if eq .Values.storage.driver "decomposeds3" }}
# System data stays on the data volume; it is small.
- name: STORAGE_SYSTEM_DRIVER
  value: decomposed
- name: STORAGE_USERS_DECOMPOSEDS3_ENDPOINT
  value: {{ .Values.storage.s3.endpoint | quote }}
- name: STORAGE_USERS_DECOMPOSEDS3_REGION
  value: {{ .Values.storage.s3.region | quote }}
- name: STORAGE_USERS_DECOMPOSEDS3_BUCKET
  value: {{ .Values.storage.s3.bucket | quote }}
- name: STORAGE_USERS_DECOMPOSEDS3_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      {{- include "opencloud.secretRef" (dict "existingSecret" .Values.storage.s3.existingSecret "existingKey" .Values.storage.s3.accessKeySecretKey "managedKey" "s3AccessKey" "context" .) | nindent 18 }}
- name: STORAGE_USERS_DECOMPOSEDS3_SECRET_KEY
  valueFrom:
    secretKeyRef:
      {{- include "opencloud.secretRef" (dict "existingSecret" .Values.storage.s3.existingSecret "existingKey" .Values.storage.s3.secretKeySecretKey "managedKey" "s3SecretKey" "context" .) | nindent 18 }}
{{- end }}
{{- if .Values.smtp.enabled }}
- name: NOTIFICATIONS_SMTP_HOST
  value: {{ .Values.smtp.host | quote }}
- name: NOTIFICATIONS_SMTP_PORT
  value: {{ .Values.smtp.port | quote }}
- name: NOTIFICATIONS_SMTP_SENDER
  value: {{ .Values.smtp.sender | quote }}
{{- with .Values.smtp.username }}
- name: NOTIFICATIONS_SMTP_USERNAME
  value: {{ . | quote }}
- name: NOTIFICATIONS_SMTP_PASSWORD
  valueFrom:
    secretKeyRef:
      {{- include "opencloud.secretRef" (dict "existingSecret" $.Values.smtp.existingSecret "existingKey" $.Values.smtp.passwordSecretKey "managedKey" "smtpPassword" "context" $) | nindent 18 }}
{{- end }}
- name: NOTIFICATIONS_SMTP_AUTHENTICATION
  value: {{ .Values.smtp.authentication | quote }}
- name: NOTIFICATIONS_SMTP_ENCRYPTION
  value: {{ .Values.smtp.encryption | quote }}
- name: NOTIFICATIONS_SMTP_INSECURE
  value: {{ .Values.smtp.insecure | quote }}
{{- end }}
{{- if .Values.collaboration.enabled }}
{{- $app := .Values.collaboration.app }}
# The WOPI endpoint is served by the proxy on the OpenCloud host.
- name: COLLABORATION_WOPI_SRC
  value: {{ include "opencloud.url" . | quote }}
- name: COLLABORATION_APP_NAME
  value: {{ $app.name | quote }}
- name: COLLABORATION_APP_PRODUCT
  value: {{ $app.product | quote }}
- name: COLLABORATION_APP_ADDR
  value: {{ trimSuffix "/" $app.url | quote }}
- name: COLLABORATION_APP_ICON
  value: {{ $app.icon | default (printf "%s/favicon.ico" (trimSuffix "/" $app.url)) | quote }}
- name: COLLABORATION_APP_INSECURE
  value: {{ $app.insecure | quote }}
- name: COLLABORATION_APP_PROOF_DISABLE
  value: {{ $app.proofDisable | quote }}
- name: COLLABORATION_CS3API_DATAGATEWAY_INSECURE
  value: {{ .Values.insecure | quote }}
{{- if and .Values.collaboration.secureView (eq $app.product "Collabora") }}
- name: FRONTEND_APP_HANDLER_SECURE_VIEW_APP_ADDR
  value: eu.opencloud.api.collaboration
# The default share roles plus the secure-view role (aa97fe03-...).
- name: GRAPH_AVAILABLE_ROLES
  value: "b1e2218d-eef8-4d4c-b82d-0f1a1b48f3b5,a8d5fe5e-96e3-418d-825b-534dbdf22b99,fb6c3e19-e378-47e5-b277-9732f9de6e21,58c63c02-1d89-4572-916a-870abc5a1b7d,2d00ce52-1fc2-4dbc-8b95-a73b73395f5a,1c996275-f1c9-4e71-abdf-a42f6495e960,312c0871-5ef7-4b3a-85b6-0e4074c64049,aa97fe03-7980-45ac-9e50-b325749fd7e6"
{{- end }}
{{- end }}
{{- with include "opencloud.tikaUrl" . }}
- name: SEARCH_EXTRACTOR_TYPE
  value: tika
- name: SEARCH_EXTRACTOR_TIKA_TIKA_URL
  value: {{ . | quote }}
- name: FRONTEND_FULL_TEXT_SEARCH_ENABLED
  value: "true"
{{- end }}
{{- if .Values.antivirus.enabled }}
- name: POSTPROCESSING_STEPS
  value: virusscan
- name: ANTIVIRUS_SCANNER_TYPE
  value: {{ .Values.antivirus.scanner | quote }}
{{- if eq .Values.antivirus.scanner "clamav" }}
- name: ANTIVIRUS_CLAMAV_SOCKET
  value: {{ .Values.antivirus.clamavSocket | quote }}
{{- else }}
- name: ANTIVIRUS_ICAP_URL
  value: {{ .Values.antivirus.icapUrl | quote }}
{{- end }}
- name: ANTIVIRUS_MAX_SCAN_SIZE
  value: {{ .Values.antivirus.maxScanSize | quote }}
- name: ANTIVIRUS_MAX_SCAN_SIZE_MODE
  value: {{ .Values.antivirus.maxScanSizeMode | quote }}
- name: ANTIVIRUS_INFECTED_FILE_HANDLING
  value: {{ .Values.antivirus.infectedFileHandling | quote }}
- name: ANTIVIRUS_WORKERS
  value: {{ .Values.antivirus.workers | quote }}
{{- end }}
{{- if .Values.yjs.enabled }}
- name: WEB_OPTION_YJS_SERVER_URL
  value: {{ printf "%s/yjs" (include "opencloud.url" . | replace "https://" "wss://" | replace "http://" "ws://") | quote }}
{{- end }}
{{- end -}}
