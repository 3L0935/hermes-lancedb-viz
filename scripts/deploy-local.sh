#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_AGENT_HOME="${HERMES_AGENT_HOME:-$HERMES_HOME/hermes-agent}"
CANONICAL="$HERMES_HOME/plugins/lancedb"
RUNTIME="$HERMES_AGENT_HOME/plugins/memory/lancedb"
VIZ="$HERMES_HOME/lancedb-viz"
UNIT="$HOME/.config/systemd/user/lancedb-viz.service"
DRY_RUN=0

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
elif [[ $# -gt 0 ]]; then
  printf 'Usage: %s [--dry-run]\n' "$0" >&2
  exit 2
fi

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

run install -d "$CANONICAL" "$RUNTIME" "$VIZ/static" "$(dirname "$UNIT")"
for file in store.py __init__.py plugin.yaml; do
  run install -m 0644 "$ROOT/plugin/$file" "$CANONICAL/$file"
  run install -m 0644 "$ROOT/plugin/$file" "$RUNTIME/$file"
done
run install -m 0644 "$ROOT/server/server.py" "$VIZ/server.py"
for file in "$ROOT"/static/*; do
  run install -m 0644 "$file" "$VIZ/static/$(basename "$file")"
done
run install -m 0644 "$ROOT/systemd/lancedb-viz.service" "$UNIT"
run systemctl --user daemon-reload
run systemctl --user restart lancedb-viz.service

if [[ "$DRY_RUN" -eq 0 ]]; then
  systemctl --user is-active --quiet lancedb-viz.service
  curl -fsS http://127.0.0.1:7778/api/stats >/dev/null
  printf 'Deployment verified: plugin synced, lancedb-viz active on 127.0.0.1:7778\n'
else
  printf 'Dry-run only; no files or services changed.\n'
fi
