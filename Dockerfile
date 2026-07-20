FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY edgechaindb ./edgechaindb
COPY tests ./tests
RUN pip install --upgrade pip && pip install ".[dev]" && \
    groupadd --gid 1000 edgechain && \
    useradd --uid 1000 --gid edgechain --create-home --shell /usr/sbin/nologin edgechain && \
    mkdir -p /data /app/result && chown -R edgechain:edgechain /data /app/result

USER edgechain
VOLUME ["/data"]
EXPOSE 8000
CMD ["edgechain-api", "--database", "/data/edgechain.db", "--node-key", "/data/gateway.key", "--host", "0.0.0.0", "--port", "8000"]
