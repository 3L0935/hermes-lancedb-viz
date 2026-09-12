#!/usr/bin/env bash
# Post-install smoke test for the primary Docker deployment or systemd fallback.

PASS=0
FAIL=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
VIZ_MODE="${LANCEDB_VIZ_MODE:-docker}"
if [[ "$VIZ_MODE" == "systemd" ]]; then
  BASE_URL="${LANCEDB_VIZ_URL:-http://127.0.0.1:7778}"
else
  BASE_URL="${LANCEDB_VIZ_URL:-http://127.0.0.1:7777}"
fi

check() {
  local name="$1"
  shift
  if "$@" 2>/dev/null; then
    printf '  OK  %s\n' "$name"
    PASS=$((PASS + 1))
  else
    printf '  FAIL %s\n' "$name"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== hermes-lancedb-viz smoke test ==="
if [[ "$VIZ_MODE" == "systemd" ]]; then
  check "systemd fallback active" systemctl --user is-active --quiet lancedb-viz.service
else
  check "Docker container running" sh -c "test \"\$(docker inspect --format '{{.State.Running}}' lancedb-viz 2>/dev/null)\" = true"
fi
check "HTTP 200 on /" sh -c "curl -sfo /dev/null -w '%{http_code}' '$BASE_URL/' | grep -q 200"
check "stats contains memories" sh -c "curl -fsS '$BASE_URL/api/stats' | python3 -c 'import json,sys; assert json.load(sys.stdin).get(\"total_memories\", 0) > 0'"
check "dashboard returns JSON" sh -c "curl -fsS '$BASE_URL/api/dashboard' | python3 -c 'import json,sys; json.load(sys.stdin)'"
check "conflicts returns an array" sh -c "curl -fsS '$BASE_URL/api/conflicts?status=open' | python3 -c 'import json,sys; assert isinstance(json.load(sys.stdin), list)'"
check "typed edges have IDs" sh -c "curl -fsS '$BASE_URL/api/typed-edges' | python3 -c 'import json,sys; edges=json.load(sys.stdin).get(\"edges\", []); assert isinstance(edges, list); assert all(e.get(\"from\") and e.get(\"to\") for e in edges)'"
check "static app.js served" curl -fsS -o /dev/null "$BASE_URL/static/app.js"
check "static graph.js served" curl -fsS -o /dev/null "$BASE_URL/static/graph.js"
check "canonical plugin synced" diff -q "$ROOT/plugin/store.py" "$HERMES_HOME/plugins/lancedb/store.py"
check "runtime plugin synced" diff -q "$ROOT/plugin/store.py" "$HERMES_HOME/hermes-agent/plugins/memory/lancedb/store.py"

MEM_COUNT=$(curl -fsS "$BASE_URL/api/stats" | python3 -c "import json,sys; print(json.load(sys.stdin).get('total_memories','?'))" 2>/dev/null || printf '?')
printf '\n  Memories stored: %s\n' "$MEM_COUNT"
printf '=== Results: %s passed, %s failed ===\n' "$PASS" "$FAIL"
exit "$FAIL"
