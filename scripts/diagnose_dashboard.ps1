param(
    [switch]$Repair
)

$ErrorActionPreference = "Continue"
$failures = 0

function Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Probe-Json([string]$Url, [string]$ExpectedKey, [string]$ExpectedValue) {
    try {
        $response = Invoke-RestMethod -Uri $Url -TimeoutSec 8
        if ($response.$ExpectedKey -ne $ExpectedValue) {
            throw "Expected $ExpectedKey=$ExpectedValue, received $($response.$ExpectedKey)"
        }
        Write-Host "PASS $Url" -ForegroundColor Green
        return $true
    }
    catch {
        Write-Host "FAIL $Url -- $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }
}

if ($Repair) {
    Step "Rebuilding and recreating the gateway with the real 3030 listener"
    docker compose up -d --build --force-recreate gateway
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Step "Gateway image and module command"
$rendered = docker compose config
if (($rendered -notmatch "edgechaindb:0.8.2") -or ($rendered -notmatch "edgechaindb.gateway_server")) {
    Write-Host "FAIL Compose is not using the 0.8.2 module entrypoint" -ForegroundColor Red
    $failures++
} else {
    Write-Host "PASS image edgechaindb:0.8.2 and python -m gateway entrypoint" -ForegroundColor Green
}

Step "Gateway container state"
docker compose ps gateway
if ($LASTEXITCODE -ne 0) { $failures++ }

Step "Published monitor port"
$published = docker compose port gateway 3030 2>&1
$published | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0 -or -not ($published -match "3030")) {
    Write-Host "Port 3030 is not published. Run this script with -Repair." -ForegroundColor Red
    $failures++
}

Step "API and monitor health"
if (-not (Probe-Json "http://127.0.0.1:8000/health" "status" "ok")) { $failures++ }
if (-not (Probe-Json "http://127.0.0.1:3030/monitor/health" "dashboard" "ready")) { $failures++ }

Step "Dashboard HTML marker"
try {
    $html = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:3030/" -TimeoutSec 8).Content
    if ($html -notmatch "EdgeChainDB Cluster Monitor") {
        throw "HTML marker was not found"
    }
    Write-Host "PASS dashboard HTML rendered ($($html.Length) bytes)" -ForegroundColor Green
}
catch {
    Write-Host "FAIL dashboard HTML -- $($_.Exception.Message)" -ForegroundColor Red
    $failures++
}

Step "Database metadata"
try {
    $database = Invoke-RestMethod -Uri "http://127.0.0.1:3030/database/info?quick_check=true" -TimeoutSec 60
    Write-Host "Engine: $($database.engine)"
    Write-Host "Journal: $($database.pragmas.journal_mode)"
    Write-Host "Quick check: $($database.quick_check)"
    Write-Host "Events: $($database.statistics.events), blocks: $($database.statistics.blocks)"
    if ($database.quick_check -ne "ok") { throw "SQLite quick_check failed" }
    Write-Host "PASS database integrity" -ForegroundColor Green
}
catch {
    Write-Host "FAIL database metadata -- $($_.Exception.Message)" -ForegroundColor Red
    $failures++
}

Step "Recent gateway logs"
docker compose logs --tail=120 gateway

if ($failures -gt 0) {
    Write-Host "`nDashboard diagnosis failed with $failures problem(s)." -ForegroundColor Red
    Write-Host "Repair command: .\scripts\diagnose_dashboard.ps1 -Repair" -ForegroundColor Yellow
    exit 1
}

Write-Host "`nDashboard diagnosis passed. Open http://127.0.0.1:3030" -ForegroundColor Green
