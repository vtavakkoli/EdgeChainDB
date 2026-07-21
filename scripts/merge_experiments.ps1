param(
    [string]$Config = "full-matrix.yaml",
    [string]$InputPattern = "/app/result/experiments/shard-*",
    [string]$Output = "/app/result/experiments/combined"
)
$ErrorActionPreference = "Stop"
& docker compose --profile experiment run --rm experiment `
    python -m edgechaindb.experiments.merge `
    --config "/app/experiments/$Config" `
    --input $InputPattern `
    --output $Output
if ($LASTEXITCODE -ne 0) { throw "Merge failed with exit code $LASTEXITCODE" }
Write-Host "Combined report: result/experiments/combined/report.html"
