#!/usr/bin/env bash
# Runs on the Neura VM as root. deploy/gcp/deploy.sh publishes it in the instance metadata
# (neura-apply) together with the release; the startup script installs it on every boot.
#
#   neura-apply boot    install Docker, mount the data disk, install the units, then apply
#   neura-apply apply   read the release from the metadata and bring the containers to it
#   neura-apply watch   apply again whenever the instance metadata changes (neura-watch.service)
#   neura-apply renew   renew the TLS certificate (neura-renew.timer)
#
# The VM keeps no configuration of its own: everything comes from the metadata (the release) and
# Secret Manager (the secrets, kept on tmpfs), so a rebuilt VM converges to the same state.
set -euo pipefail

MD=http://metadata.google.internal/computeMetadata/v1
ROOT=/srv/neura
DATA=$ROOT/data
SECRETS=/run/neura
SELF=/usr/local/sbin/neura-apply
DISK=/dev/disk/by-id/google-neura-data
CERTBOT=mirror.gcr.io/certbot/certbot:v3.0.1@sha256:4d8875e85a2af373262914e15c0a149e1985c5bbd05ac433fe4763f465020fcf
CONTROL_UID=10001 # the neura user in the images (deploy/docker/Dockerfile)

log() { echo "neura-apply: $*" >&2; }
die() {
    log "$*"
    report state failed
    exit 1
}
attr() { curl -fsS -H Metadata-Flavor:Google "$MD/instance/attributes/$1"; }
compose() {
    docker compose --project-name neura --project-directory "$ROOT" \
        -f "$ROOT/compose.yaml" --env-file "$ROOT/release.env" "$@"
}
# Progress for deploy.sh, as guest attributes (neura/attempt, neura/state, neura/release).
report() {
    curl -fsS -X PUT --data "$2" -H Metadata-Flavor:Google \
        "$MD/instance/guest-attributes/neura/$1" >/dev/null 2>&1 || true
}
lock() {
    exec 9>/run/neura-apply.lock
    flock 9
}

install_docker() {
    if ! command -v envsubst >/dev/null; then
        apt-get update -q
        apt-get install -yq gettext-base
    fi
    if ! docker compose version >/dev/null 2>&1; then
        log "installing Docker"
        apt-get update -q
        apt-get install -yq ca-certificates curl
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
        # shellcheck disable=SC1091 # the OS release file
        codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
            "https://download.docker.com/linux/debian $codename stable" \
            >/etc/apt/sources.list.d/docker.list
        apt-get update -q
        apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    fi
    local want='{"log-driver": "local", "log-opts": {"max-size": "20m", "max-file": "5"}}'
    if [[ "$(cat /etc/docker/daemon.json 2>/dev/null)" != "$want" ]]; then
        echo "$want" >/etc/docker/daemon.json
        systemctl restart docker
    fi
}

mount_data() {
    [[ -e $DISK ]] || die "the data disk (device name neura-data) is not attached"
    if ! blkid "$DISK" >/dev/null 2>&1; then
        log "formatting the new data disk"
        mkfs.ext4 -q -F -L neura-data "$DISK"
    fi
    local uuid
    uuid=$(blkid -s UUID -o value "$DISK")
    mkdir -p "$DATA"
    grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $DATA ext4 defaults,nofail,discard 0 2" >>/etc/fstab
    mountpoint -q "$DATA" || mount "$DATA"
    install -d -m 0755 "$DATA/letsencrypt" "$DATA/acme" "$DATA/tls"
    install -d -m 0750 -o "$CONTROL_UID" -g "$CONTROL_UID" "$DATA/sealed-weights"
}

install_units() {
    cat >/etc/systemd/system/neura-watch.service <<'EOF'
[Unit]
Description=Apply Neura releases published in the instance metadata
After=docker.service network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/sbin/neura-apply watch
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    cat >/etc/systemd/system/neura-renew.service <<'EOF'
[Unit]
Description=Renew Neura's TLS certificate
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/neura-apply renew
EOF
    cat >/etc/systemd/system/neura-renew.timer <<'EOF'
[Unit]
Description=Renew Neura's TLS certificate daily

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
}

fetch_release() {
    install -d -m 0755 "$ROOT"
    attr neura-compose >"$ROOT/compose.yaml.new"
    attr neura-edge >"$ROOT/edge.conf.template.new"
    attr neura-config >"$ROOT/release.env.new"
    mv "$ROOT/compose.yaml.new" "$ROOT/compose.yaml"
    mv "$ROOT/edge.conf.template.new" "$ROOT/edge.conf.template"
    mv "$ROOT/release.env.new" "$ROOT/release.env"
    set -a
    # shellcheck disable=SC1091 # written by deploy.sh
    . "$ROOT/release.env"
    set +a
    [[ ${DOMAIN:-} =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || die "invalid DOMAIN: ${DOMAIN:-}"
    [[ -n ${PROJECT:-} && -n ${REGISTRY_HOST:-} ]] || die "release.env lacks PROJECT/REGISTRY_HOST"
}

secret() { gcloud secrets versions access latest --secret="$1" --project="$PROJECT"; }

fetch_secrets() {
    install -d -m 0700 "$SECRETS"
    local password signing
    password=$(secret neura-db-password)
    signing=$(secret neura-manifest-signing-key)
    (
        umask 077
        printf 'NEURA_CONTROL_DATABASE_URL=postgresql://neura:%s@cloudsql-proxy:5432/neura\n' \
            "$password" >"$SECRETS/control-plane.env"
        printf 'NEURA_CONTROL_MANIFEST_SIGNING_KEY=%s\n' "$signing" >>"$SECRETS/control-plane.env"
        secret neura-agent-ca-cert >"$SECRETS/agent-ca.crt"
        secret neura-agent-ca-key >"$SECRETS/agent-ca.key"
    )
    chown "$CONTROL_UID:$CONTROL_UID" "$SECRETS/agent-ca.crt" "$SECRETS/agent-ca.key"
    chmod 0400 "$SECRETS/agent-ca.crt" "$SECRETS/agent-ca.key"
}

install_certificate() {
    # The control plane runs as an unprivileged user and serves the same certificate to agents.
    install -m 0400 -o "$CONTROL_UID" -g "$CONTROL_UID" \
        "$DATA/letsencrypt/live/neura/fullchain.pem" "$DATA/tls/fullchain.pem"
    install -m 0400 -o "$CONTROL_UID" -g "$CONTROL_UID" \
        "$DATA/letsencrypt/live/neura/privkey.pem" "$DATA/tls/privkey.pem"
}

certbot() {
    docker run --rm -v "$DATA/letsencrypt:/etc/letsencrypt" -v "$DATA/acme:/var/www/acme" "$@"
}

ensure_certificate() {
    local names="$DOMAIN console.$DOMAIN agents.$DOMAIN enroll.$DOMAIN"
    if [[ -s $DATA/tls/fullchain.pem && "$(cat "$DATA/tls/names" 2>/dev/null)" == "$names" ]]; then
        return
    fi
    log "requesting a certificate for $names"
    local args=() name
    for name in $names; do args+=(-d "$name"); done
    if [[ -n ${ACME_EMAIL:-} ]]; then
        args+=(--email "$ACME_EMAIL" --no-eff-email)
    else
        args+=(--register-unsafely-without-email)
    fi
    # The first certificate comes before the edge can serve challenges: certbot answers on :80.
    compose stop edge >/dev/null 2>&1 || true
    if [[ -d $DATA/letsencrypt/live/neura ]]; then
        certbot "$CERTBOT" delete --cert-name neura --non-interactive
    fi
    certbot -p 80:80 "$CERTBOT" certonly --standalone --non-interactive --agree-tos \
        --cert-name neura "${args[@]}"
    install_certificate
    echo "$names" >"$DATA/tls/names"
}

bootstrap() {
    local organization out token
    organization=$(attr neura-bootstrap-org 2>/dev/null || true)
    [[ -n $organization && ! -e $DATA/.bootstrapped ]] || return 0
    log "creating the organization and its first admin token"
    out=$(compose exec -T control-plane neura-control bootstrap \
        --organization "$organization" --create-token --token-days 30)
    token=$(sed -n 's/^admin API token (shown once): //p' <<<"$out")
    [[ -n $token ]] || die "bootstrap printed no token"
    printf '%s' "$token" |
        gcloud secrets versions add neura-admin-token --data-file=- --project="$PROJECT" >/dev/null
    date -u >"$DATA/.bootstrapped"
}

apply() {
    lock
    fetch_release
    report attempt "$CONTROL_IMAGE"
    report state applying
    trap 'report state failed' ERR
    fetch_secrets
    # shellcheck disable=SC2016 # the list of variables envsubst may replace, kept literal
    envsubst '${DOMAIN}' <"$ROOT/edge.conf.template" >"$ROOT/edge.conf"
    gcloud auth configure-docker "$REGISTRY_HOST" --quiet >/dev/null 2>&1
    ensure_certificate
    compose pull --quiet
    compose up -d --remove-orphans --wait
    compose exec -T edge nginx -s reload
    bootstrap
    report release "$CONTROL_IMAGE"
    report state ok
    log "running ${CONTROL_IMAGE##*@}"
}

renew() {
    lock
    set -a
    # shellcheck disable=SC1091 # written by fetch_release
    . "$ROOT/release.env"
    set +a
    local before after
    before=$(sha256sum "$DATA/letsencrypt/live/neura/cert.pem")
    certbot "$CERTBOT" renew --non-interactive --webroot -w /var/www/acme
    after=$(sha256sum "$DATA/letsencrypt/live/neura/cert.pem")
    if [[ $before != "$after" ]]; then
        log "certificate renewed"
        install_certificate
        # Agents reconnect on their own; the console and API see a reload, not a restart.
        compose restart control-plane
        compose exec -T edge nginx -s reload
    fi
}

watch() {
    local etag=NONE headers new
    headers=$(mktemp)
    while true; do
        if curl -fsS -o /dev/null -D "$headers" -H Metadata-Flavor:Google \
            "$MD/instance/attributes/?recursive=true&wait_for_change=true&timeout_sec=600&last_etag=$etag"; then
            new=$(awk 'tolower($1) == "etag:" { print $2 }' "$headers" | tr -d '\r')
            if [[ $etag != NONE && $new != "$etag" ]]; then
                log "metadata changed: applying"
                if attr neura-apply >"$SELF.new"; then
                    chmod 0755 "$SELF.new"
                    mv "$SELF.new" "$SELF"
                fi
                "$SELF" apply || log "apply failed; the previous containers keep running"
            fi
            etag=$new
        else
            sleep 5
        fi
    done
}

case "${1:-}" in
boot)
    install_docker
    mount_data
    install_units
    "$SELF" apply || log "apply failed; it runs again on the next release"
    systemctl enable --now neura-watch.service neura-renew.timer
    ;;
apply) apply ;;
watch) watch ;;
renew) renew ;;
*) die "usage: neura-apply boot|apply|watch|renew" ;;
esac
