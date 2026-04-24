#!/usr/bin/env bash
set -euo pipefail

ROLE=""
INTERNAL_CIDR="192.168.0.0/24"
CHALLENGE_SOURCE_CIDR=""
APPLY=0
ACKNOWLEDGE_SSH_LOCKOUT=0

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
  bash qa/deployment/configure_koth_ufw.sh --role referee|node
                                           [--internal-cidr CIDR]
                                           [--challenge-source-cidr CIDR]
                                           [--apply]
                                           [--acknowledge-ssh-lockout]

Defaults:
  --internal-cidr 192.168.0.0/24
  --challenge-source-cidr defaults to --internal-cidr for node role and Anywhere for referee role

Behavior:
  - preview mode is the default and only prints the exact ufw commands
  - --apply runs the commands and enables ufw
  - only ports listed in docs/manual-tester-checklist.md are opened
  - SSH is restricted to the internal CIDR on both host roles

Safety:
  --apply refuses to run when invoked over ssh unless
  --acknowledge-ssh-lockout is also passed. The ruleset this script
  installs allows SSH only from --internal-cidr, so applying from an
  ssh session originating outside that CIDR will lock you out the moment
  ufw flips active.

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
    --acknowledge-ssh-lockout)
      ACKNOWLEDGE_SSH_LOCKOUT=1
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

  # UFW-lockout guard.
  #
  # Applying this rule set flushes existing rules and re-enables ufw with
  # SSH restricted to INTERNAL_CIDR. If the operator is currently ssh'd in
  # from an IP outside INTERNAL_CIDR, ``ufw enable`` will drop their
  # session the moment conntrack ages out and they lose console access
  # until someone drives to the box.
  #
  # Run this check BEFORE the ``command -v ufw`` probe — a fresh host that
  # does not yet have ufw installed is exactly the scenario where an
  # operator is most likely to be ssh'd in and unaware of the blast radius.
  #
  # $SSH_CONNECTION, when set by sshd, contains "client_ip client_port
  # server_ip server_port". We refuse --apply from an ssh session unless
  # the operator passes --acknowledge-ssh-lockout, which is the smallest
  # explicit opt-in that still lets an experienced operator proceed.
  if [[ -n "${SSH_CONNECTION:-}" && "$ACKNOWLEDGE_SSH_LOCKOUT" -ne 1 ]]; then
    ssh_client_ip="${SSH_CONNECTION%% *}"
    cat >&2 <<EOF

REFUSING TO --apply OVER AN SSH SESSION.

You are running this script over ssh (SSH_CONNECTION=$SSH_CONNECTION).
The plan above flushes ufw and re-enables it with SSH allowed only from
$INTERNAL_CIDR. If your client address ($ssh_client_ip) is not inside
that CIDR, this script will lock you out the moment the new ruleset
takes effect.

If you are intentionally applying from a console or a host inside
$INTERNAL_CIDR, re-run with --acknowledge-ssh-lockout to proceed.
EOF
    exit 1
  fi

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
