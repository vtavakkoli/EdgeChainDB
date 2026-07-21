param(
    [switch]$Follow
)

$ErrorActionPreference = "Stop"
Write-Host "Building EdgeChainDB 0.8.3 and starting the one-day experiment campaign detached..." -ForegroundColor Cyan
docker compose up --build -d experiment
if ($LASTEXITCODE -ne 0) { throw "docker compose failed" }

docker compose ps experiment
Write-Host "Report: $PWD\result\experiments\report.html" -ForegroundColor Green
Write-Host "Progress: Get-Content .\result\experiments\progress.json" -ForegroundColor Green
if ($Follow) {
    docker compose logs -f experiment
}
