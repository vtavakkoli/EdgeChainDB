#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
rm -rf result
mkdir -p result
export EDGECHAIN_UID="$(id -u)"
export EDGECHAIN_GID="$(id -g)"
cleanup() {
  docker compose --profile test logs --no-color > result/docker-compose.log 2>&1 || true
  docker compose --profile test down --remove-orphans || true
}
trap cleanup EXIT

DEVICES="device-01 device-02 device-03 device-04 device-05 device-06 device-07 device-08 device-09 device-10 device-11 device-12 device-13 device-14 device-15 device-16 device-17 device-18 device-19 device-20"

docker compose --profile test down -v --remove-orphans
docker compose build
docker compose up -d gateway $DEVICES

for service in $DEVICES; do
  container_id=$(docker compose ps -q "$service")
  if [ -z "$container_id" ]; then
    echo "No container found for $service" >&2
    exit 1
  fi
  exit_code=$(docker wait "$container_id")
  if [ "$exit_code" != "0" ]; then
    echo "$service failed with exit code $exit_code" >&2
    docker logs "$container_id" >&2 || true
    exit "$exit_code"
  fi
done

docker compose --profile test run --rm test-runner

echo "Restarting the gateway to exercise persistent recovery..."
docker compose restart gateway
docker compose exec -T gateway edgechain-verify /data/edgechain.db > result/docker-restart-verification.json
docker compose --profile test run --rm test-runner \
  edgechain-recovery-check --base-url http://gateway:8000 --result-dir /app/result

echo "Report: result/report.html"
