#!/bin/sh
# Install the Neura site agent on a GPU server (design §17.2).
#
#   sudo ./install.sh --bundle neura-agent-<version>.tar --release-keys release-keys.pem \
#       --control-plane <host:port> --token <enrollment token> [--env KEY=VALUE ...]
#
# It verifies the release bundle before installing anything: the Ed25519 signature over
# release.json against the release keys (openssl), then every file against its signed SHA-256.
# Then it installs the release in its own virtual environment under /opt/neura/agent, pins the
# release keys, writes /etc/neura/agent.env and a hardened systemd unit, enrolls the server with
# the one-time token (the agent's key is generated here and never leaves the server), and starts
# the agent. Later upgrades arrive signed through the control plane or `neura-agent upgrade`.
#
# Requirements: Linux, Python 3.13+ with venv, openssl 3, and for policy workers Docker (or
# Podman) with the NVIDIA container toolkit.
#
# Testing and packaging options: --prefix DIR installs under DIR, --no-systemd writes the unit but
# does not run systemctl, --no-enroll skips enrollment, --python PATH picks the interpreter.
set -eu

bundle="" keys="" control_plane="" token="" prefix="" python="python3"
systemd=1 enroll=1 extra_env=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bundle) bundle="$2"; shift 2 ;;
    --release-keys) keys="$2"; shift 2 ;;
    --control-plane) control_plane="$2"; shift 2 ;;
    --token) token="$2"; shift 2 ;;
    --env) extra_env="${extra_env}$2
"; shift 2 ;;
    --prefix) prefix="$2"; shift 2 ;;
    --python) python="$2"; shift 2 ;;
    --no-systemd) systemd=0; shift ;;
    --no-enroll) enroll=0; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

fail() { echo "install: $*" >&2; exit 1; }
[ -n "$bundle" ] || fail "--bundle is required"
[ -n "$keys" ] || fail "--release-keys is required"
if [ "$enroll" = 1 ]; then
  [ -n "$control_plane" ] && [ -n "$token" ] || fail "--control-plane and --token are required"
fi
command -v openssl >/dev/null || fail "openssl is required"
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 13))' \
  || fail "Python 3.13 or newer is required ($python)"

install_dir="$prefix/opt/neura/agent"
state_dir="$prefix/var/lib/neura-agent"
config_dir="$prefix/etc/neura"
unit_dir="$prefix/etc/systemd/system"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# --- verify the bundle before anything is installed ------------------------------------------------
mkdir "$work/bundle"
tar -xf "$bundle" -C "$work/bundle" --no-same-owner --no-same-permissions
release_dir="$(dirname "$(find "$work/bundle" -name release.json -print | head -n 1)")"
[ -f "$release_dir/release.json" ] || fail "$bundle is not a release bundle"
verified=0
awk -v dir="$work" '/-----BEGIN PUBLIC KEY-----/{n++} n{print > (dir "/key" n ".pem")}' "$keys"
for key in "$work"/key*.pem; do
  [ -f "$key" ] || continue
  if openssl pkeyutl -verify -pubin -inkey "$key" -rawin -in "$release_dir/release.json" \
      -sigfile "$release_dir/release.sig" >/dev/null 2>&1; then
    verified=1
  fi
done
[ "$verified" = 1 ] || fail "the bundle is not signed by a release key in $keys"
version="$("$python" - "$release_dir" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
manifest = json.loads((root / "release.json").read_bytes())
assert manifest["type"] == "neura-agent-release" and manifest["name"] == "neura-agent"
for name, digest in manifest["files"].items():
    path = (root / name).resolve()
    assert root.resolve() in path.parents, f"unsafe path {name}"
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        sys.exit(f"{name} does not match its signed digest")
listed = set(manifest["files"])
present = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
extra = present - listed - {"release.json", "release.sig"}
if extra:
    sys.exit(f"files not in the signed release: {sorted(extra)}")
print(manifest["version"])
PY
)" || fail "the bundle's files do not match release.json"
echo "verified neura-agent $version"

# --- the service user and directories ---------------------------------------------------------
owner=""
if [ -z "$prefix" ] && [ "$(id -u)" = 0 ]; then
  id neura >/dev/null 2>&1 || useradd --system --home-dir "$state_dir" --shell /usr/sbin/nologin neura
  owner="neura:neura"
fi
mkdir -p "$install_dir/releases" "$install_dir/bin" "$config_dir" "$unit_dir" "$state_dir"
chmod 0700 "$state_dir"

# --- the release ----------------------------------------------------------------------------
target="$install_dir/releases/$version"
if [ ! -f "$target/.complete" ]; then
  rm -rf "$target"
  "$python" -m venv "$target/venv"
  "$target/venv/bin/python" -m pip install --quiet --no-index --no-cache-dir \
    --disable-pip-version-check --find-links "$release_dir/wheels" "neura-agent==$version"
  echo "$version" > "$target/.complete"
fi
ln -sfn "releases/$version" "$install_dir/.current-new"
mv -T "$install_dir/.current-new" "$install_dir/current"
install -m 0755 "$release_dir/bin/neura-agent-launch" "$install_dir/bin/neura-agent-launch"
install -m 0644 "$keys" "$config_dir/release-keys.pem"

# --- configuration and the unit ---------------------------------------------------------------
if [ ! -f "$config_dir/agent.env" ]; then
  {
    echo "NEURA_AGENT_STATE_DIR=/var/lib/neura-agent"
    echo "NEURA_AGENT_INSTALL_DIR=/opt/neura/agent"
    echo "NEURA_AGENT_RELEASE_KEYS_FILE=/etc/neura/release-keys.pem"
    printf '%s' "$extra_env"
  } > "$config_dir/agent.env"
  chmod 0640 "$config_dir/agent.env"
fi
groups=""
getent group docker >/dev/null 2>&1 && groups="SupplementaryGroups=docker"
cat > "$unit_dir/neura-agent.service" <<UNIT
[Unit]
Description=Neura site agent
Documentation=https://github.com/rkd0608/neura
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=exec
User=neura
Group=neura
$groups
EnvironmentFile=/etc/neura/agent.env
ExecStart=$python /opt/neura/agent/bin/neura-agent-launch run
Restart=always
RestartSec=2
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/neura-agent /opt/neura/agent
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
if [ -n "$owner" ]; then
  chown -R "$owner" "$install_dir" "$state_dir"
  chown "root:neura" "$config_dir/agent.env"
fi

# --- enroll and start -------------------------------------------------------------------------
if [ "$enroll" = 1 ]; then
  run_as=""
  [ -n "$owner" ] && run_as="runuser -u neura --"
  # shellcheck disable=SC2086
  env NEURA_AGENT_STATE_DIR="$state_dir" NEURA_AGENT_INSTALL_DIR="$install_dir" \
    $run_as "$install_dir/current/venv/bin/neura-agent" enroll \
    --control-plane "$control_plane" --token "$token"
fi
if [ "$systemd" = 1 ] && [ -z "$prefix" ]; then
  systemctl daemon-reload
  systemctl enable --now neura-agent.service
fi
echo "installed neura-agent $version in $install_dir"
