$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (Test-Path result) { Remove-Item result -Recurse -Force }
New-Item result -ItemType Directory | Out-Null
$env:EDGECHAIN_UID = "1000"
$env:EDGECHAIN_GID = "1000"
$devices = 1..20 | ForEach-Object { "device-{0:D2}" -f $_ }
try {
  docker compose --profile test down -v --remove-orphans
  docker compose build
  docker compose up -d gateway @devices
  foreach ($service in $devices) {
    $containerId = docker compose ps -q $service
    if (-not $containerId) { throw "No container found for $service" }
    docker wait $containerId | Out-Null
    $exitCode = [int](docker inspect $containerId --format='{{.State.ExitCode}}')
    if ($exitCode -ne 0) {
      docker logs $containerId
      throw "$service failed with exit code $exitCode"
    }
  }
  docker compose --profile test run --rm test-runner
  docker compose restart gateway
  docker compose exec -T gateway edgechain-verify /data/edgechain.db | Out-File result/docker-restart-verification.json -Encoding utf8
  docker compose --profile test run --rm test-runner edgechain-recovery-check --base-url http://gateway:8000 --result-dir /app/result
  Write-Host "Report: result/report.html"
}
finally {
  docker compose --profile test logs --no-color | Out-File result/docker-compose.log -Encoding utf8
  docker compose --profile test down --remove-orphans
}
