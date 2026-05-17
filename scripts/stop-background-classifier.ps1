$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "data\alpha-classifier.pid"

if (-not (Test-Path $pidFile)) {
  Write-Output "alpha-classifier pid file not found"
  exit 0
}

$pidText = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
if (-not $pidText) {
  Remove-Item -LiteralPath $pidFile -Force
  Write-Output "alpha-classifier pid file was empty"
  exit 0
}

try {
  $targetPid = [int]$pidText
} catch {
  Remove-Item -LiteralPath $pidFile -Force
  Write-Output "alpha-classifier pid file was invalid"
  exit 0
}

$process = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
if (-not $process) {
  Remove-Item -LiteralPath $pidFile -Force
  Write-Output "alpha-classifier process not running"
  exit 0
}

$children = Get-CimInstance Win32_Process | Where-Object {
  $_.ParentProcessId -eq $process.Id -or
  ($_.Name -eq "python.exe" -and $_.CommandLine -match "alpha_listener\.cli classify-backlog" -and $_.CommandLine -match "ether-onchain-alpha-listen")
}
foreach ($child in $children) {
  Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
}
Stop-Process -Id $process.Id
Remove-Item -LiteralPath $pidFile -Force
Write-Output "alpha-classifier stopped pid=$($process.Id)"
