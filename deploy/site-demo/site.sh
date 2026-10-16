#!/usr/bin/env bash
# The site kit (README.md here): a customer's site on this machine, connected to your Neura.
#
#   ./site.sh publish CONSOLE_URL      the ML team's release: the published ALOHA policy's
#                                      contracts, signed, registered in your project (asks for
#                                      an API token; the project is the one in the URL)
#   ./site.sh connect 'neura-agent enroll --control-plane HOST:PORT --token nen_...'
#                                      start this site's server and enroll it with the command
#                                      the console gives (Sites, your site, Add a server); the
#                                      robot cell starts with it
#   ./site.sh approve                  approve the activations waiting at the site, as its
#                                      operator (the cell's page has the same buttons)
#   ./site.sh status                   what the site agent reports
#   ./site.sh logs [SERVICE]           follow the logs (site-agent, cell)
#   ./site.sh down                     stop everything (the site's state is kept)
#   ./site.sh forget                   stop, and delete the site's state: its enrollment, keys,
#                                      weights and the publisher key
#
# Needs Docker with Compose v2. Runs on the CPU: no GPU needed.
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE=(docker compose)
ENV_FILE=.env
PAGE=http://127.0.0.1:8095

die() {
  echo "site.sh: $*" >&2
  exit 1
}
step() { printf '\n\033[1m--> %s\033[0m\n' "$*"; }

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}

# .env holds the kit's settings (Compose reads it): the Neura URL and the site operator's
# credential, made here. Only you can read it.
get() { [[ -f $ENV_FILE ]] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 || true; }
put() {
  umask 077
  touch "$ENV_FILE"
  { grep -v "^$1=" "$ENV_FILE" || true; } >"$ENV_FILE.tmp"
  echo "$1=$2" >>"$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
}

init() {
  command -v docker >/dev/null 2>&1 || die "Docker is needed: https://docs.docker.com/get-docker/"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is needed ('docker compose')"
  docker info >/dev/null 2>&1 || die "Docker is not running: start Docker Desktop (or dockerd)"
  if [[ -z "$(get NEURA_OPERATOR_TOKEN)" ]]; then
    local token
    token="ntop_$(openssl rand -hex 24)"
    put NEURA_OPERATOR_TOKEN "$token"
    put NEURA_OPERATOR_TOKEN_SHA256 "$(printf %s "$token" | sha256)"
  fi
}

published() {
  [[ -n "$(get NEURA_URL)" ]] || die "publish the model first: ./site.sh publish https://console.<domain>/o/<org>/p/<project>"
}

kit() { "${COMPOSE[@]}" --profile tools run --rm -T kit "$@"; }
agent() { "${COMPOSE[@]}" exec -T site-agent "$@"; }

cmd_publish() {
  local url=${1:-}
  [[ $url =~ ^https?://[^/]+ ]] ||
    die "give your project's console URL: ./site.sh publish https://console.<domain>/o/<org>/p/<project>"
  shift
  put NEURA_URL "${BASH_REMATCH[0]}"
  if [[ -z ${NEURA_TOKEN:-} ]]; then
    echo "An API token for your organization: create one in the console (Settings, API tokens)."
    read -r -s -p "API token (not shown, not stored): " NEURA_TOKEN
    echo
  fi
  [[ -n $NEURA_TOKEN ]] || die "no API token"
  export NEURA_TOKEN
  step "building the kit's image (the first time takes a few minutes)"
  "${COMPOSE[@]}" --profile tools build kit
  step "the ML team's release: the model, its contracts, signed, registered in Neura"
  "${COMPOSE[@]}" --profile tools run --rm -T -e NEURA_TOKEN kit publish --url "$url" "$@"
  cat <<EOF

Published. Next, in the Neura console ($(get NEURA_URL)):
  1. Sites: create a site (say "My lab").
  2. Robots: add two robots at that site, robot profile aloha-sim-transfer-cube
     (names like aloha-1 and aloha-2).
  3. The site's page: Add a server, and copy the install command. Then here:
       ./site.sh connect '<the install command>'
EOF
}

cmd_connect() {
  local command="$*" endpoint token
  [[ $command == *"neura-agent enroll"* ]] ||
    die "paste the console's install command: ./site.sh connect 'neura-agent enroll --control-plane ... --token ...'"
  published
  endpoint=$(sed -nE 's/.*--control-plane[ =]+([^ ]+).*/\1/p' <<<"$command")
  token=$(sed -nE 's/.*--token[ =]+([^ ]+).*/\1/p' <<<"$command")
  [[ -n $endpoint && -n $token ]] || die "the install command needs --control-plane and --token"

  step "starting this site's server: the site agent"
  "${COMPOSE[@]}" up -d --wait site-agent
  if agent neura-agent status | grep -q '"enrolled": true'; then
    echo "already enrolled (./site.sh forget starts over)"
  else
    step "enrolling it with Neura (outbound only: nothing here listens for the internet)"
    agent neura-agent enroll --control-plane "$endpoint" --token "$token"
  fi

  step "trusting your publisher's key: this server runs only artifacts it signed"
  agent neura-agent trust-publisher "$(kit publisher-key | tr -d '\r')" --note "site kit publisher"

  step "importing the model's weights into the site's artifact store (checked against the artifact)"
  agent neura-agent import-weights /kit/checkpoint --artifact /kit/contracts/artifact.json

  step "starting the robot cell"
  "${COMPOSE[@]}" up -d cell
  cat <<EOF

Your site is connected. Watch the robots: $PAGE
Then, in the Neura console:
  1. Deployments: create a deployment for the site's robots, and add a revision with the
     model artifact act-aloha-transfer-cube and its execution profile.
  2. Prepare the revision: the site checks the signature, loads the model and measures it.
  3. Activate it. The site operator approves on the cell's page (or ./site.sh approve), and
     the robots start.
EOF
}

cmd_approve() {
  NEURA_AGENT_OPERATOR_TOKEN="$(get NEURA_OPERATOR_TOKEN)"
  export NEURA_AGENT_OPERATOR_TOKEN
  "${COMPOSE[@]}" exec -T -e NEURA_AGENT_OPERATOR_TOKEN site-agent python -I - "${1:-site operator}" <<'EOF'
import json, subprocess, sys

items = json.loads(subprocess.run(["neura-agent", "approvals"], capture_output=True, text=True,
                                  check=True).stdout)["items"]
pending = [a for a in items if not a["approved_by"]]
if not pending:
    print("nothing is waiting for the site operator")
for a in pending:
    print(f"approving {a['kind']} of {a['revision_id']} for {', '.join(a['robot_names'])}")
    subprocess.run(["neura-agent", "approve", a["command_id"], "--operator", sys.argv[1]],
                   check=True, stdout=subprocess.DEVNULL)
EOF
}

cmd_forget() {
  read -r -p "Delete this site's enrollment, keys, weights and publisher key? [y/N] " answer
  [[ $answer == [yY]* ]] || exit 0
  "${COMPOSE[@]}" --profile tools down --volumes --remove-orphans
  rm -f "$ENV_FILE"
  echo "forgotten; remove the server from the site in the console too"
}

main() {
  local command=${1:-help}
  shift || true
  case $command in
  publish) init && cmd_publish "$@" ;;
  connect) init && cmd_connect "$@" ;;
  approve) published && cmd_approve "$@" ;;
  status) published && agent neura-agent status ;;
  logs) published && "${COMPOSE[@]}" logs -f --tail 100 "$@" ;;
  down) published && "${COMPOSE[@]}" --profile tools down ;;
  forget) published && cmd_forget ;;
  help | -h | --help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command $command (./site.sh help)" ;;
  esac
}

main "$@"
