#!/usr/bin/env bash
# End-to-end test of topology split with several storage pods on one shared
# NFS volume (storagePods.replicas 2, ReadWriteMany), on a throwaway kind
# cluster with three nodes:
#   1. NFS: two pods on different workers share a volume; content, extended
#      attributes and flock must work across nodes (what OpenCloud relies on)
#   2. OpenCloud with external NATS, Keycloak and OpenLDAP: the regular
#      checks, then parallel uploads and renames through both storage pods
#   3. each storage pod is replaced in turn; the files must stay complete
#   4. the storage pods are scaled 2 -> 1 -> 2 with helm upgrade on the same
#      data claim; the files must stay complete
#
# Requires kind, kubectl, helm, python3 and Docker (or Podman with
# KIND_EXPERIMENTAL_PROVIDER=podman), run with enough privileges for kind's
# privileged node containers (rootful Docker or Podman). NFS mounts do not work
# in rootless container engines.
#
# Environment:
#   NFS_SERVER         external NFS export as host:/path (e.g. a NAS), reachable
#                      from the kind nodes; default: an NFS server in the cluster,
#                      which needs the nfsd kernel module on this host
#                      (sudo modprobe nfsd)
#   NFS_MOUNT_OPTIONS  default nfsvers=4.2,noac (OpenCloud needs NFS 4.2 for
#                      extended attributes)
#   DRIVER             storage driver: decomposed (default; metadata handling as
#                      decomposeds3, without S3) or posix
#   CSI_NFS_VERSION    csi-driver-nfs chart version, default as pinned below
#   FILES, WORKERS     size of the parallel upload test (40 files, 16 workers)
#   REGISTRY_SETTLE    seconds to wait after pods start or are replaced, default 40
#   KEEP=1             leave the cluster running for inspection
set -euo pipefail

shared="$(cd "$(dirname "$0")" && pwd)"
e2e="$(dirname "$shared")"
chart="$(cd "$e2e/../.." && pwd)"
work="$(mktemp -d)"
cluster="opencloud-shared-$(basename "$work" | tr -cd 'a-z0-9' | tail -c 8)"
export KUBECONFIG="$work/kubeconfig"
NFS_MOUNT_OPTIONS="${NFS_MOUNT_OPTIONS:-nfsvers=4.2,noac}"
DRIVER="${DRIVER:-decomposed}"
# renovate: datasource=helm depName=csi-driver-nfs registryUrl=https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts
CSI_NFS_VERSION="${CSI_NFS_VERSION:-4.13.6}"
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
    # Unmount the NFS volumes before the NFS server goes away: a node container
    # with a hanging NFS mount cannot be removed, and kind delete fails halfway.
    kubectl delete namespace oc nfs-check --ignore-not-found --wait --timeout=180s >/dev/null 2>&1 || true
    kubectl delete pvc --all -A --wait --timeout=60s >/dev/null 2>&1 || true
    # NFS mounts are hard: a mount left on a worker after the server is gone
    # hangs its processes, and the node container cannot be killed. Lazily
    # force-unmount whatever is left, while the server is still running.
    for node in $(kind get nodes --name "$cluster" 2>/dev/null); do
      docker exec "$node" sh -c \
        "awk '\$3 ~ /^nfs/ {print \$2}' /proc/mounts | sort -r | xargs -r -n1 umount -f -l" >/dev/null 2>&1 || true
    done
    # The in-cluster server runs the kernel nfsd in the control plane's network
    # namespace: a node container killed with nfsd still running cannot be
    # removed either. Deleting the pod lets it stop nfsd on SIGTERM.
    kubectl delete namespace nfs --ignore-not-found --wait --timeout=120s >/dev/null 2>&1 || true
    if ! output=$(kind delete cluster --name "$cluster" 2>&1); then
      echo "WARN could not delete kind cluster $cluster:" >&2
      echo "$output" >&2
      echo "     remove it with: kind delete cluster --name $cluster" >&2
    fi
  fi
  rm -rf "$work"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAIL $*" >&2; exit 1; }

forward() { # forward NAMESPACE TARGET PORT: sets FORWARD_URL and FORWARD_PID
  local port
  port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  kubectl -n "$1" port-forward "$2" "$port:$3" >/dev/null 2>&1 &
  FORWARD_PID=$!
  pids+=("$FORWARD_PID")
  FORWARD_URL="http://127.0.0.1:$port"
  for _ in $(seq 1 30); do
    kill -0 "$FORWARD_PID" 2>/dev/null || { echo "port-forward to $1/$2 exited" >&2; return 1; }
    curl -s --connect-timeout 1 --max-time 2 -o /dev/null "$FORWARD_URL" && return 0
    sleep 1
  done
  echo "port-forward to $1/$2 did not come up" >&2
  return 1
}

wait_ready() { kubectl -n "$1" wait --for=condition=Ready pod --all --timeout=600s >/dev/null; }

with_opencloud() { # with_opencloud COMMAND...: runs COMMAND with OC and KC set
  forward oc svc/opencloud 9200 || return $?
  local status=0
  OC="$FORWARD_URL" KC="$KC" "$@" || status=$?
  kill "$FORWARD_PID" 2>/dev/null || true
  wait "$FORWARD_PID" 2>/dev/null || true
  unset 'pids[${#pids[@]}-1]'
  return "$status"
}

replace_pod() { # replace_pod NAME: deletes a storage pod and waits for its replacement
  kubectl -n oc delete pod "$1" --wait >/dev/null
  kubectl -n oc rollout status statefulset/opencloud --timeout=600s >/dev/null
  wait_ready oc
  settle
}

# Every OpenCloud pod caches the service registry for 30 seconds (hard-coded in
# OpenCloud 8.1): after pods start or go away, requests may go to an address
# that is not (or no longer) registered and fail until the cache expires. The
# checks test the steady state, not that window.
settle() { sleep "${REGISTRY_SETTLE:-40}"; }

step "prerequisites"
for tool in kind kubectl helm python3 curl; do
  command -v "$tool" >/dev/null || fail "$tool is not installed"
done
if [ -z "${NFS_SERVER:-}" ] && ! grep -qw nfsd /proc/filesystems; then
  fail "the in-cluster NFS server needs the nfsd kernel module: sudo modprobe nfsd (or set NFS_SERVER=host:/path)"
fi
case "$DRIVER" in decomposed|posix) ;; *) fail "DRIVER must be decomposed or posix" ;; esac
echo "ok   driver $DRIVER, NFS ${NFS_SERVER:-in-cluster}, mount options $NFS_MOUNT_OPTIONS"

step "kind cluster (control plane + 2 workers)"
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --config "$shared/kind.yaml" --wait 180s >/dev/null 2>&1
created=1

step "NFS server and csi-driver-nfs"
if [ -n "${NFS_SERVER:-}" ]; then
  nfs_host="${NFS_SERVER%%:*}"
  nfs_share="${NFS_SERVER#*:}"
else
  kubectl create namespace nfs >/dev/null
  kubectl -n nfs apply -f "$shared/nfs-server.yaml" >/dev/null
  kubectl -n nfs rollout status deploy/nfs-server --timeout=300s >/dev/null
  nfs_host="nfs-server.nfs.svc.cluster.local"
  nfs_share="/"
fi
helm repo add csi-driver-nfs https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts >/dev/null
helm repo update csi-driver-nfs >/dev/null
helm upgrade --install csi-driver-nfs csi-driver-nfs/csi-driver-nfs --namespace kube-system \
  --version "$CSI_NFS_VERSION" --wait --timeout 5m >/dev/null
mount_options=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1].split(",")))' "$NFS_MOUNT_OPTIONS")
kubectl apply -f - >/dev/null <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: nfs-rwx
provisioner: nfs.csi.k8s.io
parameters:
  server: "$nfs_host"
  share: "$nfs_share"
reclaimPolicy: Delete
volumeBindingMode: Immediate
mountOptions: $mount_options
EOF
echo "ok   storage class nfs-rwx -> $nfs_host:$nfs_share"

step "1. NFS across nodes: content, extended attributes, flock"
kubectl create namespace nfs-check >/dev/null
kubectl -n nfs-check apply -f "$shared/nfs-check.yaml" >/dev/null
kubectl -n nfs-check wait --for=jsonpath='{.status.phase}'=Succeeded pod/nfs-checker --timeout=300s >/dev/null \
  || { kubectl -n nfs-check logs nfs-checker || true; fail "NFS check did not pass"; }
kubectl -n nfs-check logs nfs-checker
holder_node=$(kubectl -n nfs-check get pod nfs-holder -o jsonpath='{.spec.nodeName}')
checker_node=$(kubectl -n nfs-check get pod nfs-checker -o jsonpath='{.spec.nodeName}')
[ "$holder_node" != "$checker_node" ] || fail "NFS check pods ran on the same node"
echo "ok   pods on $holder_node and $checker_node"
kubectl delete namespace nfs-check --wait=false >/dev/null

step "Keycloak, OpenLDAP and NATS"
kubectl create namespace idp >/dev/null
kubectl -n idp create configmap keycloak-realm --from-file=openCloud-realm.json="$e2e/realm.json" >/dev/null
kubectl -n idp apply -f "$e2e/infra.yaml" >/dev/null
kubectl create namespace nats >/dev/null
kubectl -n nats apply -f "$shared/nats.yaml" >/dev/null
kubectl -n idp rollout status deploy/openldap deploy/keycloak --timeout=600s >/dev/null
kubectl -n nats rollout status deploy/nats --timeout=300s >/dev/null
forward idp svc/keycloak 8080
KC=$FORWARD_URL

step "2. OpenCloud: split, 2 storage pods on one NFS volume, 2 frontend pods"
kubectl create namespace oc >/dev/null
kubectl -n oc create secret generic ldap-bind --from-literal=bindPassword=admin-pass >/dev/null
python3 "$chart/scripts/service-secrets.py" generate --name service-secrets 2>/dev/null | kubectl -n oc apply -f - >/dev/null
install() { # install [helm args...]: installs or upgrades the release with the test values
  helm upgrade --install opencloud "$chart" --namespace oc -f "$e2e/values.yaml" -f "$shared/values.yaml" \
    --set "externalUrl=http://opencloud.oc.svc.cluster.local:9200" --set "storage.driver=$DRIVER" \
    --wait --timeout 15m "$@" >/dev/null
  kubectl -n oc rollout status statefulset/opencloud --timeout=600s >/dev/null
  wait_ready oc
}
install
kubectl -n oc get pvc opencloud-data -o jsonpath='{.spec.accessModes[0]} {.spec.storageClassName}{"\n"}' \
  | grep -qx 'ReadWriteMany nfs-rwx' || fail "the shared data claim is not ReadWriteMany on nfs-rwx"
nodes=$(kubectl -n oc get pod opencloud-0 opencloud-1 -o jsonpath='{.items[*].spec.nodeName}')
read -r node0 node1 <<<"$nodes"
[ "$node0" != "$node1" ] || fail "both storage pods run on $node0"
echo "ok   storage pods on $node0 and $node1, sharing opencloud-data"

# search and ocm run on the first storage pod only: the container command adds
# them to OC_RUN_SERVICES there before exec'ing opencloud (PID 1).
running() { # running POD: the services the opencloud process was started with
  kubectl -n oc exec "$1" -c opencloud -- sh -c 'tr "\0" "\n" < /proc/1/environ | sed -n "s/^OC_RUN_SERVICES=//p"'
}
services0=$(running opencloud-0)
services1=$(running opencloud-1)
echo "     opencloud-0 runs: $services0"
echo "     opencloud-1 runs: $services1"
if [ "$services0" != "storage-users,storage-system,search,ocm" ]; then
  fail "opencloud-0 should run storage-users,storage-system,search,ocm"
fi
if [ "$services1" != "storage-users,storage-system" ]; then
  fail "opencloud-1 should run storage-users,storage-system only"
fi
echo "ok   search and ocm on opencloud-0 only"
settle

with_opencloud python3 "$e2e/check.py" write shared
with_opencloud python3 "$shared/check_shared.py" write one

step "3a. replace storage pod opencloud-1"
replace_pod opencloud-1
with_opencloud python3 "$shared/check_shared.py" verify one
with_opencloud python3 "$shared/check_shared.py" write two

step "3b. replace storage pod opencloud-0 (search, ocm)"
replace_pod opencloud-0
with_opencloud python3 "$shared/check_shared.py" verify one
with_opencloud python3 "$shared/check_shared.py" verify two
with_opencloud python3 "$e2e/check.py" read shared

step "4. scale the storage pods 2 -> 1 -> 2 with helm upgrade (shared mode)"
claim_uid=$(kubectl -n oc get pvc opencloud-data -o jsonpath='{.metadata.uid}')
install --set storagePods.replicas=1
[ "$(kubectl -n oc get statefulset opencloud -o jsonpath='{.status.readyReplicas}')" = 1 ] || fail "not scaled down to one storage pod"
settle
echo "ok   one storage pod"
with_opencloud python3 "$shared/check_shared.py" verify one
with_opencloud python3 "$shared/check_shared.py" write three
install --set storagePods.replicas=2
[ "$(kubectl -n oc get statefulset opencloud -o jsonpath='{.status.readyReplicas}')" = 2 ] || fail "not scaled up to two storage pods"
settle
echo "ok   two storage pods again"
if [ "$(kubectl -n oc get pvc opencloud-data -o jsonpath='{.metadata.uid}')" != "$claim_uid" ]; then
  fail "the data claim was replaced"
fi
echo "ok   same data claim throughout"
for marker in one two three; do
  with_opencloud python3 "$shared/check_shared.py" verify "$marker"
done

step "all shared-storage checks passed (driver $DRIVER, NFS ${NFS_SERVER:-in-cluster})"
