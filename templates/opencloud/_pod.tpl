{{/* Pod template of an OpenCloud role (see _topology.tpl), indented for a
     workload's spec. The frontend has its own resources, scheduling and pod
     metadata, which take precedence over the top-level podLabels and
     podAnnotations; every other setting is shared. */}}
{{- define "opencloud.podTemplate" }}
{{- $ctx := .context }}
{{- $role := .role }}
{{- $frontend := eq $role "frontend" }}
{{- $shared := and (eq $role "storage") (eq (include "opencloud.sharedData" $ctx) "true") }}
{{- $fullname := include "opencloud.fullname" $ctx }}
{{- $podLabels := $ctx.Values.podLabels }}
{{- $podAnnotations := $ctx.Values.podAnnotations }}
{{- $scheduling := pick $ctx.Values "nodeSelector" "affinity" "tolerations" "topologySpreadConstraints" }}
{{- $resources := $ctx.Values.resources }}
{{- if $frontend }}
{{- $podLabels = merge (dict) $ctx.Values.frontend.podLabels $ctx.Values.podLabels }}
{{- $podAnnotations = merge (dict) $ctx.Values.frontend.podAnnotations $ctx.Values.podAnnotations }}
{{- $scheduling = pick $ctx.Values.frontend "nodeSelector" "affinity" "tolerations" "topologySpreadConstraints" }}
{{- $resources = $ctx.Values.frontend.resources }}
{{- end }}
  template:
    metadata:
      labels:
        {{- include "opencloud.componentPodLabels" (dict "component" (ternary "frontend" "server" $frontend) "podLabels" $podLabels "context" $ctx) | nindent 8 }}
      annotations:
        {{- with (omit $podAnnotations "checksum/config" "checksum/secret") }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
        checksum/config: {{ include (print $ctx.Template.BasePath "/opencloud/configmap.yaml") $ctx | sha256sum }}
        checksum/secret: {{ include (print $ctx.Template.BasePath "/opencloud/secret.yaml") $ctx | sha256sum }}
    spec:
      serviceAccountName: {{ include "opencloud.serviceAccountName" $ctx }}
      automountServiceAccountToken: {{ $ctx.Values.serviceAccount.automountServiceAccountToken }}
      terminationGracePeriodSeconds: {{ $ctx.Values.terminationGracePeriodSeconds }}
      {{- include "common.images.renderPullSecrets" (dict "images" (list $ctx.Values.image) "context" $ctx) | nindent 6 }}
      {{- if $ctx.Values.podSecurityContext.enabled }}
      securityContext:
        {{- omit $ctx.Values.podSecurityContext "enabled" | toYaml | nindent 8 }}
      {{- end }}
      {{- with $ctx.Values.hostAliases }}
      hostAliases: {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $scheduling.nodeSelector }}
      nodeSelector: {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $scheduling.affinity }}
      affinity: {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $scheduling.tolerations }}
      tolerations: {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $scheduling.topologySpreadConstraints }}
      topologySpreadConstraints: {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- $external := eq (include "opencloud.externalServiceSecrets" $ctx) "true" }}
      {{- if or (not $external) $ctx.Values.extraInitContainers }}
      initContainers:
        {{- if not $external }}
        # Generates /etc/opencloud/opencloud.yaml with random service secrets on the
        # first start; the file lives on the config volume. Later starts apply what a
        # new OpenCloud version adds (opencloud init --diff keeps existing values). The
        # diff runs on a copy in /tmp, because it writes a backup file on every run.
        # --diff refuses to run with an admin password set, so the diff unsets it.
        - name: init-config
          image: {{ include "opencloud.image" $ctx | quote }}
          imagePullPolicy: {{ $ctx.Values.image.pullPolicy }}
          {{- if $ctx.Values.containerSecurityContext.enabled }}
          securityContext:
            {{- omit $ctx.Values.containerSecurityContext "enabled" | toYaml | nindent 12 }}
          {{- end }}
          command:
            - /bin/sh
            - -ec
            - |
              if [ -s /etc/opencloud/opencloud.yaml ]; then
                work=$(mktemp -d)
                cp /etc/opencloud/opencloud.yaml "$work/"
                env -u IDM_ADMIN_PASSWORD -u ADMIN_PASSWORD \
                  opencloud init --diff --quiet --config-path "$work" >/dev/null
                if [ -f "$work/opencloud.config.patch" ]; then
                  patch /etc/opencloud/opencloud.yaml < "$work/opencloud.config.patch"
                  echo "opencloud.yaml updated for this OpenCloud version"
                else
                  echo "opencloud.yaml is up to date"
                fi
                rm -rf "$work"
              else
                opencloud init --quiet
                echo "opencloud.yaml generated"
              fi
          env:
            - name: OC_INSECURE
              value: {{ $ctx.Values.insecure | quote }}
            {{- if or $ctx.Values.admin.existingSecret $ctx.Values.admin.password }}
            - name: IDM_ADMIN_PASSWORD
              valueFrom:
                secretKeyRef:
                  {{- include "opencloud.secretRef" (dict "existingSecret" $ctx.Values.admin.existingSecret "existingKey" $ctx.Values.admin.passwordSecretKey "managedKey" "adminPassword" "context" $ctx) | nindent 18 }}
            {{- end }}
          volumeMounts:
            - name: config
              mountPath: /etc/opencloud
            - name: tmp
              mountPath: /tmp
          resources:
            {{- toYaml $ctx.Values.initResources | nindent 12 }}
        {{- end }}
        {{- with $ctx.Values.extraInitContainers }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- end }}
      {{- /* Environment for the OpenCloud container. extraEnv and extraEnvVars entries replace chart-set
             variables of the same name, since Kubernetes keeps one per name. */}}
      {{- $overrides := $ctx.Values.extraEnvVars | default list }}
      {{- $names := list }}
      {{- range $overrides }}
      {{- if has .name $names }}
      {{- fail (printf "extraEnvVars contains duplicate variable %q" .name) }}
      {{- end }}
      {{- $names = append $names .name }}
      {{- end }}
      {{- range $key, $value := $ctx.Values.extraEnv }}
      {{- if not (has $key $names) }}
      {{- $overrides = append $overrides (dict "name" $key "value" (toString $value)) }}
      {{- $names = append $names $key }}
      {{- end }}
      {{- end }}
      {{- $env := list }}
      {{- range include "opencloud.roleEnv" (dict "context" $ctx "role" $role) | fromYamlArray }}
      {{- if not (has .name $names) }}{{- $env = append $env . }}{{- end }}
      {{- end }}
      {{- $env = concat $env $overrides }}
      {{- $envFrom := list }}
      {{- if $ctx.Values.extraSecretEnv }}{{- $envFrom = append $envFrom (dict "secretRef" (dict "name" (printf "%s-env" $fullname))) }}{{- end }}
      {{- with $ctx.Values.existingSecret }}{{- $envFrom = append $envFrom (dict "secretRef" (dict "name" .)) }}{{- end }}
      {{- $mounts := list (dict "name" "config" "mountPath" "/etc/opencloud") (dict "name" "data" "mountPath" "/var/lib/opencloud") (dict "name" "tmp" "mountPath" "/tmp") }}
      {{- range $file := include "opencloud.configFiles" $ctx | fromYamlArray }}
      {{- $mounts = append $mounts (dict "name" "files" "mountPath" (printf "/etc/opencloud/%s" $file) "subPath" $file "readOnly" true) }}
      {{- end }}
      {{- $mounts = concat $mounts ($ctx.Values.extraVolumeMounts | default list) }}
      containers:
        - name: opencloud
          image: {{ include "opencloud.image" $ctx | quote }}
          imagePullPolicy: {{ $ctx.Values.image.pullPolicy }}
          {{- if $ctx.Values.containerSecurityContext.enabled }}
          securityContext:
            {{- omit $ctx.Values.containerSecurityContext "enabled" | toYaml | nindent 12 }}
          {{- end }}
          {{- if ne $role "server" }}
          # Format pod URLs and listener addresses for the pod IP family.
          command:
            - /bin/sh
            - -ec
            - |
              pod_host="$POD_IP"
              case "$POD_IP" in
                *:*)
                  pod_host="[$POD_IP]"
                  {{- $ports := include "opencloud.bindPorts" $ctx | fromYaml }}
                  {{- $_ := set $ports "STORAGE_USERS_DEBUG" 9159 }}
                  {{- $_ := set $ports "PROXY_HTTP" 9200 }}
                  {{- $_ := set $ports "PROXY_DEBUG" (include "opencloud.debugPort" $ctx) }}
                  {{- range $service, $port := $ports }}
                  {{- if not (has (printf "%s_ADDR" $service) $names) }}
                  export {{ $service }}_ADDR="[$POD_IP]:{{ $port }}"
                  {{- end }}
                  {{- end }}
                  ;;
              esac
              export WEBDAV_THUMBNAILS_GENERATOR_URL="${WEBDAV_THUMBNAILS_GENERATOR_URL:-http://${pod_host}:9186}"
              {{- if $shared }}
              if [ "${HOSTNAME##*-}" = "0" ] && [ -n "${OC_SINGLETON_SERVICES:-}" ]; then
                export OC_RUN_SERVICES="${OC_RUN_SERVICES},${OC_SINGLETON_SERVICES}"
              fi
              {{- end }}
              exec opencloud server
          {{- else }}
          args: ["server"]
          {{- end }}
          env:
            {{- toYaml $env | nindent 12 }}
          {{- with $envFrom }}
          envFrom:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with $ctx.Values.lifecycleHooks }}
          lifecycle: {{- toYaml . | nindent 12 }}
          {{- end }}
          ports:
            {{- if eq $role "storage" }}
            - name: users-data
              containerPort: 9158
            - name: system-data
              containerPort: 9216
            - name: ocm-data
              containerPort: 9280
            {{- else }}
            - name: http
              containerPort: 9200
            {{- end }}
            - name: debug
              containerPort: {{ include "opencloud.roleDebugPort" (dict "role" $role "context" $ctx) }}
          {{- if $ctx.Values.startupProbe.enabled }}
          startupProbe:
            httpGet:
              path: /healthz
              port: debug
            {{- omit $ctx.Values.startupProbe "enabled" | toYaml | nindent 12 }}
          {{- end }}
          {{- if $ctx.Values.livenessProbe.enabled }}
          livenessProbe:
            httpGet:
              path: /healthz
              port: debug
            {{- omit $ctx.Values.livenessProbe "enabled" | toYaml | nindent 12 }}
          {{- end }}
          {{- if $ctx.Values.readinessProbe.enabled }}
          readinessProbe:
            httpGet:
              path: /readyz
              port: debug
            {{- omit $ctx.Values.readinessProbe "enabled" | toYaml | nindent 12 }}
          {{- end }}
          volumeMounts:
            {{- toYaml $mounts | nindent 12 }}
          resources:
            {{- toYaml $resources | nindent 12 }}
      volumes:
        - name: tmp
          emptyDir: {}
        - name: files
          configMap:
            name: {{ $fullname }}-files
        {{- if $frontend }}
        # Service secrets come from the environment; /var/lib/opencloud only
        # holds caches such as thumbnails and web extensions.
        - name: config
          emptyDir: {}
        - name: data
          emptyDir: {}
        {{- else }}
        {{- range $name, $persistent := dict "config" (eq (include "opencloud.configPersistent" $ctx) "true") "data" $ctx.Values.persistence.enabled }}
        {{- $p := index $ctx.Values.persistence $name }}
        {{- if not $persistent }}
        - name: {{ $name }}
          emptyDir: {}
        {{- else if and $shared (eq $name "data") }}
        - name: data
          persistentVolumeClaim:
            claimName: {{ include "opencloud.dataClaimName" $ctx | quote }}
        {{- else if $p.existingClaim }}
        - name: {{ $name }}
          persistentVolumeClaim:
            claimName: {{ $p.existingClaim | quote }}
        {{- end }}
        {{- end }}
        {{- end }}
        {{- with $ctx.Values.extraVolumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
{{- end }}
