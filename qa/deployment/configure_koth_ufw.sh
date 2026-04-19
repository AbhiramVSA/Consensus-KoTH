#!/usr/bin/env bash
set -euo pipefail

ROLE=""
INTERNAL_CIDR="192.168.0.0/24"
CHALLENGE_SOURCE_CIDR=""
APPLY=0

TCP_PORTS=(
  10001 10002 10004
  10010 10011 10012
  10020 10022 10023
  10030 10031 10032
  10040 10041 10042
  10050 10052 10053 10054 10055
  10061 10062 10063
  10070 10071 10072
)

UDP_PORTS=(10060)
MODE="preview"

usage() {
  cat <<'EOF'
Usage:
  bash qa/deployment/configure_koth_ufw.sh --role referee|node [--internal-cidr CIDR] [--challenge-source-cidr CIDR] [--apply]

Defaults:
  --internal-cidr 192.168.0.0/24
  --challenge-source-cidr defaults to --internal-cidr for node role and Anywhere for referee role

Behavior:
  - preview mode is the default and only prints the exact ufw commands
  - --apply runs the commands and enables ufw
  - only ports listed in docs/manual-tester-checklist.md are opened
  - SSH is restricted to the internal CIDR on both host roles

Host roles:
  referee
    - allows public ingress to the manual-testing challenge ports
    - allows TCP/8000 only from the internal CIDR
    - allows TCP/22 only from the internal CIDR

  node
    - allows the manual-testing challenge ports only from the challenge-source CIDR
    - allows TCP/22 only from the internal CIDR
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)
      ROLE="$2"
      shift 2
      ;;
    --internal-cidr)
      INTERNAL_CIDR="$2"
      shift 2
      ;;
    --challenge-source-cidr)
      CHALLENGE_SOURCE_CIDR="$2"
      shift 2
      ;;
    --apply)
      APPLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$ROLE" != "referee" && "$ROLE" != "node" ]]; then
  echo "--role must be referee or node" >&2
  usage >&2
  exit 2
fi

COMMANDS=()

add_cmd() {
  COMMANDS+=("$*")
}

build_rule_set() {
  local port
  local challenge_source

  add_cmd "ufw --force reset"
  add_cmd "ufw default deny incoming"
  add_cmd "ufw default allow outgoing"
  add_cmd "ufw allow in on lo"
  add_cmd "ufw allow from ${INTERNAL_CIDR} to any port 22 proto tcp comment 'internal ssh only'"

  if [[ "$ROLE" == "referee" ]]; then
    add_cmd "ufw allow from ${INTERNAL_CIDR} to any port 8000 proto tcp comment 'internal referee ui'"
    for port in "${TCP_PORTS[@]}"; do
      add_cmd "ufw allow ${port}/tcp comment 'manual-testing tcp port ${port}'"
    done
    for port in "${UDP_PORTS[@]}"; do
      add_cmd "ufw allow ${port}/udp comment 'manual-testing udp port ${port}'"
    done
  else
    challenge_source="${CHALLENGE_SOURCE_CIDR:-$INTERNAL_CIDR}"
    for port in "${TCP_PORTS[@]}"; do
      add_cmd "ufw allow from ${challenge_source} to any port ${port} proto tcp comment 'challenge tcp ${port} from trusted source'"
    done
    for port in "${UDP_PORTS[@]}"; do
      add_cmd "ufw allow from ${challenge_source} to any port ${port} proto udp comment 'challenge udp ${port} from trusted source'"
    done
  fi

  add_cmd "ufw logging on"
  add_cmd "ufw --force enable"
  add_cmd "ufw status numbered"
}

print_plan() {
  echo "== KOTH UFW Plan =="
  echo "Role: $ROLE"
  echo "Internal CIDR: $INTERNAL_CIDR"
  if [[ "$ROLE" == "node" ]]; then
    echo "Challenge source CIDR: ${CHALLENGE_SOURCE_CIDR:-$INTERNAL_CIDR}"
  fi
  echo "Mode: $MODE"
  echo
  echo "Source matrix: docs/manual-tester-checklist.md"
  echo "Manual-testing TCP ports: ${TCP_PORTS[*]}"
  echo "Manual-testing UDP ports: ${UDP_PORTS[*]}"
  echo
  echo "Commands:"
  local cmd
  for cmd in "${COMMANDS[@]}"; do
    printf '  %s\n' "$cmd"
  done
}

apply_plan() {
  local cmd
  for cmd in "${COMMANDS[@]}"; do
    echo "+ $cmd"
    eval "$cmd"
  done
}

build_rule_set
if [[ "$APPLY" -eq 1 ]]; then
  MODE="apply"
  if ! command -v ufw >/dev/null 2>&1; then
    echo "ufw is not installed or not in PATH" >&2
    exit 1
  fi
fi
print_plan

if [[ "$APPLY" -eq 1 ]]; then
  echo
  echo "Applying ufw rule set..."
  apply_plan
else
  echo
  echo "Preview only. Re-run with --apply to enable ufw."
fi
