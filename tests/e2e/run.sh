#!/usr/bin/env bash
# End-to-end test on a throwaway kind cluster with Keycloak and OpenLDAP:
#   1. fresh install with serviceSecrets.existingSecret and no config volume
#      (Keycloak login, roles, LDAP groups, upload/download, all companions,
#      read-only root filesystem), then persistence across pod replacement
#   2. migration of an initialised installation to serviceSecrets, first
#      keeping the config claim, then a controlled StatefulSet recreation
#      without it, preserving the data claim
# Requires kind, kubectl, helm, docker and python3. Uses its own kubeconfig;
# KEEP=1 leaves the cluster running for inspection.
set -euo pipefail

chart="$(cd "$(dirname "$0")/../.." && pwd)"
e2e="$chart/tests/e2e"
work="$(mktemp -d)"
# A name of its own per run: cleanup must never touch a cluster this run did
# not create, such as one kept by an earlier KEEP=1 run.
cluster="opencloud-e2e-$(basename "$work" | tr -cd 'a-z0-9' | tail -c 8)"
export KUBECONFIG="$work/kubeconfig"
created=0
pids=()

cleanup() {
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
  if [ "$created" = 1 ] && [ "${KEEP:-0}" = 1 ]; then
    echo "Cluster $cluster kept: KUBECONFIG=$KUBECONFIG kind delete cluster --name $cluster"
    return
  fi
  if [ "$created" = 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }

# forward NAMESPACE TARGET PORT: starts a port-forward and sets FORWARD_URL and
# FORWARD_PID. Call it directly, not in $(...): a subshell would lose the PID
# that cleanup needs.
forward() {
  local port
  port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  kubectl -n "$1" port-forward "$2" "$port:$3" >/dev/null 2>&1 &
  FORWARD_PID=$!
  pids+=("$FORWARD_PID")
  FORWARD_URL="http://127.0.0.1:$port"
  for _ in $(seq 1 30); do
    if ! kill -0 "$FORWARD_PID" 2>/dev/null; then
      echo "port-forward to $1/$2 exited before becoming ready" >&2
      return 1
    fi
    curl -s --connect-timeout 1 --max-time 2 -o /dev/null "$FORWARD_URL" && return 0
    sleep 1
  done
  echo "port-forward to $1/$2 did not come up" >&2
  return 1
}

wait_ready() { # wait_ready NAMESPACE
  kubectl -n "$1" wait --for=condition=Ready pod --all --timeout=600s >/dev/null
}

install() { # install NAMESPACE [helm args...]
  local ns=$1; shift
  helm upgrade --install opencloud "$chart" --namespace "$ns" -f "$e2e/values.yaml" \
    --set "externalUrl=http://opencloud.$ns.svc.cluster.local:9200" --wait --timeout 10m "$@" >/dev/null
  wait_ready "$ns"
}

checks() { # checks NAMESPACE write|read MARKER
  forward "$1" svc/opencloud 9200 || return $?
  local status=0
  OC="$FORWARD_URL" KC="$KC" python3 "$e2e/check.py" "$2" "$3" || status=$?
  kill "$FORWARD_PID" 2>/dev/null || true
  wait "$FORWARD_PID" 2>/dev/null || true
  # This forward is the last one registered; do not retain a stale PID.
  unset 'pids[${#pids[@]}-1]'
  return "$status"
}

step "kind cluster"
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 120s >/dev/null 2>&1
created=1

step "Keycloak and OpenLDAP"
kubectl create namespace idp >/dev/null
kubectl -n idp create configmap keycloak-realm --from-file=openCloud-realm.json="$e2e/realm.json" >/dev/null
kubectl -n idp apply -f "$e2e/infra.yaml" >/dev/null
kubectl -n idp rollout status deploy/openldap deploy/keycloak --timeout=600s >/dev/null
forward idp svc/keycloak 8080
KC=$FORWARD_URL

prepare() { # prepare NAMESPACE
  kubectl create namespace "$1" >/dev/null
  kubectl -n "$1" create secret generic ldap-bind --from-literal=bindPassword=admin-pass >/dev/null
}

step "1. fresh install, externally managed service secrets, no config volume"
prepare oc
python3 "$chart/scripts/service-secrets.py" generate --name service-secrets 2>/dev/null | kubectl -n oc apply -f - >/dev/null
install oc --set serviceSecrets.existingSecret=service-secrets --set persistence.config.enabled=false
kubectl -n oc get statefulset opencloud -o json | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]
pod = spec["template"]["spec"]
assert [t["metadata"]["name"] for t in spec["volumeClaimTemplates"]] == ["data"], "only the data claim"
assert "initContainers" not in pod, "no opencloud init"
assert {"name": "config", "emptyDir": {}} in pod["volumes"]
print("ok   StatefulSet: data claim only, no init container, emptyDir config")'
kubectl -n oc get pods -o json | python3 -c '
import json, sys
pods = json.load(sys.stdin)["items"]
assert pods, "no pods found"
for pod in pods:
    for container in pod["spec"]["containers"] + pod["spec"].get("initContainers", []):
        assert container.get("securityContext", {}).get("readOnlyRootFilesystem") is True, (pod["metadata"]["name"], container["name"])
print("ok   every container has a read-only root filesystem")'
kubectl -n oc exec opencloud-0 -c opencloud -- sh -c '! test -e /etc/opencloud/opencloud.yaml'
echo "ok   no generated opencloud.yaml"
checks oc write fresh

step "1b. pod replacement"
kubectl -n oc delete pod opencloud-0 --wait >/dev/null
wait_ready oc
checks oc read fresh

step "2. migration: initialised install -> service secrets"
prepare oc-mig
install oc-mig
checks oc-mig write before
kubectl -n oc-mig exec opencloud-0 -c opencloud -- cat /etc/opencloud/opencloud.yaml \
  | python3 "$chart/scripts/service-secrets.py" from-config - --name service-secrets 2>/dev/null \
  | kubectl -n oc-mig apply -f - >/dev/null
data_uid=$(kubectl -n oc-mig get pvc data-opencloud-0 -o jsonpath='{.metadata.uid}')

step "2a. switch to the Secret, keeping the config claim (volumeClaimTemplates unchanged)"
install oc-mig --set serviceSecrets.existingSecret=service-secrets
checks oc-mig read before
checks oc-mig write after-switch

step "2b. controlled StatefulSet recreation without the config claim"
kubectl -n oc-mig delete statefulset opencloud --cascade=orphan >/dev/null
install oc-mig --set serviceSecrets.existingSecret=service-secrets --set persistence.config.enabled=false
kubectl -n oc-mig rollout status statefulset/opencloud --timeout=600s >/dev/null
wait_ready oc-mig
[ "$(kubectl -n oc-mig get pvc data-opencloud-0 -o jsonpath='{.metadata.uid}')" = "$data_uid" ] \
  && echo "ok   data claim preserved" || { echo "FAIL data claim was replaced"; exit 1; }
kubectl -n oc-mig get pod opencloud-0 -o json | python3 -c '
import json, sys
volumes = {v["name"]: v for v in json.load(sys.stdin)["spec"]["volumes"]}
assert "emptyDir" in volumes["config"], volumes["config"]
assert volumes["data"]["persistentVolumeClaim"]["claimName"] == "data-opencloud-0", volumes["data"]
print("ok   config is an emptyDir, data still uses data-opencloud-0")'
checks oc-mig read before
checks oc-mig read after-switch

step "all end-to-end checks passed"
