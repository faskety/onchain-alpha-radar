param(
  [int]$BatchLimit = 50,
  [int]$IntervalSeconds = 300,
  [int]$CatchupIntervalSeconds = 5,
  [int]$RecoverProcessingOlderThanMinutes = 30
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
$data = Join-Path $root "data"
$pidFile = Join-Path $data "alpha-classifier.pid"
New-Item -ItemType Directory -Force -Path $logs, $data | Out-Null

if (Test-Path $pidFile) {
  $existingPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
  if ($existingPid -and (Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue)) {
    Write-Output "alpha-classifier already running pid=$existingPid"
    exit 0
  }
}

$stdout = Join-Path $logs "alpha-classifier.out.log"
$stderr = Join-Path $logs "alpha-classifier.err.log"
$script = Join-Path $PSScriptRoot "run-classifier-loop.ps1"
$env:PYTHONPATH = Join-Path $root "src"
$recoverMinutes = [Math]::Max(0, $RecoverProcessingOlderThanMinutes)
python -m alpha_listener.cli recover-processing --workspace $root --older-than-minutes $recoverMinutes | Out-Null

$arguments = @(
  "-NoProfile",
  "-ExecutionPolicy",
  "Bypass",
  "-File",
  $script,
  "-BatchLimit",
  $BatchLimit,
  "-IntervalSeconds",
  $IntervalSeconds,
  "-CatchupIntervalSeconds",
  $CatchupIntervalSeconds
)

$process = Start-Process -FilePath "powershell.exe" `
  -ArgumentList $arguments `
  -WorkingDirectory $root `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden `
  -PassThru

Set-Content -Path $pidFile -Value $process.Id -Encoding ASCII
Write-Output "alpha-classifier started pid=$($process.Id)"
Write-Output "stdout=$stdout"
Write-Output "stderr=$stderr"
