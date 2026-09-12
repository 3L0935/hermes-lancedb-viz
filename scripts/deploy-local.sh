#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT_HOME="${HERMES_AGENT_HOME:-$HERMES_HOME/hermes-agent}"
CANONICAL="$HERMES_HOME/plugins/lancedb"
RUNTIME="$HERMES_AGENT_HOME/plugins/memory/lancedb"
VIZ="$HERMES_HOME/lancedb-viz"
UNIT="$HOME/.config/systemd/user/lancedb-viz.service"
MAINTENANCE_UNIT="$HOME/.config/systemd/user/lancedb-viz-maintenance.service"
MAINTENANCE_TIMER="$HOME/.config/systemd/user/lancedb-viz-maintenance.timer"
DRY_RUN=0
SYSTEMD_FALLBACK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --systemd-fallback) SYSTEMD_FALLBACK=1 ;;
    *)
      printf 'Usage: %s [--dry-run] [--systemd-fallback]\n' "$0" >&2
      exit 2
      ;;
  esac
  shift
done

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

wait_for_http() {
  local url="$1"
  local attempt
  for attempt in {1..20}; do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf 'Timed out waiting for %s\n' "$url" >&2
  return 1
}

run install -d "$CANONICAL" "$RUNTIME" "$VIZ/static" "$VIZ/scripts" "$(dirname "$UNIT")"
for file in store.py memory_contract.py __init__.py plugin.yaml; do
  run install -m 0644 "$ROOT/plugin/$file" "$CANONICAL/$file"
  run install -m 0644 "$ROOT/plugin/$file" "$RUNTIME/$file"
done
run install -m 0644 "$ROOT/server/server.py" "$VIZ/server.py"
# server.py imports maintenance.py (health diagnostics + manual compaction);
# without it the deployed viz fails to import and serves no new routes.
run install -m 0644 "$ROOT/server/maintenance.py" "$VIZ/maintenance.py"
run install -m 0755 "$ROOT/scripts/compact-if-needed.py" "$VIZ/scripts/compact-if-needed.py"
for file in "$ROOT"/static/*; do
  run install -m 0644 "$file" "$VIZ/static/$(basename "$file")"
done
run install -m 0644 "$ROOT/systemd/lancedb-viz.service" "$UNIT"
run install -m 0644 "$ROOT/systemd/lancedb-viz-maintenance.service" "$MAINTENANCE_UNIT"
run install -m 0644 "$ROOT/systemd/lancedb-viz-maintenance.timer" "$MAINTENANCE_TIMER"
run systemctl --user daemon-reload

if [[ "$SYSTEMD_FALLBACK" -eq 1 ]]; then
  run systemctl --user restart lancedb-viz.service
else
  run docker restart lancedb-viz
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  if [[ "$SYSTEMD_FALLBACK" -eq 1 ]]; then
    systemctl --user is-active --quiet lancedb-viz.service
    wait_for_http http://127.0.0.1:7778/
    printf 'Deployment verified: optional systemd fallback active on 127.0.0.1:7778\n'
  else
    docker inspect --format '{{.State.Running}}' lancedb-viz | grep -qx true
    wait_for_http http://127.0.0.1:7777/
    run systemctl --user enable --now lancedb-viz-maintenance.timer
    printf 'Deployment verified: primary Docker viz active on 127.0.0.1:7777\n'
  fi
else
  if [[ "$SYSTEMD_FALLBACK" -eq 0 ]]; then
    run systemctl --user enable --now lancedb-viz-maintenance.timer
  fi
  printf 'Dry-run only; no files or services changed.\n'
fi
