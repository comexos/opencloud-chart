#!/usr/bin/env bash
# Generate README.md from README.md.gotmpl and values.yaml descriptions.
# --check compares without modifying the checkout.
set -euo pipefail

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--check" ]; }; then
  echo "Usage: $0 [--check]" >&2
  exit 2
fi

chart="$(cd "$(dirname "$0")/.." && pwd)"
image=docker.io/jnorwood/helm-docs@sha256:7e562b49ab6b1dbc50c3da8f2dd6ffa8a5c6bba327b1c6335cc15ce29267979c

generated="$(mktemp)"
trap 'rm -f "$generated"' EXIT

# Render only this chart, with no container writes or root-owned output files.
docker run --rm -v "$chart:/chart:ro" -w /chart --entrypoint helm-docs "$image" \
  --chart-search-root . --chart-to-generate . --dry-run --log-level error > "$generated"

if [ "${1:-}" = "--check" ]; then
  if ! cmp -s "$generated" "$chart/README.md"; then
    echo "README.md is out of date. Run scripts/generate-docs.sh and commit it." >&2
    diff -u "$chart/README.md" "$generated" >&2 || true
    exit 1
  fi
else
  cat "$generated" > "$chart/README.md"
fi
