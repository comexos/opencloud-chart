#!/usr/bin/env bash
# Lints the chart and renders each configuration in ci/ plus the example.
# Usage: scripts/render-matrix.sh [OUTDIR]   (default: ./rendered)
set -euo pipefail

chart="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$chart/rendered}"
mkdir -p "$out"

# The Bitnami common archive is vendored in charts/ and must match Chart.lock;
# checking its status avoids a registry pull on every run.
helm dependency list "$chart" | tail -n +2 | awk 'NF && $NF != "ok" { bad = 1 } END { exit bad }' \
  || { echo "charts/ does not match Chart.lock; run: helm dependency build" >&2; exit 1; }
helm lint --strict "$chart" -f "$chart/examples/keycloak-s3.yaml"

declare -A matrix=(
  [example]="-f $chart/examples/keycloak-s3.yaml"
  [full]="-f $chart/ci/full.yaml"
  [builtin]="-f $chart/ci/builtin.yaml"
  [external-secrets]="-f $chart/ci/external-secrets.yaml"
)
for name in "${!matrix[@]}"; do
  # shellcheck disable=SC2086  # word splitting of the -f argument is intended
  helm template opencloud "$chart" --namespace opencloud ${matrix[$name]} > "$out/$name.yaml"
  echo "rendered $name"
done
