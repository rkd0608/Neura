#!/usr/bin/env bash
# A customer's site in Google Cloud, for staging (design §20.12): the site kit (deploy/site-demo) on
# its own VM, in its own network and region, connected over the internet to the Neura that
# deploy.sh deployed. Like a customer's site behind its firewall, it only dials out: nothing
# reaches it from the internet, and SSH comes only through Identity-Aware Proxy.
#
#   deploy/gcp/site.sh up          push the kit's image, create the site's network and server
#   deploy/gcp/site.sh test        the customer's whole flow against the deployed Neura, then real
#                                  network faults on the site's uplink (tools/staging_site_test.py)
#   deploy/gcp/site.sh ssh [CMD]   a shell on the site's server; the kit is in /opt/neura-site
#   deploy/gcp/site.sh page        how to watch the robots from Cloud Shell
#   deploy/gcp/site.sh status      the server, and what its site agent reports
#   deploy/gcp/site.sh stop        stop the server (its disk and enrollment are kept)
#   deploy/gcp/site.sh delete      delete the server and its disk (remove it in the console too)
#
# Settings (environment), besides deploy.sh's GCP_SA_KEY and NEURA_GCP_PROJECT:
#   NEURA_GCP_REGION      Neura's region (default us-central1), for its registry and address
#   NEURA_SITE_REGION     the site's region (default us-east1), another than Neura's
#   NEURA_SITE_ZONE       default <site region>-b
#   NEURA_SITE_MACHINE    default e2-standard-8: the model runs on its 8 vCPUs
#   NEURA_SITE_SPOT       true (default): a Spot VM, about a quarter of the price, which Google
#                         can stop at any time (as a site can lose power); false: a standard VM
#   NEURA_SITE_HOURS      the server stops itself after this many hours (default 4)
#
# Costs while running, with the defaults: about $0.07 an hour (Spot e2-standard-8, its address
# and its 40 GB disk); stopped, the disk alone, about $4 a month; deleted, nothing.
set -euo pipefail

case "${1:-}" in
up | test | ssh | page | status | stop | delete) ;;
*)
    sed -n '2,/^set -euo/{/^set -euo/!p}' "$0"
    exit 2
    ;;
esac

cd "$(dirname "$0")/../.."

CLEANUP=()
trap '[[ ${#CLEANUP[@]} -eq 0 ]] || rm -rf "${CLEANUP[@]}"' EXIT
scratch() {
    local name=$1
    shift
    printf -v "$name" '%s' "$(mktemp "$@")"
    CLEANUP+=("${!name}")
}

# Credentials as in deploy.sh: the signed-in account, or the service account in GCP_SA_KEY.
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

REGION=${NEURA_GCP_REGION:-us-central1}
NEURA_ZONE=${NEURA_GCP_ZONE:-$REGION-a}
SITE_REGION=${NEURA_SITE_REGION:-us-east1}
SITE_ZONE=${NEURA_SITE_ZONE:-$SITE_REGION-b}
MACHINE=${NEURA_SITE_MACHINE:-e2-standard-8}
SPOT=${NEURA_SITE_SPOT:-true}
HOURS=${NEURA_SITE_HOURS:-4}

VM=neura-site-1
NETWORK=neura-site
SA_NAME=neura-site
SA=$SA_NAME@$PROJECT.iam.gserviceaccount.com
REGISTRY_HOST=$REGION-docker.pkg.dev
REGISTRY=$REGISTRY_HOST/$PROJECT/neura
KIT=/opt/neura-site

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
on_site() { g compute ssh "$VM" --zone "$SITE_ZONE" --tunnel-through-iap --command="$*"; }
guest() {
    g compute instances get-guest-attributes "$VM" --zone "$SITE_ZONE" \
        --query-path="neura-site/$1" --format='value(value)' 2>/dev/null || true
}

# The Neura this site connects to: the domain deploy.sh released.
neura_domain() {
    local domain
    domain=$(g compute instances describe neura --zone "$NEURA_ZONE" \
        --format='value(metadata.items.neura-config)' 2>/dev/null | sed -n 's/^DOMAIN=//p' || true)
    [[ -n $domain ]] || {
        echo "no Neura deployed in $PROJECT ($NEURA_ZONE): run deploy/gcp/deploy.sh up first" >&2
        exit 1
    }
    echo "$domain"
}

network() {
    step "the site's network $NETWORK in $SITE_REGION: no inbound traffic but SSH through IAP"
    g services enable compute.googleapis.com iap.googleapis.com artifactregistry.googleapis.com
    exists compute networks describe "$NETWORK" ||
        g compute networks create "$NETWORK" --subnet-mode=custom
    exists compute networks subnets describe "$NETWORK-$SITE_REGION" --region "$SITE_REGION" ||
        g compute networks subnets create "$NETWORK-$SITE_REGION" --network="$NETWORK" \
            --region="$SITE_REGION" --range=10.70.0.0/24
    exists compute firewall-rules describe "$NETWORK-iap-ssh" ||
        g compute firewall-rules create "$NETWORK-iap-ssh" --network="$NETWORK" \
            --direction=INGRESS --allow=tcp:22 --source-ranges=35.235.240.0/20 \
            --target-tags=neura-site

    step "the site's service account: it pulls the kit's image, nothing else"
    exists iam service-accounts describe "$SA" ||
        g iam service-accounts create "$SA_NAME" --display-name="Neura staging site"
    bind projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" \
        --role=roles/logging.logWriter --condition=None
    bind artifacts repositories add-iam-policy-binding neura --location "$REGION" \
        --member="serviceAccount:$SA" --role=roles/artifactregistry.reader
}

push_image() {
    local tag secret=()
    tag=$(git rev-parse --short=12 HEAD)
    [[ -z $(git status --porcelain) ]] || tag="$tag-dirty-$(date -u +%Y%m%d%H%M%S)"
    g auth configure-docker "$REGISTRY_HOST" >/dev/null 2>&1
    [[ -z ${NEURA_BUILD_CA_BUNDLE:-} ]] || secret=(--secret "id=ca_bundle,src=$NEURA_BUILD_CA_BUNDLE")
    step "building neura-site-kit:$tag (CPU PyTorch, about 2 GB)"
    docker build --quiet --platform=linux/amd64 -f deploy/docker/Dockerfile --target neura-site-kit \
        ${secret[@]+"${secret[@]}"} -t "$REGISTRY/neura-site-kit:$tag" . >/dev/null
    docker push --quiet "$REGISTRY/neura-site-kit:$tag" >/dev/null
    docker image inspect --format '{{index .RepoDigests 0}}' "$REGISTRY/neura-site-kit:$tag"
}

startup_script() {
    cat <<'EOF'
#!/bin/bash
# The staging site's server (deploy/gcp/site.sh): Docker, and the site kit in /opt/neura-site with
# its image pinned by the metadata. On every boot; the containers restart with Docker.
set -euo pipefail
MD=http://metadata.google.internal/computeMetadata/v1/instance
attr() { curl -fsS -H Metadata-Flavor:Google "$MD/attributes/$1"; }
report() {
    curl -fsS -X PUT --data "$2" -H Metadata-Flavor:Google \
        "$MD/guest-attributes/neura-site/$1" >/dev/null || true
}
report state installing
if ! docker compose version >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -yq ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
        "https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
KIT=/opt/neura-site
install -d -m 0755 "$KIT"
attr neura-site-compose >"$KIT/compose.yaml"
attr neura-site-compose-image >"$KIT/compose.image.yaml"
attr neura-site-sh >"$KIT/site.sh"
attr neura-site-readme >"$KIT/README.md"
chmod 0755 "$KIT/site.sh"
: >"$KIT/empty-ca-bundle.pem"
image=$(attr neura-site-image)
# .env: the image and compose files here; site.sh keeps its own values next to them.
set_env() {
    umask 077
    touch "$KIT/.env"
    { grep -v "^$1=" "$KIT/.env" || true; } >"$KIT/.env.tmp"
    echo "$1=$2" >>"$KIT/.env.tmp"
    mv "$KIT/.env.tmp" "$KIT/.env"
}
set_env NEURA_KIT_IMAGE "$image"
set_env COMPOSE_FILE compose.yaml:compose.image.yaml
set_env NEURA_CELL_ALLOW_HOSTS .cloudshell.dev
set_env NEURA_SERVER_NAME "$(hostname)"
registry=${image%%/*}
gcloud auth configure-docker "$registry" --quiet >/dev/null 2>&1
report state pulling
docker pull --quiet "$image" >/dev/null
report image "$image"
report state ready
EOF
}

up() {
    local domain image startup
    domain=$(neura_domain)
    network
    image=$(push_image)
    scratch startup
    startup_script >"$startup"
    local files="startup-script=$startup,neura-site-compose=deploy/site-demo/compose.yaml"
    files+=",neura-site-compose-image=deploy/site-demo/compose.image.yaml"
    files+=",neura-site-sh=deploy/site-demo/site.sh,neura-site-readme=deploy/site-demo/README.md"
    if exists compute instances describe "$VM" --zone "$SITE_ZONE"; then
        step "updating $VM to $image"
        g compute instances add-metadata "$VM" --zone "$SITE_ZONE" \
            --metadata="neura-site-image=$image" --metadata-from-file="$files"
        if [[ $(g compute instances describe "$VM" --zone "$SITE_ZONE" --format='value(status)') == RUNNING ]]; then
            on_site "sudo google_metadata_script_runner startup" >/dev/null
        else
            g compute instances start "$VM" --zone "$SITE_ZONE"
        fi
    else
        local model=()
        [[ $SPOT != true ]] || model=(--provisioning-model=SPOT)
        step "creating $VM ($MACHINE${SPOT:+, spot}) in $SITE_ZONE; it stops itself after ${HOURS}h"
        g compute instances create "$VM" --zone "$SITE_ZONE" --machine-type="$MACHINE" \
            --subnet="$NETWORK-$SITE_REGION" --tags=neura-site \
            --image-family=debian-12 --image-project=debian-cloud \
            --boot-disk-size=40GB --boot-disk-type=pd-balanced \
            "${model[@]}" --instance-termination-action=STOP --max-run-duration="${HOURS}h" \
            --service-account="$SA" --scopes=cloud-platform \
            --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
            --metadata="enable-oslogin=TRUE,enable-guest-attributes=TRUE,neura-site-image=$image" \
            --metadata-from-file="$files"
    fi
    step "waiting for the server to install Docker and pull the kit"
    local _
    for _ in $(seq 90); do
        [[ $(guest state) == ready && $(guest image) == "$image" ]] && break
        sleep 10
    done
    [[ $(guest state) == ready ]] || {
        echo "the site's server is not ready after 15 minutes; its boot output:" >&2
        g compute instances get-serial-port-output "$VM" --zone "$SITE_ZONE" 2>/dev/null |
            grep startup-script | tail -n 60 >&2 || true
        return 1
    }
    cat <<EOF

The staging site's server is ready: $VM in $SITE_ZONE, with the kit in $KIT, connecting to
https://console.$domain. It stops itself in ${HOURS}h (deploy/gcp/site.sh up starts it again).

  The whole flow, automated:   deploy/gcp/site.sh test   (or Actions → Staging site → test)
  As the customer, by hand:    in Cloud Shell, gcloud compute ssh $VM --zone $SITE_ZONE --tunnel-through-iap
                               then: sudo $KIT/site.sh publish 'https://console.$domain/o/<org>/p/<project>'
                               and the steps of $KIT/README.md.
EOF
}

case "${1:-}" in
up) up ;;
test)
    shift
    domain=$(neura_domain)
    # The admin API token, for the API calls a customer makes in the console; never printed.
    token=$(g secrets versions access latest --secret=neura-admin-token)
    [[ -z ${GITHUB_ACTIONS:-} ]] || echo "::add-mask::$token"
    address=$(g compute addresses describe neura-ip --region "$REGION" --format='value(address)')
    NEURA_TOKEN=$token uv run python tools/staging_site_test.py --url "https://console.$domain" \
        --neura-address "$address" --project-id "$PROJECT" --vm "$VM" --zone "$SITE_ZONE" "$@"
    ;;
ssh)
    shift
    g compute ssh "$VM" --zone "$SITE_ZONE" --tunnel-through-iap ${1:+--command="$*"}
    ;;
page)
    cat <<EOF
In Cloud Shell (console.cloud.google.com, the >_ button), forward the cell's page:
  gcloud compute ssh $VM --project $PROJECT --zone $SITE_ZONE --tunnel-through-iap -- -N -L 8080:127.0.0.1:8095
then Web Preview → Preview on port 8080.
EOF
    ;;
status)
    g compute instances describe "$VM" --zone "$SITE_ZONE" \
        --format='value(name,status,machineType.basename(),scheduling.provisioningModel)'
    [[ $(g compute instances describe "$VM" --zone "$SITE_ZONE" --format='value(status)') != RUNNING ]] ||
        on_site "sudo $KIT/site.sh status 2>/dev/null || echo 'the site is not connected yet'"
    ;;
stop) g compute instances stop "$VM" --zone "$SITE_ZONE" ;;
delete) g compute instances delete "$VM" --zone "$SITE_ZONE" --delete-disks=all ;;
esac
