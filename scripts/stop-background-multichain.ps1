$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$data = Join-Path $root "data"
$pidFile = Join-Path $data "alpha-multichain.pid"

if (-not (Test-Path $pidFile)) {
  Write-Output "alpha-multichain pid file not found"
  exit 0
}

$pidText = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
if (-not $pidText) {
  Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
  Write-Output "alpha-multichain pid file was empty"
  exit 0
}

$process = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
if ($process) {
  $children = Get-CimInstance Win32_Process | Where-Object {
    $_.ParentProcessId -eq $process.Id -or
    ($_.Name -eq "python.exe" -and $_.CommandLine -match "alpha_listener\.cli once" -and $_.CommandLine -match "ether-onchain-alpha-listen" -and $_.CommandLine -match "--chain")
  }
  foreach ($child in $children) {
    Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Stop-Process -Id $process.Id -Force
  Write-Output "alpha-multichain stopped pid=$pidText"
} else {
  Write-Output "alpha-multichain pid=$pidText is not running"
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
