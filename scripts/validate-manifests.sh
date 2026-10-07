#!/usr/bin/env bash
# Checks rendered manifests against the Kubernetes schema (kubeconform) and the
# Kyverno policies in policies/kyverno, then proves the policies still reject
# tests/policy/insecure-workload.yaml. Tools run from digest-pinned images.
# Usage: scripts/validate-manifests.sh [RENDERED_DIR]   (default: ./rendered)
set -euo pipefail

chart="$(cd "$(dirname "$0")/.." && pwd)"
rendered="${1:-$chart/rendered}"

KYVERNO=ghcr.io/kyverno/kyverno-cli@sha256:ced7b2be0b04250cabfe695f15307f69eb715fe23234816388af4f3812915b2a
KUBECONFORM=ghcr.io/yannh/kubeconform@sha256:faffaf43f95aa6425306e1ab8d6fcad72acb9049158f38e574c085ea1ec0f64e

kyverno() {
  docker run --rm -v "$chart/policies/kyverno:/policies:ro" -v "$chart/tests/policy:/tests:ro" \
    -v "$(realpath "$rendered"):/rendered:ro" "$KYVERNO" "$@"
}

status=0
for file in "$rendered"/*.yaml; do
  name="$(basename "$file")"
  if kyverno apply /policies --resource "/rendered/$name" >/dev/null 2>&1; then
    echo "policies: pass  $name"
  else
    echo "policies: FAIL  $name" >&2
    kyverno apply /policies --resource "/rendered/$name" >&2 || true
    status=1
  fi
done

if kyverno apply /policies --resource /tests/insecure-workload.yaml >/dev/null 2>&1; then
  echo "policies: FAIL  insecure-workload.yaml was accepted; the policies have stopped working" >&2
  status=1
else
  echo "policies: pass  insecure-workload.yaml is still rejected"
fi

mapfile -t files < <(basename -a "$rendered"/*.yaml)
docker run --rm -v "$(realpath "$rendered"):/rendered:ro" "$KUBECONFORM" \
  -strict -summary -ignore-missing-schemas -kubernetes-version 1.37.0 \
  "${files[@]/#//rendered/}" || status=1

exit "$status"
