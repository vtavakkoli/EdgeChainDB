$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

try {
  docker compose --profile test down -v --remove-orphans
  docker compose build
  docker compose --profile test up -d test

  # Compose hides stopped one-shot containers unless --all is used. The previous
  # script queried only running containers, so a device that had already exited
  # successfully looked as if it had never existed.
  $containerLine = docker compose --profile test ps --all --quiet test | Select-Object -First 1
  $containerId = "$containerLine".Trim()
  if (-not $containerId) { throw "No container found for test" }

  docker wait $containerId | Out-Null
  $exitCode = [int](docker inspect $containerId --format='{{.State.ExitCode}}')
  docker logs $containerId 2>&1 | Out-File result/test-container.log -Encoding utf8

  if ($exitCode -ne 0) {
    throw "Benchmark failed with exit code $exitCode. Open result/report.html for details."
  }

  Write-Host "Dashboard: http://localhost:8000/dashboard"
  Write-Host "Report:    result/report.html"
}
catch {
  if (Test-Path result/report.html) {
    Write-Host "Failure report: result/report.html"
  }
  throw
}
