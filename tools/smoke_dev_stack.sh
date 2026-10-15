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

# The console serves the app and proxies the API on its own origin, with its security headers.
headers=$(curl -fsS -D - -o /dev/null http://127.0.0.1:3000/sites)
grep -qi "^content-security-policy: default-src 'self'" <<<"$headers" || { echo "console: no CSP" >&2; exit 1; }
grep -qi "^x-frame-options: DENY" <<<"$headers" || { echo "console: no X-Frame-Options" >&2; exit 1; }
echo "console via its proxy:   $(fetch http://127.0.0.1:3000/v1/meta | head -c 80)..."

# The whole pilot: onboard, enroll, prepare, approve locally, serve robots, survive an outage.
${UV:-uv} run python "$(dirname "$0")/smoke_api.py"
echo "dev stack smoke test passed"
