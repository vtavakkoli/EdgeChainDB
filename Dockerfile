FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends iproute2 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY edgechaindb ./edgechaindb
COPY tests ./tests
COPY experiments ./experiments
RUN pip install --upgrade pip && pip install ".[dev]" && \
    python -c "import edgechaindb.gateway_server, edgechaindb.device_node, edgechaindb.benchmark, edgechaindb.experiments.runner, edgechaindb.experiments.worker, edgechaindb.experiments.merge" && \
    python -m edgechaindb.gateway_server --help >/dev/null && \
    python -m edgechaindb.benchmark --help >/dev/null && \
    python -m edgechaindb.experiments.runner --help >/dev/null && \
    python -m edgechaindb.experiments.worker --help >/dev/null && \
    python -m edgechaindb.experiments.merge --help >/dev/null && \
    python -m edgechaindb.experiments.runner --config /app/experiments/smoke.yaml --result-dir /tmp/experiment-plan --dry-run >/dev/null && \
    groupadd --gid 1000 edgechain && \
    useradd --uid 1000 --gid edgechain --create-home --shell /usr/sbin/nologin edgechain && \
    mkdir -p /data /app/result && chown -R edgechain:edgechain /data /app/result

USER edgechain
VOLUME ["/data"]
EXPOSE 8000 3030
CMD ["python", "-m", "edgechaindb.gateway_server", "--database", "/data/edgechain.db", "--node-key", "/data/gateway.key", "--host", "0.0.0.0", "--api-port", "8000", "--monitor-port", "3030"]
