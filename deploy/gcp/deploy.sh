#!/usr/bin/env bash
# Deploy Neura's cloud side to Google Cloud: one Compute Engine VM running the containers, Cloud SQL
# for Postgres, Secret Manager for the keys, Artifact Registry for the images (design §20.10).
#
#   deploy/gcp/deploy.sh up            create or update everything, then release this commit
#   deploy/gcp/deploy.sh release       build and push the images of this commit, then roll the VM
#   deploy/gcp/deploy.sh admin-token   print the first admin API token (created by `up`)
#   deploy/gcp/deploy.sh status        what is running, and whether the public endpoints answer
#   deploy/gcp/deploy.sh ssh [CMD]     a shell on the VM (through IAP)
#
# Settings (environment):
#   GCP_SA_KEY           a service account's JSON key (its contents), instead of `gcloud auth login`
#   NEURA_GCP_PROJECT    project id (default: the key's project, else gcloud's configured project)
#   NEURA_GCP_REGION     default us-central1;  NEURA_GCP_ZONE  default <region>-a
#   NEURA_DOMAIN         e.g. nucleilabs.ai. Unset: <ip>.sslip.io, which works with no DNS setup.
#                        With your own domain, point <domain>, console., agents. and enroll. at the
#                        address `up` prints before the certificate is requested.
#   NEURA_ACME_EMAIL     Let's Encrypt expiry notices (optional)
#   NEURA_ORGANIZATION   the first organization's name (default NucleiLabs)
#   NEURA_MACHINE_TYPE   default e2-small (the stack uses ~200 MB; e2-micro is in the free tier)
#   NEURA_SQL_TIER       default db-f1-micro
#   NEURA_CONTACT_URL    the site's contact link (default mailto:hello@nucleilabs.ai)
#   NEURA_BUILD_CA_BUNDLE  CA bundle for image builds behind a TLS-intercepting proxy
#
# Re-running is safe: existing resources are kept, and secrets are created once and never rotated
# here (sites pin the manifest signing key and the agent CA at enrollment).
set -euo pipefail

case "${1:-}" in
up | release | admin-token | status | ssh) ;;
*)
    sed -n '2,/^set -euo/{/^set -euo/!p}' "$0"
    exit 2
    ;;
esac

cd "$(dirname "$0")/../.."
HERE=deploy/gcp

for tool in gcloud docker uv openssl; do
    command -v "$tool" >/dev/null || {
        echo "deploy.sh needs $tool on PATH" >&2
        exit 2
    }
done

# Credentials: the account gcloud is signed in with or, when GCP_SA_KEY holds a service account's
# JSON key (a CI secret), that service account. The key goes to gcloud through a private temporary
# file, removed once gcloud has it; the key's project is the default project.
if [[ -n ${GCP_SA_KEY:-} ]]; then
    key_file=$(mktemp)
    trap 'rm -f "$key_file"' EXIT
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
ZONE=${NEURA_GCP_ZONE:-$REGION-a}
MACHINE_TYPE=${NEURA_MACHINE_TYPE:-e2-small}
SQL_TIER=${NEURA_SQL_TIER:-db-f1-micro}
ORGANIZATION=${NEURA_ORGANIZATION:-NucleiLabs}
CONTACT_URL=${NEURA_CONTACT_URL:-mailto:hello@nucleilabs.ai}
DOCS_URL=https://rkd0608.github.io/neura/

VM=neura
SA_NAME=neura-host
SA=$SA_NAME@$PROJECT.iam.gserviceaccount.com
SQL=neura-pg
REPO=neura
REGISTRY_HOST=$REGION-docker.pkg.dev
REGISTRY=$REGISTRY_HOST/$PROJECT/$REPO
SECRETS=(neura-db-password neura-manifest-signing-key neura-agent-ca-cert neura-agent-ca-key)

g() { gcloud --project "$PROJECT" --quiet "$@"; }
step() { echo "--> $*" >&2; }
exists() { g "$@" >/dev/null 2>&1; }

address() { g compute addresses describe "$VM-ip" --region "$REGION" --format='value(address)'; }

domain() {
    if [[ -n ${NEURA_DOMAIN:-} ]]; then
        echo "$NEURA_DOMAIN"
        return
    fi
    # Keep the domain a previous release used, so a release without NEURA_DOMAIN does not move it.
    local current
    current=$(g compute instances describe "$VM" --zone "$ZONE" \
        --format='value(metadata.items.neura-config)' 2>/dev/null |
        sed -n 's/^DOMAIN=//p' || true)
    if [[ -n $current ]]; then
        echo "$current"
    else
        echo "$(address | tr . -).sslip.io"
    fi
}

# --- secrets --------------------------------------------------------------------------------------

has_version() {
    [[ -n $(g secrets versions list "$1" --filter=state=enabled --limit=1 --format='value(name)' \
        2>/dev/null) ]]
}

ensure_secret() { exists secrets describe "$1" || g secrets create "$1" --replication-policy=automatic; }

generate_secrets() {
    local name tmp
    for name in "${SECRETS[@]}" neura-admin-token; do ensure_secret "$name"; done
    if ! has_version neura-db-password; then
        openssl rand -hex 32 | tr -d '\n' | g secrets versions add neura-db-password --data-file=-
    fi
    if ! has_version neura-manifest-signing-key; then
        uv run --quiet python -c \
            'from neura_control.security.signing import new_seed; print(new_seed(), end="")' |
            g secrets versions add neura-manifest-signing-key --data-file=-
    fi
    if ! has_version neura-agent-ca-key; then
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' RETURN
        (umask 077 && uv run --quiet python - "$tmp" <<'EOF')
import sys
from pathlib import Path

from neura_contracts import pki

out = Path(sys.argv[1])
authority = pki.CertificateAuthority.create("Neura agent CA")
(out / "ca.crt").write_bytes(authority.certificate_pem)
(out / "ca.key").write_bytes(authority.key_pem)
EOF
        g secrets versions add neura-agent-ca-cert --data-file="$tmp/ca.crt"
        g secrets versions add neura-agent-ca-key --data-file="$tmp/ca.key"
    fi
}

# --- infrastructure -------------------------------------------------------------------------------

infrastructure() {
    step "enabling the APIs"
    g services enable iam.googleapis.com cloudresourcemanager.googleapis.com \
        compute.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
        artifactregistry.googleapis.com iap.googleapis.com

    step "service account $SA"
    exists iam service-accounts describe "$SA" ||
        g iam service-accounts create "$SA_NAME" --display-name="Neura host"
    for role in roles/cloudsql.client roles/logging.logWriter roles/monitoring.metricWriter; do
        g projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$role" \
            --condition=None >/dev/null
    done

    step "Artifact Registry $REGISTRY"
    exists artifacts repositories describe "$REPO" --location "$REGION" ||
        g artifacts repositories create "$REPO" --location "$REGION" --repository-format=docker
    g artifacts repositories add-iam-policy-binding "$REPO" --location "$REGION" \
        --member="serviceAccount:$SA" --role=roles/artifactregistry.reader >/dev/null

    step "secrets"
    generate_secrets
    for name in "${SECRETS[@]}"; do
        g secrets add-iam-policy-binding "$name" --member="serviceAccount:$SA" \
            --role=roles/secretmanager.secretAccessor >/dev/null
    done
    # The VM adds the bootstrap admin token; it cannot read it back.
    g secrets add-iam-policy-binding neura-admin-token --member="serviceAccount:$SA" \
        --role=roles/secretmanager.secretVersionAdder >/dev/null

    step "Cloud SQL $SQL (a first create takes several minutes)"
    if ! exists sql instances describe "$SQL"; then
        g sql instances create "$SQL" --database-version=POSTGRES_17 --edition=enterprise \
            --tier="$SQL_TIER" --region="$REGION" --storage-type=SSD --storage-size=10 \
            --storage-auto-increase --backup-start-time=03:00 --enable-point-in-time-recovery \
            --retained-backups-count=14 --deletion-protection \
            --ssl-mode=TRUSTED_CLIENT_CERTIFICATE_REQUIRED
    fi
    exists sql databases describe neura --instance "$SQL" ||
        g sql databases create neura --instance "$SQL"
    if ! exists sql users describe neura --instance "$SQL"; then
        g sql users create neura --instance "$SQL" \
            --password="$(g secrets versions access latest --secret=neura-db-password)"
    fi

    step "network"
    # Projects whose organization skips the default network get one like it.
    exists compute networks describe default ||
        g compute networks create default --subnet-mode=auto
    exists compute addresses describe "$VM-ip" --region "$REGION" ||
        g compute addresses create "$VM-ip" --region "$REGION"
    exists compute firewall-rules describe neura-web ||
        g compute firewall-rules create neura-web --network=default --direction=INGRESS \
            --allow=tcp:80,tcp:443 --target-tags=neura-host --source-ranges=0.0.0.0/0
    # SSH only through Identity-Aware Proxy.
    exists compute firewall-rules describe neura-iap-ssh ||
        g compute firewall-rules create neura-iap-ssh --network=default --direction=INGRESS \
            --allow=tcp:22 --target-tags=neura-host --source-ranges=35.235.240.0/20

    step "data disk with daily snapshots"
    exists compute resource-policies describe neura-daily --region "$REGION" ||
        g compute resource-policies create snapshot-schedule neura-daily --region "$REGION" \
            --daily-schedule --start-time=04:00 --max-retention-days=14 \
            --on-source-disk-delete=keep-auto-snapshots
    if ! exists compute disks describe neura-data --zone "$ZONE"; then
        g compute disks create neura-data --zone "$ZONE" --size=20GB --type=pd-balanced \
            --resource-policies=neura-daily
    fi
}

# --- release --------------------------------------------------------------------------------------

build_images() {
    local tag=$1 domain=$2 target
    g auth configure-docker "$REGISTRY_HOST" >/dev/null 2>&1
    for target in neura-control neura-console neura-landing; do
        step "building $target:$tag"
        docker build --quiet --platform=linux/amd64 -f deploy/docker/Dockerfile --target "$target" \
            --secret "id=ca_bundle,src=${NEURA_BUILD_CA_BUNDLE:-/dev/null}" \
            --build-arg "VITE_CONSOLE_URL=https://console.$domain/sign-in" \
            --build-arg "VITE_DOCS_URL=$DOCS_URL" \
            --build-arg "VITE_CONTACT_URL=$CONTACT_URL" \
            -t "$REGISTRY/$target:$tag" . >/dev/null
        docker push --quiet "$REGISTRY/$target:$tag" >/dev/null
    done
}

pinned() { docker image inspect --format '{{index .RepoDigests 0}}' "$REGISTRY/$1:$2"; }

release() {
    local tag domain config startup
    tag=$(git rev-parse --short=12 HEAD)
    [[ -z $(git status --porcelain) ]] || tag="$tag-dirty-$(date -u +%Y%m%d%H%M%S)"
    domain=$(domain)
    build_images "$tag" "$domain"

    config=$(mktemp)
    startup=$(mktemp)
    trap 'rm -f "$config" "$startup"' RETURN
    cat >"$config" <<EOF
PROJECT=$PROJECT
DOMAIN=$domain
ACME_EMAIL=${NEURA_ACME_EMAIL:-}
REGISTRY_HOST=$REGISTRY_HOST
SQL_INSTANCE=$PROJECT:$REGION:$SQL
CONTROL_IMAGE=$(pinned neura-control "$tag")
CONSOLE_IMAGE=$(pinned neura-console "$tag")
LANDING_IMAGE=$(pinned neura-landing "$tag")
EOF
    cat >"$startup" <<'EOF'
#!/bin/bash
# Install neura-apply from the metadata and run it (deploy/gcp/neura-apply.sh).
set -euo pipefail
curl -fsS -H Metadata-Flavor:Google -o /usr/local/sbin/neura-apply.new \
    http://metadata.google.internal/computeMetadata/v1/instance/attributes/neura-apply
chmod 0755 /usr/local/sbin/neura-apply.new
mv /usr/local/sbin/neura-apply.new /usr/local/sbin/neura-apply
exec /usr/local/sbin/neura-apply boot
EOF
    local files="startup-script=$startup,neura-apply=$HERE/neura-apply.sh"
    files+=",neura-compose=$HERE/compose.yaml,neura-edge=$HERE/edge.conf.template"
    files+=",neura-config=$config"

    if exists compute instances describe "$VM" --zone "$ZONE"; then
        step "rolling $VM to $tag"
        g compute instances add-metadata "$VM" --zone "$ZONE" --metadata-from-file="$files"
    else
        step "creating $VM ($MACHINE_TYPE)"
        g compute instances create "$VM" --zone "$ZONE" --machine-type="$MACHINE_TYPE" \
            --image-family=debian-12 --image-project=debian-cloud \
            --boot-disk-size=20GB --boot-disk-type=pd-balanced \
            --disk=name=neura-data,device-name=neura-data,mode=rw,boot=no,auto-delete=no \
            --address="$VM-ip" --tags=neura-host \
            --service-account="$SA" --scopes=cloud-platform \
            --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
            --metadata=enable-oslogin=TRUE,enable-guest-attributes=TRUE --metadata-from-file="$files"
    fi
    wait_for "$domain" "$(pinned neura-control "$tag")"
}

guest() {
    g compute instances get-guest-attributes "$VM" --zone "$ZONE" --query-path="neura/$1" \
        --format='value(value)' 2>/dev/null || true
}

wait_for() {
    local domain=$1 image=$2 _
    step "waiting for the VM to apply the release (a new VM installs Docker first)"
    for _ in $(seq 120); do
        if [[ $(guest release) == "$image" && $(guest state) == ok ]]; then
            curl -fsS --max-time 10 "https://console.$domain/v1/meta" && echo && return 0
        fi
        if [[ $(guest state) == failed && $(guest attempt) == "$image" ]]; then
            echo "the VM failed to apply the release:" >&2
            echo "  deploy/gcp/deploy.sh ssh 'sudo journalctl -u google-startup-scripts -u neura-watch -n 100'" >&2
            return 1
        fi
        sleep 10
    done
    echo "the VM did not apply the release in 20 minutes" >&2
    return 1
}

bootstrap_admin() {
    has_version neura-admin-token && return 0
    step "creating the organization $ORGANIZATION and its first admin token"
    g compute instances add-metadata "$VM" --zone "$ZONE" --metadata="neura-bootstrap-org=$ORGANIZATION"
    local _
    for _ in $(seq 30); do
        if has_version neura-admin-token; then
            g compute instances remove-metadata "$VM" --zone "$ZONE" --keys=neura-bootstrap-org
            return 0
        fi
        sleep 5
    done
    echo "no admin token yet; check: deploy/gcp/deploy.sh ssh 'sudo journalctl -u neura-watch'" >&2
    return 1
}

summary() {
    local domain
    domain=$(domain)
    cat <<EOF

Neura is deployed in $PROJECT.
  site       https://$domain
  console    https://console.$domain   (sign in with: deploy/gcp/deploy.sh admin-token)
  agents     agents.$domain:443 and enroll.$domain:443 (install commands in the console)
  address    $(address)
EOF
}

case "${1:-}" in
up)
    infrastructure
    if [[ -n ${NEURA_DOMAIN:-} ]]; then
        echo "point $NEURA_DOMAIN, console.$NEURA_DOMAIN, agents.$NEURA_DOMAIN and" \
            "enroll.$NEURA_DOMAIN at $(address) (A records) before the certificate is requested" >&2
    fi
    release
    bootstrap_admin
    summary
    ;;
release)
    release
    summary
    ;;
admin-token) g secrets versions access latest --secret=neura-admin-token && echo ;;
status)
    domain=$(domain)
    g compute instances describe "$VM" --zone "$ZONE" --format='value(status)'
    curl -fsS --max-time 5 "https://console.$domain/v1/meta" && echo
    curl -fsS --max-time 5 -o /dev/null -w "site: HTTP %{http_code}\n" "https://$domain/"
    ;;
ssh)
    shift
    g compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap ${1:+--command="$*"}
    ;;
esac
