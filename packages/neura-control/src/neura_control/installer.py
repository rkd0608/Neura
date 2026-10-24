"""The one-line installer (design §29.2): what a production server's administrator runs to install
the site agent and connect it to a site.

    curl -fsSL https://neura.example/install.sh | sudo sh -s -- --token nen_...

``GET /install.sh`` serves a short script with the agent release this control plane offers
pinned in it: the bundle's address and SHA-256, and the release keys it must be signed with. The
script downloads the bundle, refuses it unless its SHA-256 matches, then runs the installer the
bundle carries (``deploy/install/install.sh``). That installer verifies the release's signature
against the pinned keys before installing anything, as it does when run by hand.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from neura_control.settings import ControlPlaneSettings

TEMPLATE = r"""#!/bin/sh
# Install the Neura site agent on this server and connect it to its site (design §29.2):
#
#   curl -fsSL __PUBLIC_URL__/install.sh | sudo sh -s -- --token <enrollment token>
#
# Options: --token TOKEN (from the console), --bench (a test bench), --trust KEY (a publisher key
# to trust, repeatable); passed on to the installer: --env KEY=VALUE, --prefix DIR, --python PATH,
# --no-systemd, --no-enroll.
#
# It downloads neura-agent __VERSION__, refuses it unless its SHA-256 is the one below, and runs
# the installer it carries, which checks the release's signature against the release keys below
# before installing anything.
set -eu

VERSION=__Q_VERSION__
BUNDLE_URL=__Q_URL__
BUNDLE_SHA256=__Q_SHA256__
CONTROL_PLANE=__Q_ENDPOINT__
RELEASE_KEYS=__Q_KEYS__

main() {
  token="" bench=0 trust="" envs="" prefix="" python="" no_systemd=0 no_enroll=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --token) token="$2"; shift 2 ;;
      --bench) bench=1; shift ;;
      --trust) trust="${trust}${trust:+,}\"$2\""; shift 2 ;;
      --env) envs="${envs}$2
"; shift 2 ;;
      --prefix) prefix="$2"; shift 2 ;;
      --python) python="$2"; shift 2 ;;
      --no-systemd) no_systemd=1; shift ;;
      --no-enroll) no_enroll=1; shift ;;
      -h|--help) echo "usage: sh install.sh --token TOKEN [--bench] [--trust KEY]..."; exit 0 ;;
      *) echo "install: unknown option $1" >&2; exit 2 ;;
    esac
  done
  fail() { echo "install: $*" >&2; exit 1; }
  [ -n "$token" ] || [ "$no_enroll" = 1 ] || fail "--token is required (the console shows it)"

  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  echo "downloading neura-agent $VERSION"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --proto '=https,file' -o "$work/bundle.tar" "$BUNDLE_URL" \
      || fail "could not download $BUNDLE_URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$work/bundle.tar" "$BUNDLE_URL" || fail "could not download $BUNDLE_URL"
  else
    fail "curl or wget is required"
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$work/bundle.tar" | cut -d' ' -f1)"
  else
    actual="$(shasum -a 256 "$work/bundle.tar" | cut -d' ' -f1)"
  fi
  [ "$actual" = "$BUNDLE_SHA256" ] \
    || fail "the download is not neura-agent $VERSION (SHA-256 $actual)"
  printf '%s\n' "$RELEASE_KEYS" > "$work/release-keys.pem"
  mkdir "$work/bundle"
  tar -xf "$work/bundle.tar" -C "$work/bundle" --no-same-owner --no-same-permissions
  installer="$(find "$work/bundle" -path '*/bin/install.sh' -print | head -n 1)"
  [ -n "$installer" ] || fail "neura-agent $VERSION carries no installer"

  set -- --bundle "$work/bundle.tar" --release-keys "$work/release-keys.pem"
  if [ "$no_enroll" = 1 ]; then
    set -- "$@" --no-enroll
  else
    set -- "$@" --control-plane "$CONTROL_PLANE" --token "$token"
  fi
  [ "$bench" = 1 ] && set -- "$@" --env "NEURA_AGENT_BENCH=true"
  [ -n "$trust" ] && set -- "$@" --env "NEURA_AGENT_TRUSTED_PUBLISHER_KEYS='[$trust]'"
  while IFS= read -r line; do
    [ -n "$line" ] && set -- "$@" --env "$line"
  done <<ENVS
$envs
ENVS
  [ -n "$prefix" ] && set -- "$@" --prefix "$prefix"
  [ -n "$python" ] && set -- "$@" --python "$python"
  [ "$no_systemd" = 1 ] && set -- "$@" --no-systemd
  sh "$installer" "$@"
}

# Everything runs from here, so a script cut off while downloading runs nothing.
main "$@"
"""


def available(settings: ControlPlaneSettings) -> bool:
    return settings.agent_release_url is not None


def script(settings: ControlPlaneSettings) -> str:
    """The installer for the agent release this control plane offers."""
    assert settings.agent_release_keys_file is not None
    keys = Path(settings.agent_release_keys_file).read_text(encoding="ascii").strip()
    endpoint = settings.agent_enroll_endpoint or settings.agent_endpoint
    values = {
        "__PUBLIC_URL__": settings.public_url.rstrip("/"),
        "__VERSION__": str(settings.agent_release_version),
        "__Q_VERSION__": shlex.quote(str(settings.agent_release_version)),
        "__Q_URL__": shlex.quote(str(settings.agent_release_url)),
        "__Q_SHA256__": shlex.quote(str(settings.agent_release_sha256)),
        "__Q_ENDPOINT__": shlex.quote(endpoint),
        "__Q_KEYS__": shlex.quote(keys),
    }
    text = TEMPLATE
    for name, value in values.items():
        text = text.replace(name, value)
    return text


def command(settings: ControlPlaneSettings, token: str, *, bench: bool, trust: list[str]) -> str:
    """What a server's administrator runs to install the agent and connect it to the site."""
    flags = "".join(f" --trust {key}" for key in trust) + (" --bench" if bench else "")
    url = f"{settings.public_url.rstrip('/')}/install.sh"
    return f"curl -fsSL {url} | sudo sh -s -- --token {token}{flags}"
