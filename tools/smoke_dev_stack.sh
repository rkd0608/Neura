#!/usr/bin/env bash
# Bring up the dev stack, verify each service end to end, then tear it down.
# Usage: tools/smoke_dev_stack.sh [--keep]
# Set NEURA_DEV_NO_BUILD=1 to use already-built neura-*:dev images.
set -euo pipefail

COMPOSE=(docker compose -f "$(dirname "$0")/../deploy/compose/compose.yaml")
keep=false
[[ "${1:-}" == "--keep" ]] && keep=true

cleanup() {
  if [[ "$keep" == false ]]; then
    "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

build_flag=(--build)
[[ "${NEURA_DEV_NO_BUILD:-0}" == "1" ]] && build_flag=(--no-build)
"${COMPOSE[@]}" up --detach "${build_flag[@]}" --wait --wait-timeout 180

fetch() { python3 -c "import json,sys,urllib.request; print(json.dumps(json.load(urllib.request.urlopen(sys.argv[1], timeout=5))))" "$1"; }

echo "control plane readiness: $(fetch http://127.0.0.1:8080/readyz)"
echo "control plane meta:      $(fetch http://127.0.0.1:8080/v1/meta)"

# The agent probes every 2 s; wait until it reports the control plane reachable.
for _ in $(seq 1 30); do
  status=$(fetch http://127.0.0.1:8090/v1/local/status)
  if python3 -c "import json,sys; sys.exit(0 if json.loads(sys.argv[1])['control_plane']['reachable'] else 1)" "$status"; then
    echo "site agent status:       $status"
    echo "dev stack smoke test passed"
    exit 0
  fi
  sleep 1
done
echo "site agent never reported the control plane reachable: $status" >&2
exit 1
