#!/usr/bin/env bash
# The GPU-sharing benchmark (design §9.5, §10, §16 step 0) on one NVIDIA L4 in Google Cloud: a
# Spot VM that runs `neura-bench run --preset gpu` on this commit and is deleted after, whatever
# happens. The report lands in bench-report/ (README.md and results.json).
#
#   deploy/gcp/bench.sh quota     can this project run an L4? (and how to ask for one)
#   deploy/gcp/bench.sh quota-request
#                                 ask Google for the GPUs-in-all-regions quota a run needs, 1
#                                 (free; Google decides, in minutes to days), and say where it is
#   deploy/gcp/bench.sh run       quota, a bucket for the source and report, the VM in the first
#                                 zone with an L4 free, the benchmark, the report, the VM deleted
#   deploy/gcp/bench.sh status    the benchmark's VMs and their progress
#   deploy/gcp/bench.sh delete    delete every benchmark VM (run does it too)
#
# Settings (environment), besides deploy.sh's GCP_SA_KEY and NEURA_GCP_PROJECT:
#   NEURA_BENCH_ZONES     zones to try in order (default: the L4 zones of us-central1, us-east4,
#                         us-east1, us-west1, us-west4)
#   NEURA_BENCH_MACHINE   default g2-standard-8: one L4 (24 GB), 8 vCPUs and 32 GB of memory
#                         (building pi0 takes about 14 GB)
#   NEURA_BENCH_SPOT      true (default): a Spot VM, a fraction of the price, which Google can
#                         take back (then run again); false: a standard VM
#   NEURA_BENCH_HOURS     the VM is deleted after this many hours, finished or not (default 3)
#   NEURA_BENCH_SIZE      the models' size (default base)
#
# Costs, with the defaults: about $0.10 to $0.35 an hour on Spot, $0.85 standard (the L4 VM and
# its 100 GB disk); a run takes about 1.5 hours. The VM is deleted when the run ends, and by its
# maximum run time if this script is stopped first.
set -euo pipefail

case "${1:-}" in
quota | quota-request | run | status | delete) ;;
*)
    sed -n '2,/^set -euo/{/^set -euo/!p}' "$0"
    exit 2
    ;;
esac

cd "$(dirname "$0")/../.."

CLEANUP=()
trap '[[ ${#CLEANUP[@]} -eq 0 ]] || rm -rf "${CLEANUP[@]}"' EXIT

if [[ -n ${GCP_SA_KEY:-} ]]; then
    key_file=$(mktemp)
    CLEANUP+=("$key_file")
    (umask 077 && printf '%s' "$GCP_SA_KEY" >"$key_file")
    gcloud auth activate-service-account --key-file="$key_file" --quiet >/dev/null
    : "${NEURA_GCP_PROJECT:=$(python3 -c \
        'import json, sys; print(json.load(open(sys.argv[1]))["project_id"])' "$key_file")}"
    rm -f "$key_file"
fi
PROJECT=${NEURA_GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}
[[ -n $PROJECT ]] || {
    echo "set NEURA_GCP_PROJECT (or gcloud config set project ...)" >&2
    exit 2
}

ZONES=${NEURA_BENCH_ZONES:-us-central1-a us-central1-b us-central1-c us-east4-a us-east4-c us-east1-b us-east1-c us-west1-a us-west1-b us-west4-a}
MACHINE=${NEURA_BENCH_MACHINE:-g2-standard-8}
SPOT=${NEURA_BENCH_SPOT:-true}
HOURS=${NEURA_BENCH_HOURS:-3}
SIZE=${NEURA_BENCH_SIZE:-base}
BUCKET=gs://$PROJECT-neura-bench
NETWORK=neura-bench
SA_NAME=neura-bench
SA=$SA_NAME@$PROJECT.iam.gserviceaccount.com
OUT=bench-report

g() { gcloud --project "$PROJECT" --quiet "$@"; }
step() { echo "--> $*" >&2; }
exists() { g "$@" >/dev/null 2>&1; }
bind() {
    local attempt
    for attempt in 1 2 3 4 5 6; do
        g "$@" >/dev/null && return 0
        [[ $attempt == 6 ]] || sleep 10
    done
    return 1
}
regions() { tr ' ' '\n' <<<"$ZONES" | sed 's/-[a-z]$//' | awk '!seen[$0]++'; }

# The L4 quota of a region (Spot or standard), and the project's quota for GPUs anywhere.
quota_of() {
    local region=$1 metric=$2
    g compute regions describe "$region" --format=json |
        python3 -c 'import json, sys; m = sys.argv[1]
print(int(next((q["limit"] for q in json.load(sys.stdin)["quotas"] if q["metric"] == m), 0)))' "$metric"
}
global_gpus() {
    g compute project-info describe --format=json |
        python3 -c 'import json, sys
print(int(next((q["limit"] for q in json.load(sys.stdin)["quotas"] if q["metric"] == "GPUS_ALL_REGIONS"), 0)))'
}
metric() { [[ $SPOT == true ]] && echo PREEMPTIBLE_NVIDIA_L4_GPUS || echo NVIDIA_L4_GPUS; }

# Prints the zones whose region has an L4 to give; fails, saying how to ask, when none has.
usable_zones() {
    local all region limit usable=()
    all=$(global_gpus)
    declare -A region_ok=()
    for region in $(regions); do
        limit=$(quota_of "$region" "$(metric)")
        echo "    $region: $(metric) $limit" >&2
        [[ $limit -lt 1 ]] || region_ok[$region]=1
    done
    echo "    all regions: GPUS_ALL_REGIONS $all" >&2
    if [[ $all -ge 1 ]]; then
        local zone
        for zone in $ZONES; do
            [[ -z ${region_ok[${zone%-*}]:-} ]] || usable+=("$zone")
        done
    fi
    if [[ ${#usable[@]} -eq 0 ]]; then
        cat >&2 <<EOF

This project cannot run an L4 yet: its GPU quota is 0. Ask for 1 (free; approval takes from minutes
to a couple of days):
  console.cloud.google.com → IAM & Admin → Quotas & System Limits, project $PROJECT
  - "GPUs (all regions)" (GPUS_ALL_REGIONS): 1
  - "$(metric)" in us-central1 (or another region above): 1
Then run this again. A billing account in free trial cannot use GPUs.
EOF
        return 3
    fi
    echo "${usable[@]}"
}

# Asks for GPUS_ALL_REGIONS = 1 through the Cloud Quotas API (new projects have 0, which no
# regional L4 quota overrides), then prints what Google has decided so far. Asking again only
# reads the request back.
request_quota() {
    step "asking for 1 GPU in all regions (GPUS_ALL_REGIONS)"
    g services enable cloudquotas.googleapis.com
    local api=https://cloudquotas.googleapis.com/v1/projects/$PROJECT/locations/global/quotaPreferences
    local id=neura-bench-gpus-all-regions answer token status
    answer=$(mktemp)
    CLEANUP+=("$answer")
    token=$(gcloud auth print-access-token)
    status=$(curl -sS -o "$answer" -w '%{http_code}' -X POST "$api?quotaPreferenceId=$id" \
        -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
        -d '{"service": "compute.googleapis.com", "quotaId": "GPUS-ALL-REGIONS-per-project",
             "quotaConfig": {"preferredValue": "1"},
             "justification": "One NVIDIA L4 Spot VM for a few hours, deleted after: a GPU inference benchmark."}')
    if [[ $status == 409 ]]; then
        echo "    asked before; reading the request back" >&2
        status=$(curl -sS -o "$answer" -w '%{http_code}' "$api/$id" -H "Authorization: Bearer $token")
    fi
    if [[ $status != 200 ]]; then
        echo "the Cloud Quotas API answered $status:" >&2
        cat "$answer" >&2
        return 1
    fi
    python3 - "$answer" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
c = p.get("quotaConfig", {})
print(f"    asked for {c.get('preferredValue')}; granted so far {c.get('grantedValue', 'nothing')}")
print(f"    still being decided: {'yes' if p.get('reconciling') else 'no'}")
if c.get("stateDetail"):
    print(f"    Google says: {c['stateDetail']}")
PY
}

# The newest Deep Learning VM image family with NVIDIA's 580 driver (CUDA 13, which the locked
# PyTorch is built for); Ubuntu 22.04.
image_family() {
    local family
    family=$(g compute images list --project=deeplearning-platform-release --no-standard-images \
        --filter="family~^common-cu.*-ubuntu-2204-nvidia-580$" --format='value(family)' 2>/dev/null |
        sort -uV | tail -n 1 || true)
    echo "${family:-common-cu129-ubuntu-2204-nvidia-580}"
}

setup() {
    step "APIs, the benchmark's network (outbound only), bucket and service account"
    g services enable compute.googleapis.com storage.googleapis.com
    exists compute networks describe "$NETWORK" ||
        g compute networks create "$NETWORK" --subnet-mode=auto
    exists storage buckets describe "$BUCKET" ||
        g storage buckets create "$BUCKET" --location=us-central1 --uniform-bucket-level-access
    exists iam service-accounts describe "$SA" ||
        g iam service-accounts create "$SA_NAME" --display-name="Neura GPU benchmark"
    bind projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" \
        --role=roles/logging.logWriter --condition=None
    bind storage buckets add-iam-policy-binding "$BUCKET" --member="serviceAccount:$SA" \
        --role=roles/storage.objectAdmin
}

startup_script() {
    cat <<'EOF'
#!/bin/bash
# The GPU benchmark's VM (deploy/gcp/bench.sh): this commit's source from the bucket, the models
# group, `neura-bench run --preset gpu`, the report and the outcome back to the bucket. Once.
# The VM stays up: bench.sh deletes it when it sees the outcome, and its maximum run time deletes
# it otherwise (a stopped VM would keep its disk, and its bill).
set -uo pipefail
MD=http://metadata.google.internal/computeMetadata/v1/instance
attr() { curl -fsS -H Metadata-Flavor:Google "$MD/attributes/$1"; }
report() {
    curl -fsS -X PUT --data "$2" -H Metadata-Flavor:Google \
        "$MD/guest-attributes/neura-bench/$1" >/dev/null || true
}
[[ ! -e /var/neura-bench.started ]] || exit 0
touch /var/neura-bench.started
RUN=$(attr neura-bench-run)
SIZE=$(attr neura-bench-size)
DEST=$(attr neura-bench-bucket)/runs/$RUN
WORK=/opt/neura-bench
LOG=$WORK/bench.log
mkdir -p "$WORK/src"
cd "$WORK" || exit 1
exec >>"$LOG" 2>&1
finish() {
    report state "$1"
    gcloud storage cp "$LOG" "$DEST/bench.log" --quiet || true
    echo "$1" | gcloud storage cp - "$DEST/outcome" --quiet || true
    exit 0
}
report state preparing
for _ in $(seq 60); do nvidia-smi >/dev/null 2>&1 && break; sleep 10; done
nvidia-smi || finish "failed: no GPU driver"
gcloud storage cp "$DEST/src.tar.gz" src.tar.gz --quiet || finish "failed: no source"
tar -xzf src.tar.gz -C src || finish "failed: unpacking the source"
curl -LsSf https://astral.sh/uv/0.11.32/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh ||
    finish "failed: uv"
cd src || finish "failed: no source"
export HOME=/root UV_PYTHON=3.13 HF_HOME=$WORK/hf MUJOCO_GL=egl
report state installing
uv sync --frozen --group models || finish "failed: install"
# The log's last line, every half minute, for whoever watches; the log itself every 5 minutes.
( while sleep 30; do
    report progress "$(tail -n 1 "$LOG" | cut -c1-400)"
    (( SECONDS % 300 < 30 )) && gcloud storage cp "$LOG" "$DEST/bench.log" --quiet >/dev/null 2>&1
done ) &
report state running
if uv run --group models neura-bench run --preset gpu --device cuda:0 --size "$SIZE" \
    --out "$WORK/report"; then
    gcloud storage cp -r "$WORK/report" "$DEST/" --quiet
    finish "done"
else
    finish "failed: the benchmark exited with $?"
fi
EOF
}

vms() { g compute instances list --filter="labels.neura-bench=true" --format='value(name,zone.basename(),status)'; }

delete_all() {
    local name zone _
    while read -r name zone _; do
        [[ -n $name ]] || continue
        step "deleting $name ($zone)"
        g compute instances delete "$name" --zone "$zone" --delete-disks=all || true
    done < <(vms)
}

run() {
    local zones run vm zone="" family startup src created=""
    setup
    step "GPU quota ($(metric), for a $([[ $SPOT == true ]] && echo Spot || echo standard) VM)"
    zones=$(usable_zones)
    run=$(date -u +%Y%m%d-%H%M%S)-$(git rev-parse --short=8 HEAD)
    vm=neura-bench-${run,,}
    vm=${vm:0:62}
    step "this commit's source to $BUCKET/runs/$run"
    src=$(mktemp)
    CLEANUP+=("$src")
    git archive --format=tar.gz -o "$src" HEAD
    g storage cp "$src" "$BUCKET/runs/$run/src.tar.gz" >/dev/null
    startup=$(mktemp)
    CLEANUP+=("$startup")
    startup_script >"$startup"
    family=$(image_family)
    local model=() kind=standard
    [[ $SPOT != true ]] || {
        model=(--provisioning-model=SPOT)
        kind=Spot
    }
    trap 'delete_all; [[ ${#CLEANUP[@]} -eq 0 ]] || rm -rf "${CLEANUP[@]}"' EXIT
    for zone in $zones; do
        step "creating $vm ($MACHINE, one L4, $kind, image $family) in $zone; deleted after ${HOURS}h"
        if g compute instances create "$vm" --zone "$zone" --machine-type="$MACHINE" \
            --network="$NETWORK" --image-family="$family" --image-project=deeplearning-platform-release \
            --boot-disk-size=100GB --boot-disk-type=pd-balanced --maintenance-policy=TERMINATE \
            "${model[@]}" --instance-termination-action=DELETE --max-run-duration="${HOURS}h" \
            --service-account="$SA" --scopes=cloud-platform --labels=neura-bench=true \
            --metadata="enable-guest-attributes=TRUE,install-nvidia-driver=True,neura-bench-run=$run,neura-bench-size=$SIZE,neura-bench-bucket=$BUCKET" \
            --metadata-from-file="startup-script=$startup" 2>&1 | tail -n 3 >&2; then
            if exists compute instances describe "$vm" --zone "$zone"; then
                created=$zone
                break
            fi
        fi
        echo "    no L4 in $zone now; the next zone" >&2
    done
    [[ -n $created ]] || {
        echo "no zone had an L4 to give: run again later, or set NEURA_BENCH_ZONES" >&2
        return 1
    }
    step "the benchmark runs (about 1.5 hours); its progress every minute"
    local deadline=$((SECONDS + HOURS * 3600)) state="" outcome="" progress last=""
    while ((SECONDS < deadline)); do
        sleep 60
        outcome=$(g storage cat "$BUCKET/runs/$run/outcome" 2>/dev/null || true)
        [[ -z $outcome ]] || break
        if [[ $(g compute instances describe "$vm" --zone "$created" --format='value(status)' 2>/dev/null) != RUNNING ]]; then
            outcome="failed: the VM stopped or is gone (Spot VMs can be taken back): run again"
            break
        fi
        state=$(g compute instances get-guest-attributes "$vm" --zone "$created" \
            --query-path=neura-bench/state --format='value(value)' 2>/dev/null || true)
        progress=$(g compute instances get-guest-attributes "$vm" --zone "$created" \
            --query-path=neura-bench/progress --format='value(value)' 2>/dev/null || true)
        [[ -z $progress || $progress == "$last" ]] || echo "    [$state] $progress" >&2
        last=$progress
    done
    rm -rf "$OUT"
    mkdir -p "$OUT"
    g storage cp "$BUCKET/runs/$run/bench.log" "$OUT/bench.log" >/dev/null 2>&1 || true
    if [[ $outcome != "done" ]]; then
        echo "the benchmark did not finish: ${outcome:-no outcome after ${HOURS}h}; the end of its log:" >&2
        tail -n 80 "$OUT/bench.log" >&2 2>/dev/null || true
        return 1
    fi
    g storage cp -r "$BUCKET/runs/$run/report/*" "$OUT/" >/dev/null
    step "the report: $OUT/README.md"
    cat "$OUT/README.md"
}

case "$1" in
quota)
    usable_zones >/dev/null && echo "this project can run an L4 ($(metric))"
    ;;
quota-request) request_quota ;;
run) run ;;
status)
    vms
    ;;
delete) delete_all ;;
esac
