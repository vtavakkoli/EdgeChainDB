param(
    [string]$Config = "full-matrix.yaml",
    [int]$Repetitions = 0
)
$ErrorActionPreference = "Stop"
$args = @(
    "compose", "--profile", "experiment", "run", "--rm", "experiment",
    "python", "-m", "edgechaindb.experiments.runner",
    "--config", "/app/experiments/$Config",
    "--result-dir", "/app/result/experiments",
    "--dry-run"
)
if ($Repetitions -gt 0) { $args += @("--repetitions", "$Repetitions") }
& docker @args
if ($LASTEXITCODE -ne 0) { throw "Experiment planning failed with exit code $LASTEXITCODE" }
Write-Host "Plan report: result/experiments/matrix-plan.html"
