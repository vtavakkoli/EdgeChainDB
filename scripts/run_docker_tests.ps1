$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

try {
  Write-Host "Building EdgeChainDB 0.8.0 and starting the benchmark..." -ForegroundColor Cyan
  # Preserve named volumes. The benchmark is a durability test, so deleting the
  # ledger before every run would hide persistence and recovery defects.
  docker compose --profile test up -d --build --force-recreate test
  if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

  $containerLine = docker compose --profile test ps --all --quiet test | Select-Object -First 1
  $containerId = "$containerLine".Trim()
  if (-not $containerId) { throw "No container found for test" }

  docker wait $containerId | Out-Null
  $exitCode = [int](docker inspect $containerId --format='{{.State.ExitCode}}')
  New-Item -ItemType Directory -Force -Path result | Out-Null
  docker logs $containerId 2>&1 | Out-File result/test-container.log -Encoding utf8

  if ($exitCode -ne 0) {
    throw "Benchmark failed with exit code $exitCode. Open result/report.html and result/benchmarks/report.html."
  }

  Write-Host "Dashboard:       http://127.0.0.1:3030" -ForegroundColor Green
  Write-Host "System report:    result/report.html" -ForegroundColor Green
  Write-Host "Research report:  result/benchmarks/report.html" -ForegroundColor Green
}
catch {
  if (Test-Path result/report.html) {
    Write-Host "Failure report: result/report.html" -ForegroundColor Yellow
  }
  throw
}
