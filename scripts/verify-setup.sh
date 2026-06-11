#!/usr/bin/env bash
# verify-setup.sh — post-install smoke test for hermes-lancedb-viz
# Run this after building the container to confirm everything wired up.

PASS=0 FAIL=0
check() { local name="$1"; shift; if "$@" 2>/dev/null; then echo "  ✓ $name"; ((PASS++)); else echo "  ✗ $name"; ((FAIL++)); fi; }

echo "=== hermes-lancedb-viz smoke test ==="
echo ""

# 1. Container running
check "Container running" docker ps --format '{{.Names}}' | grep -q lancedb-viz

# 2. HTTP responds
check "HTTP 200 on /" sh -c 'curl -sfo /dev/null -w "%{http_code}" http://localhost:7777/ | grep -q 200'

# 3. API stats
check "API /api/stats returns JSON" sh -c 'curl -s http://localhost:7777/api/stats | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get(\"total_memories\",0) > 0"'

# 4. API dashboard
check "API /api/dashboard works" sh -c 'curl -s http://localhost:7777/api/dashboard | python3 -c "import sys,json; json.load(sys.stdin)"'

# 5. Static files served
check "Static /app.js served" sh -c 'curl -sfo /dev/null -w "%{http_code}" http://localhost:7777/static/app.js | grep -q 200'
check "Static /graph.js served" sh -c 'curl -sfo /dev/null -w "%{http_code}" http://localhost:7777/static/graph.js | grep -q 200'
check "Static /style.css served" sh -c 'curl -sfo /dev/null -w "%{http_code}" http://localhost:7777/static/style.css | grep -q 200'
check "Static /vis-network.min.js served" sh -c 'curl -sfo /dev/null -w "%{http_code}" http://localhost:7777/static/vis-network.min.js | grep -q 200'

# 6. Memory count
echo ""
MEM_COUNT=$(curl -s http://localhost:7777/api/stats | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_memories','?'))")
echo "  Memories stored: $MEM_COUNT"
echo ""

# Summary
echo "=== Results: $PASS passed, $FAIL failed ==="
exit $FAIL