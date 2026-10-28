{{/* topology split: the StatefulSet (role "storage", component "server")
     runs the services that keep state on the data volume, and the frontend
     Deployment (role "frontend") runs every other service. They find each
     other through the service registry in the external NATS. In topology
     single the StatefulSet has role "server" and runs everything. */}}

{{- define "opencloud.split" -}}
{{- if eq .Values.topology "split" -}}true{{- end -}}
{{- end -}}

{{/* Services that keep files on the data volume: user and system storage
     (including in-flight uploads), the search index and OCM invitations. */}}
{{- define "opencloud.storageServices" -}}
{{- list "storage-users" "storage-system" "search" "ocm" | toYaml -}}
{{- end -}}

{{/* Storage services whose state only one process may hold: the search
     index and the OCM invitation store. With several storage pods they run on
     the first pod only. */}}
{{- define "opencloud.storageSingletons" -}}
{{- list "search" "ocm" | toYaml -}}
{{- end -}}

{{/* Number of storage pods: storagePods.replicas in topology split, else 1. */}}
{{- define "opencloud.storageReplicas" -}}
{{- if eq (include "opencloud.split" .) "true" -}}{{ int .Values.storagePods.replicas }}{{- else -}}1{{- end -}}
{{- end -}}

{{/* persistence.data.shared: every storage pod mounts one standalone claim
     instead of a claim per pod, whatever the replica count, so the count can
     change later without touching the volume layout. */}}
{{- define "opencloud.sharedData" -}}
{{- if and (eq (include "opencloud.split" .) "true") .Values.persistence.data.shared -}}true{{- end -}}
{{- end -}}

{{- define "opencloud.dataClaimName" -}}
{{- .Values.persistence.data.existingClaim | default (printf "%s-data" (include "opencloud.fullname" .)) -}}
{{- end -}}

{{/* Role of the StatefulSet's pods. */}}
{{- define "opencloud.statefulRole" -}}
{{- if eq (include "opencloud.split" .) "true" -}}storage{{- else -}}server{{- end -}}
{{- end -}}

{{/* Component label of the pods that run the proxy, which serves the
     Service, the metrics endpoint and the routes to the companions. */}}
{{- define "opencloud.proxyComponent" -}}
{{- if eq (include "opencloud.split" .) "true" -}}frontend{{- else -}}server{{- end -}}
{{- end -}}

{{- define "opencloud.proxySelectorLabels" -}}
{{- include "opencloud.componentSelectorLabels" (dict "component" (include "opencloud.proxyComponent" .) "context" .) -}}
{{- end -}}

{{/* Debug port with /healthz, /readyz and /metrics: the proxy's, or in the
     storage pod, which has no proxy, the user storage's. */}}
{{- define "opencloud.roleDebugPort" -}}
{{- if eq .role "storage" -}}9159{{- else -}}{{ include "opencloud.debugPort" .context }}{{- end -}}
{{- end -}}

{{/* Bind address of every gRPC and HTTP service, from OpenCloud's defaults.
     They listen on 127.0.0.1 unless told otherwise, which only works while
     all services share one pod; with 0.0.0.0 the registry advertises the
     pod IP instead. */}}
{{- define "opencloud.bindPorts" -}}
ACTIVITYLOG_HTTP: 9195
APP_PROVIDER_GRPC: 9164
APP_REGISTRY_GRPC: 9242
AUTH_APP_GRPC: 9246
AUTH_APP_HTTP: 9247
AUTH_BASIC_GRPC: 9146
AUTH_BEARER_GRPC: 9148
AUTH_MACHINE_GRPC: 9166
AUTH_SERVICE_GRPC: 9199
COLLABORATION_GRPC: 9301
COLLABORATION_HTTP: 9300
EVENTHISTORY_GRPC: 9274
FRONTEND_HTTP: 9140
GATEWAY_GRPC: 9142
GRAPH_HTTP: 9120
GROUPS_GRPC: 9160
INVITATIONS_HTTP: 9265
OCM_GRPC: 9282
OCM_HTTP: 9280
OCS_HTTP: 9110
POLICIES_GRPC: 9125
SEARCH_GRPC: 9220
SETTINGS_GRPC: 9191
SETTINGS_HTTP: 9190
SHARING_GRPC: 9150
SSE_HTTP: 9135
STORAGE_PUBLICLINK_GRPC: 9178
STORAGE_SHARES_GRPC: 9154
STORAGE_SYSTEM_GRPC: 9215
STORAGE_SYSTEM_HTTP: 9216
STORAGE_USERS_GRPC: 9157
STORAGE_USERS_HTTP: 9158
THUMBNAILS_GRPC: 9185
THUMBNAILS_HTTP: 9186
USERLOG_HTTP: 9210
USERS_GRPC: 9144
WEBDAV_HTTP: 9115
WEBFINGER_HTTP: 9275
WEB_HTTP: 9100
{{- end -}}

{{/* Environment shared by both roles of topology split. */}}
{{- define "opencloud.splitEnv" -}}
{{- $nats := .Values.nats -}}
{{- $storage := printf "%s-headless.%s.svc.%s" (include "opencloud.fullname" .) .Release.Namespace .Values.clusterDomain -}}
{{- $ocmHost := $storage -}}
{{- if eq (include "opencloud.sharedData" .) "true" -}}
{{- $ocmHost = printf "%s-0.%s" (include "opencloud.fullname" .) $storage -}}
{{- end -}}
# Service registry, events, caches and stores in the external NATS.
- name: MICRO_REGISTRY
  value: nats-js-kv
- name: MICRO_REGISTRY_ADDRESS
  value: {{ $nats.address | quote }}
- name: OC_EVENTS_ENDPOINT
  value: {{ $nats.address | quote }}
- name: OC_CACHE_STORE
  value: nats-js-kv
- name: OC_CACHE_STORE_NODES
  value: {{ $nats.address | quote }}
- name: OC_PERSISTENT_STORE
  value: nats-js-kv
- name: OC_PERSISTENT_STORE_NODES
  value: {{ $nats.address | quote }}
{{- if $nats.existingSecret }}
{{- range list "MICRO_REGISTRY_AUTH" "OC_EVENTS_AUTH" "OC_CACHE_AUTH" "OC_PERSISTENT_STORE_AUTH" }}
- name: {{ . }}_USERNAME
  valueFrom:
    secretKeyRef:
      name: {{ $nats.existingSecret | quote }}
      key: {{ $nats.usernameSecretKey | quote }}
- name: {{ . }}_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ $nats.existingSecret | quote }}
      key: {{ $nats.passwordSecretKey | quote }}
{{- end }}
{{- end }}
# Listen on the pod IP so the other role can reach each service.
{{- range $service, $port := include "opencloud.bindPorts" . | fromYaml }}
- name: {{ $service }}_ADDR
  value: "0.0.0.0:{{ $port }}"
{{- end }}
- name: STORAGE_USERS_DEBUG_ADDR
  value: "0.0.0.0:9159"
# OC_GATEWAY_GRPC_ADDR would also move the gateway's own bind address, so
# only the user storage's client setting points at the registry name.
- name: STORAGE_USERS_GATEWAY_GRPC_ADDR
  value: eu.opencloud.api.gateway
# A thumbnail is cached in the pod that renders it, and that pod's
# thumbnails service returns this URL for the download, so it must name the
# pod itself rather than localhost. The startup script formats IPv6 literals.
- name: POD_IP
  valueFrom:
    fieldRef:
      fieldPath: status.podIP
# Data transfers go to the storage pod, not to localhost.
- name: STORAGE_USERS_DATA_SERVER_URL
  value: {{ printf "http://%s:9158/data" $storage | quote }}
- name: STORAGE_SYSTEM_DATA_SERVER_URL
  value: {{ printf "http://%s:9216/data" $storage | quote }}
- name: OCM_OCM_STORAGE_DATA_SERVER_URL
  value: {{ printf "http://%s:9280/data" $ocmHost | quote }}
{{- end -}}

{{/* Container environment for a role: opencloud.env, with the services to
     run and the split wiring for the roles of topology split. */}}
{{- define "opencloud.roleEnv" -}}
{{- $ctx := .context -}}
{{- $env := include "opencloud.env" $ctx | fromYamlArray -}}
{{- if eq .role "server" -}}
{{- $env | toYaml -}}
{{- else -}}
{{- $kept := list -}}
{{- range $env -}}
{{- if not (has .name (list "OC_ADD_RUN_SERVICES" "OC_EXCLUDE_RUN_SERVICES")) -}}{{- $kept = append $kept . -}}{{- end -}}
{{- end -}}
{{- $storage := include "opencloud.storageServices" $ctx | fromYamlArray -}}
{{- $excluded := $ctx.Values.excludeServices | default list -}}
{{- if eq .role "storage" -}}
{{- $shared := eq (include "opencloud.sharedData" $ctx) "true" -}}
{{- $singletons := include "opencloud.storageSingletons" $ctx | fromYamlArray -}}
{{- $run := list -}}
{{- $once := list -}}
{{- range $storage -}}
{{- if not (has . $excluded) -}}
{{- if and $shared (has . $singletons) -}}{{- $once = append $once . -}}{{- else -}}{{- $run = append $run . -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- $kept = append $kept (dict "name" "OC_RUN_SERVICES" "value" (join "," $run)) -}}
{{- if $once -}}
{{- /* Added by the container command on the first pod only. */ -}}
{{- $kept = append $kept (dict "name" "OC_SINGLETON_SERVICES" "value" (join "," $once)) -}}
{{- end -}}
{{- else -}}
{{- with include "opencloud.addServices" $ctx -}}
{{- $kept = append $kept (dict "name" "OC_ADD_RUN_SERVICES" "value" .) -}}
{{- end -}}
{{- $skip := concat (splitList "," (include "opencloud.excludeServices" $ctx)) $storage (list "nats") | compact | uniq -}}
{{- $kept = append $kept (dict "name" "OC_EXCLUDE_RUN_SERVICES" "value" (join "," $skip)) -}}
{{- end -}}
{{- concat $kept (include "opencloud.splitEnv" $ctx | fromYamlArray) | toYaml -}}
{{- end -}}
{{- end -}}

{{/* Number of frontend pods the PodDisruptionBudget can count on. */}}
{{- define "opencloud.frontendMinReplicas" -}}
{{- if .Values.frontend.autoscaling.enabled -}}{{ .Values.frontend.autoscaling.minReplicas }}{{- else -}}{{ .Values.frontend.replicas }}{{- end -}}
{{- end -}}

{{- define "opencloud.validateTopology" -}}
{{- if not (has .Values.topology (list "single" "split")) -}}
{{- fail "topology must be single or split" -}}
{{- end -}}
{{- if eq (include "opencloud.split" .) "true" -}}
{{- if or (ne .Values.identity.mode "oidc") (not .Values.identity.ldap.uri) -}}
{{- fail "topology split requires identity.mode oidc and an external identity.ldap.uri: the built-in idm and idp keep their state in one pod" -}}
{{- end -}}
{{- if not .Values.serviceSecrets.existingSecret -}}
{{- fail "topology split requires serviceSecrets.existingSecret: every pod needs the same service secrets, which opencloud init can only write to one pod's config volume" -}}
{{- end -}}
{{- if not .Values.nats.address -}}
{{- fail "topology split requires nats.address, an external NATS cluster with JetStream: the pods share the service registry, events and caches through it" -}}
{{- end -}}
{{- if not .Values.persistence.enabled -}}
{{- fail "topology split requires persistence.enabled: the storage pod keeps the files and metadata on the data volume" -}}
{{- end -}}
{{- if eq (include "opencloud.sharedData" .) "true" -}}
{{- if ne .Values.persistence.data.accessMode "ReadWriteMany" -}}
{{- fail "persistence.data.shared requires persistence.data.accessMode ReadWriteMany: the storage pods share one data volume" -}}
{{- end -}}
{{- else if gt (int .Values.storagePods.replicas) 1 -}}
{{- fail "storagePods.replicas above 1 requires persistence.data.shared: the storage pods share one ReadWriteMany data volume" -}}
{{- end -}}
{{- $autoscaling := .Values.frontend.autoscaling -}}
{{- if and $autoscaling.enabled (gt (int $autoscaling.minReplicas) (int $autoscaling.maxReplicas)) -}}
{{- fail "frontend.autoscaling.minReplicas must not exceed maxReplicas" -}}
{{- end -}}
{{- if and $autoscaling.enabled (not (dig "requests" "cpu" "" .Values.frontend.resources)) -}}
{{- fail "frontend.autoscaling scales on CPU use relative to the request: set frontend.resources.requests.cpu" -}}
{{- end -}}
{{- else if .Values.nats.address -}}
{{- fail "nats.address is used only with topology split; topology single runs NATS inside the pod" -}}
{{- else if gt (int .Values.storagePods.replicas) 1 -}}
{{- fail "storagePods.replicas above 1 requires topology split" -}}
{{- else if .Values.persistence.data.shared -}}
{{- fail "persistence.data.shared requires topology split" -}}
{{- end -}}
{{- end -}}
