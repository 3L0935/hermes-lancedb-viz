FROM python:3.12-slim

LABEL description="LanceDB Memory Graph Visualizer for Hermes Agent"
LABEL maintainer="3L0935"

WORKDIR /app

# Install minimal system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps — only lancedb + numpy (embed via Ollama API)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY server/server.py /app/server.py
COPY static/ /app/static/

# Non-root user
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g 1000 -m appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 7777
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7777/')" || exit 1

ENTRYPOINT ["python", "server.py"]
CMD ["--port", "7777", "--host", "0.0.0.0"]