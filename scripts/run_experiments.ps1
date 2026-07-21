param(
    [string]$Config = "full-matrix.yaml",
    [int]$Repetitions = 0,
    [int]$ShardCount = 1,
    [int]$ShardIndex = 0,
    [int]$MaxRuns = 0,
    [switch]$NoResume,
    [switch]$RerunFailed
)
$ErrorActionPreference = "Stop"
& docker compose build experiment
if ($LASTEXITCODE -ne 0) { throw "Image build failed with exit code $LASTEXITCODE" }
$resultDir = "/result/experiments"
if ($ShardCount -gt 1) { $resultDir = "/result/experiments/shard-{0:D3}-of-{1:D3}" -f $ShardIndex, $ShardCount }
$args = @(
    "compose", "--profile", "experiment", "run", "--rm", "experiment",
    "python", "-m", "edgechaindb.experiments.runner",
    "--config", "/app/experiments/$Config",
    "--result-dir", $resultDir,
    "--shard-count", "$ShardCount",
    "--shard-index", "$ShardIndex"
)
if (-not $NoResume) { $args += "--resume" }
if ($RerunFailed) { $args += "--rerun-failed" }
if ($Repetitions -gt 0) { $args += @("--repetitions", "$Repetitions") }
if ($MaxRuns -gt 0) { $args += @("--max-runs", "$MaxRuns") }
& docker @args
$exitCode = $LASTEXITCODE
if ($ShardCount -gt 1) { Write-Host ("Shard report: result/experiments/shard-{0:D3}-of-{1:D3}/report.html" -f $ShardIndex, $ShardCount) } else { Write-Host "Matrix report: result/experiments/report.html" }
exit $exitCode
