#!/usr/bin/env bash
set -euo pipefail

SERIES_ROOT="/opt/KOTH_orchestrator"
ACTIVE_SERIES=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --series-root)
      SERIES_ROOT="$2"
      shift 2
      ;;
    --active-series)
      ACTIVE_SERIES="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: $0 [--series-root PATH] [--active-series N]"
      exit 2
      ;;
  esac
done

PASS=0
FAIL=0

pass() { echo "[PASS] $*"; PASS=$((PASS + 1)); }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL + 1)); }

require_cmd() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "command exists: $cmd"
  else
    fail "missing command: $cmd"
  fi
}

echo "== KOTH Node Validation =="
echo "Host: $(hostname)"
echo "Series root: $SERIES_ROOT"
echo

require_cmd docker
require_cmd docker-compose
require_cmd grep
require_cmd awk

if systemctl is-active --quiet chrony; then
  pass "chrony service active"
else
  fail "chrony service not active"
fi

if docker info >/dev/null 2>&1; then
  pass "docker daemon reachable"
else
  fail "docker daemon not reachable for current user"
fi

if [[ -d "$SERIES_ROOT/repo" ]]; then
  pass "repo directory exists: $SERIES_ROOT/repo"
else
  fail "repo directory missing: $SERIES_ROOT/repo"
fi

for s in 1 2 3 4 5 6 7 8; do
  compose="$SERIES_ROOT/h$s/docker-compose.yml"
  if [[ -f "$compose" ]]; then
    pass "compose exists: h$s"
  else
    fail "compose missing: $compose"
    continue
  fi

  if grep -q "machineH${s}A" "$compose" && grep -q "machineH${s}B" "$compose" && grep -q "machineH${s}C" "$compose"; then
    pass "compose naming looks correct for H$s (A/B/C)"
  else
    fail "compose naming mismatch for H$s (missing machineH${s}{A,B,C})"
  fi
done

if [[ -n "$ACTIVE_SERIES" ]]; then
  for v in A B C; do
    c="machineH${ACTIVE_SERIES}${v}"
    if docker ps --format '{{.Names}}' | grep -qx "$c"; then
      pass "active container running: $c"
    else
      fail "active container not running: $c"
    fi
  done
fi

echo
echo "Summary: PASS=$PASS FAIL=$FAIL"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
echo "ALL CHECKS PASSED"
