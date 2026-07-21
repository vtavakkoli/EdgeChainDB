$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
New-Item -ItemType Directory -Force -Path "result/experiments" | Out-Null
Write-Host "Building and starting the resumable one-day screening campaign..." -ForegroundColor Cyan
docker compose up --build -d experiment
if ($LASTEXITCODE -ne 0) { throw "Experiment service failed to start" }
Write-Host "Logs:     docker compose logs -f experiment" -ForegroundColor Green
Write-Host "Progress: result/experiments/progress.json" -ForegroundColor Green
Write-Host "Report:   result/experiments/report.html" -ForegroundColor Green
