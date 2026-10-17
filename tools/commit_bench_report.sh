#!/usr/bin/env bash
# Commits a GPU benchmark report (README.md and results.json) to the bench-reports branch, under
# bench/reports/<device>/, from the GPU benchmark workflows (.github/workflows/bench-*.yml).
#
#   tools/commit_bench_report.sh [REPORT_DIR]    default bench-report
set -euo pipefail

report=${1:-bench-report}
label=$(python3 - "$report/results.json" <<'PY'
import json, re, sys
name = json.load(open(sys.argv[1]))["environment"]["device_name"]
name = re.sub(r"\(r\)|\(tm\)|cpu @.*|@.*", "", name.lower())
print(re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:60])
PY
)
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
if git fetch --depth 1 origin bench-reports 2>/dev/null; then
    git worktree add reports FETCH_HEAD
    git -C reports switch -c bench-reports
else
    git worktree add --orphan -b bench-reports reports
fi
mkdir -p "reports/bench/reports/$label"
cp "$report/README.md" "$report/results.json" "reports/bench/reports/$label/"
git -C reports add bench
git -C reports commit -q -m "GPU benchmark report: $label, from $(git rev-parse --short HEAD)"
git -C reports push origin bench-reports
echo "Committed to bench-reports: bench/reports/$label"
