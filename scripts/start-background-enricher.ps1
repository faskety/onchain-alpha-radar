$ErrorActionPreference = "Stop"

$recoverProcessingOlderThanMinutes = 30
$websiteVerifyLimit = 10
$websiteTwitterBackfillLimit = 10
$sourceUrlBackfillLimit = 10
$passArgs = @()
for ($i = 0; $i -lt $args.Count; $i++) {
  $arg = $args[$i]
  if ($arg -eq "-RecoverProcessingOlderThanMinutes" -or $arg -eq "--RecoverProcessingOlderThanMinutes") {
    if ($i + 1 -ge $args.Count) {
      throw "$arg requires a value"
    }
    $recoverProcessingOlderThanMinutes = [int]$args[$i + 1]
    $i += 1
  } elseif ($arg -eq "-WebsiteVerifyLimit" -or $arg -eq "--WebsiteVerifyLimit" -or $arg -eq "--verify-websites-limit") {
    if ($i + 1 -ge $args.Count) {
      throw "$arg requires a value"
    }
    $websiteVerifyLimit = [int]$args[$i + 1]
    $i += 1
  } elseif ($arg -eq "-WebsiteTwitterBackfillLimit" -or $arg -eq "--WebsiteTwitterBackfillLimit" -or $arg -eq "--backfill-website-twitter-limit") {
    if ($i + 1 -ge $args.Count) {
      throw "$arg requires a value"
    }
    $websiteTwitterBackfillLimit = [int]$args[$i + 1]
    $i += 1
  } elseif ($arg -eq "-SourceUrlBackfillLimit" -or $arg -eq "--SourceUrlBackfillLimit" -or $arg -eq "--backfill-source-urls-limit") {
    if ($i + 1 -ge $args.Count) {
      throw "$arg requires a value"
    }
    $sourceUrlBackfillLimit = [int]$args[$i + 1]
    $i += 1
  } else {
    $passArgs += $arg
  }
}

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
$data = Join-Path $root "data"
$pidFile = Join-Path $data "alpha-enricher.pid"
New-Item -ItemType Directory -Force -Path $logs, $data | Out-Null

if (Test-Path $pidFile) {
  $existingPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
  if ($existingPid -and (Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue)) {
    Write-Output "alpha-enricher already running pid=$existingPid"
    exit 0
  }
}

$env:PYTHONPATH = Join-Path $root "src"
$recoverMinutes = [Math]::Max(0, $recoverProcessingOlderThanMinutes)
python -m alpha_listener.cli recover-processing --workspace $root --older-than-minutes $recoverMinutes | Out-Null

$stdout = Join-Path $logs "alpha-enricher.out.log"
$stderr = Join-Path $logs "alpha-enricher.err.log"
$script = Join-Path $PSScriptRoot "run-listener.ps1"
$arguments = @(
  "-NoProfile",
  "-ExecutionPolicy",
  "Bypass",
  "-File",
  $script,
  "--max-blocks",
  "0",
  "--enrich-limit",
  "50",
  "--report-limit",
  "50",
  "--verify-websites-limit",
  $websiteVerifyLimit,
  "--backfill-website-twitter-limit",
  $websiteTwitterBackfillLimit,
  "--backfill-source-urls-limit",
  $sourceUrlBackfillLimit
) + $passArgs

$process = Start-Process -FilePath "powershell.exe" `
  -ArgumentList $arguments `
  -WorkingDirectory $root `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden `
  -PassThru

Set-Content -Path $pidFile -Value $process.Id -Encoding ASCII
Write-Output "alpha-enricher started pid=$($process.Id)"
Write-Output "stdout=$stdout"
Write-Output "stderr=$stderr"
