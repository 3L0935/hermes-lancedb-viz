#!/usr/bin/env bash
set -eo pipefail

# hermes-lancedb-viz — quick build + run
# Usage: ./scripts/docker-run.sh [--rebuild]

IMAGE="lancedb-viz:local"
NAME="lancedb-viz"
PORT="${PORT:-7777}"

if docker ps --format '{{.Names}}' | grep -q "^${NAME}$"; then
  echo "Container ${NAME} already running. Stopping..."
  docker stop "${NAME}" >/dev/null && docker rm "${NAME}" >/dev/null
fi

if [[ "$1" == "--rebuild" ]] || ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "Building ${IMAGE}..."
  docker build -t "${IMAGE}" -f Dockerfile ..
fi

echo "Starting ${NAME} on port ${PORT}..."
docker run -d \
  --name "${NAME}" \
  --restart unless-stopped \
  -p "127.0.0.1:${PORT}:7777" \
  -e OLLAMA_HOST="${OLLAMA_HOST:-http://host.docker.internal:11434}" \
  -e HERMES_HOME=/home/hermes/.hermes \
  -v "${HOME}/.hermes/lancedb:/home/hermes/.hermes/lancedb:rw" \
  -v "${HOME}/.hermes/hermes-agent:/home/hermes/.hermes/hermes-agent:ro" \
  -v "${HOME}/.hermes/lancedb-viz/static:/app/static:ro" \
  "${IMAGE}" \
  --port 7777 --host 0.0.0.0

echo "Waiting for health check..."
sleep 3
if curl -sf "http://localhost:${PORT}" >/dev/null 2>&1; then
  echo "✓ LanceDB Viz running at http://localhost:${PORT}"
else
  echo "✗ Container started but not responding yet. Check logs: docker logs ${NAME}"
fi
