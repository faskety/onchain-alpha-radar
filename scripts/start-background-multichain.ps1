param(
  [string[]]$Chains = @("base", "bsc"),
  [int]$IntervalSeconds = 1800,
  [int]$ScanMaxBlocks = 0,
  [int]$BaseScanMaxBlocks = 30,
  [int]$BscScanMaxBlocks = 0,
  [int]$MaxCatchupBatches = 30,
  [int]$ClassifyLimit = 50,
  [int]$EnrichLimit = 10,
  [int]$ReportLimit = 50,
  [int]$WebsiteVerifyLimit = 5,
  [int]$WebsiteTwitterBackfillLimit = 5,
  [int]$SourceUrlBackfillLimit = 5,
  [switch]$NoTwitter,
  [switch]$ScanOnly
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
$data = Join-Path $root "data"
$pidFile = Join-Path $data "alpha-multichain.pid"
New-Item -ItemType Directory -Force -Path $logs, $data | Out-Null

if (Test-Path $pidFile) {
  $existingPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
  if ($existingPid -and (Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue)) {
    Write-Output "alpha-multichain already running pid=$existingPid"
    exit 0
  }
}

$stdout = Join-Path $logs "alpha-multichain.out.log"
$stderr = Join-Path $logs "alpha-multichain.err.log"
$script = Join-Path $PSScriptRoot "run-multichain-loop.ps1"
$chainArgs = @("-Chains", ($Chains -join ","))
$arguments = @(
  "-NoProfile",
  "-ExecutionPolicy",
  "Bypass",
  "-File",
  $script
) + $chainArgs + @(
  "-IntervalSeconds",
  $IntervalSeconds,
  "-ScanMaxBlocks",
  $ScanMaxBlocks,
  "-BaseScanMaxBlocks",
  $BaseScanMaxBlocks,
  "-BscScanMaxBlocks",
  $BscScanMaxBlocks,
  "-MaxCatchupBatches",
  $MaxCatchupBatches,
  "-ClassifyLimit",
  $ClassifyLimit,
  "-EnrichLimit",
  $EnrichLimit,
  "-ReportLimit",
  $ReportLimit,
  "-WebsiteVerifyLimit",
  $WebsiteVerifyLimit,
  "-WebsiteTwitterBackfillLimit",
  $WebsiteTwitterBackfillLimit,
  "-SourceUrlBackfillLimit",
  $SourceUrlBackfillLimit
)
if ($NoTwitter) {
  $arguments += "-NoTwitter"
}
if ($ScanOnly) {
  $arguments += "-ScanOnly"
}

$process = Start-Process -FilePath "powershell.exe" `
  -ArgumentList $arguments `
  -WorkingDirectory $root `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden `
  -PassThru

Set-Content -Path $pidFile -Value $process.Id -Encoding ASCII
Write-Output "alpha-multichain started pid=$($process.Id)"
Write-Output "chains=$($Chains -join ',') base_scan_max_blocks=$BaseScanMaxBlocks bsc_scan_max_blocks=$BscScanMaxBlocks max_catchup_batches=$MaxCatchupBatches"
Write-Output "stdout=$stdout"
Write-Output "stderr=$stderr"
