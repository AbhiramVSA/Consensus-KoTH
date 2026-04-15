#!/usr/bin/env bash
set -euo pipefail

SERIES_ROOT="/opt/KOTH_orchestrator"
REFEREE_DIR="/opt/KOTH_orchestrator/repo/referee-server"
API_URL="http://127.0.0.1:8000"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --series-root)
      SERIES_ROOT="$2"
      shift 2
      ;;
    --referee-dir)
      REFEREE_DIR="$2"
      shift 2
      ;;
    --api-url)
      API_URL="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: $0 [--series-root PATH] [--referee-dir PATH] [--api-url URL]"
      exit 2
      ;;
  esac
done

PASS=0
FAIL=0
SERVICE_ACTIVE=0

pass() { echo "[PASS] $*"; PASS=$((PASS + 1)); }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL + 1)); }

env_value() {
  local key="$1"
  local file="$2"
  local line
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

echo "== Referee + LB Validation =="
echo "Host: $(hostname)"
echo "Series root: $SERIES_ROOT"
echo "Referee dir: $REFEREE_DIR"
echo "API URL: $API_URL"
echo

for cmd in python3 curl jq grep awk ssh haproxy; do
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "command exists: $cmd"
  else
    fail "missing command: $cmd"
  fi
done

if systemctl is-active --quiet chrony; then
  pass "chrony service active"
else
  fail "chrony service not active"
fi

if [[ -f "$REFEREE_DIR/.env" ]]; then
  pass ".env exists: $REFEREE_DIR/.env"
else
  fail ".env missing: $REFEREE_DIR/.env"
fi

if [[ -d "$REFEREE_DIR/.venv" ]]; then
  pass "python venv exists: $REFEREE_DIR/.venv"
else
  fail "python venv missing: $REFEREE_DIR/.venv"
fi

if haproxy -c -f /etc/haproxy/haproxy.cfg >/dev/null 2>&1; then
  pass "haproxy config valid"
else
  fail "haproxy config invalid"
fi

if systemctl is-active --quiet haproxy; then
  pass "haproxy service active"
else
  fail "haproxy service not active"
fi

if systemctl is-active --quiet koth-referee; then
  SERVICE_ACTIVE=1
fi

if [[ -f "$REFEREE_DIR/.env" ]]; then
  required_keys=(
    NODE_HOSTS
    NODE_PRIORITY
    SSH_USER
    SSH_PRIVATE_KEY
    REMOTE_SERIES_ROOT
    CONTAINER_NAME_TEMPLATE
    ADMIN_API_KEY
  )
  for k in "${required_keys[@]}"; do
    v="$(env_value "$k" "$REFEREE_DIR/.env")"
    if [[ -n "$v" ]]; then
      pass ".env key present: $k"
    else
      fail ".env key missing/empty: $k"
    fi
  done

  ctmpl="$(env_value "CONTAINER_NAME_TEMPLATE" "$REFEREE_DIR/.env")"
  if [[ "$ctmpl" == "machineH{series}{variant}" ]]; then
    pass "CONTAINER_NAME_TEMPLATE matches expected value"
  else
    fail "CONTAINER_NAME_TEMPLATE expected machineH{series}{variant}, got: $ctmpl"
  fi

  remote_root="$(env_value "REMOTE_SERIES_ROOT" "$REFEREE_DIR/.env")"
  if [[ "$remote_root" == "$SERIES_ROOT" ]]; then
    pass "REMOTE_SERIES_ROOT matches expected series root argument"
  else
    fail "REMOTE_SERIES_ROOT mismatch: env=$remote_root expected=$SERIES_ROOT"
  fi

  node_hosts="$(env_value "NODE_HOSTS" "$REFEREE_DIR/.env")"
  host_count="$(printf '%s' "$node_hosts" | awk -F',' '{print NF}')"
  if [[ "$host_count" -eq 3 ]]; then
    pass "NODE_HOSTS defines 3 challenge nodes"
  else
    fail "NODE_HOSTS should contain exactly 3 hosts, got: $node_hosts"
  fi
fi

if [[ -f "$REFEREE_DIR/setup_cli.py" ]]; then
  if (cd "$REFEREE_DIR" && ./.venv/bin/python setup_cli.py --series 1 >/tmp/ref_setup_cli.out 2>/tmp/ref_setup_cli.err); then
    pass "setup_cli.py --series 1 succeeded"
  else
    fail "setup_cli.py --series 1 failed (see /tmp/ref_setup_cli.err)"
  fi
else
  fail "setup_cli.py missing in referee dir"
fi

http_code="$(curl -s -o /tmp/ref_status.json -w '%{http_code}' "$API_URL/api/status" || true)"
if [[ "$http_code" == "200" ]]; then
  pass "referee API status endpoint reachable"
  if [[ "$SERVICE_ACTIVE" -eq 1 ]]; then
    pass "koth-referee service active"
  else
    pass "referee reachable in manual mode (uvicorn without systemd)"
  fi
  if jq -e '.competition_status and .current_series != null' /tmp/ref_status.json >/dev/null 2>&1; then
    pass "referee API status payload shape valid"
  else
    fail "referee API status payload malformed"
  fi
else
  if [[ "$SERVICE_ACTIVE" -eq 1 ]]; then
    pass "koth-referee service active"
  else
    fail "koth-referee service inactive and API unavailable"
  fi
  fail "referee API status endpoint not healthy (HTTP $http_code)"
fi

echo
echo "Summary: PASS=$PASS FAIL=$FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
echo "ALL CHECKS PASSED"
