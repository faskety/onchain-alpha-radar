$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"
$pidFile = Join-Path $root "data\alpha-listener.pid"

if (-not (Test-Path $pidFile)) {
  Write-Output "alpha-listener pid file not found"
  exit 0
}

$pidText = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
if (-not $pidText) {
  Remove-Item -LiteralPath $pidFile -Force
  Write-Output "alpha-listener pid file was empty"
  exit 0
}

try {
  $targetPid = [int]$pidText
} catch {
  Remove-Item -LiteralPath $pidFile -Force
  Write-Output "alpha-listener pid file was invalid"
  exit 0
}

$process = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
if (-not $process) {
  Remove-Item -LiteralPath $pidFile -Force
  Write-Output "alpha-listener process not running"
  exit 0
}

$maxBlocksZeroPattern = '--max-blocks(?:=|\s+)["'']?0["'']?(?:\s|$)'
$children = Get-CimInstance Win32_Process | Where-Object {
  $_.ParentProcessId -eq $process.Id -or
  ($_.Name -eq "python.exe" -and $_.CommandLine -match "alpha_listener\.cli run" -and $_.CommandLine -match "ether-onchain-alpha-listen" -and $_.CommandLine -notmatch $maxBlocksZeroPattern)
}
python -m alpha_listener.cli runtime-interrupt --workspace $root --role scanner --reason background_stop --pid $process.Id | Out-Null
foreach ($child in $children) {
  Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
}
Stop-Process -Id $process.Id
Remove-Item -LiteralPath $pidFile -Force
Write-Output "alpha-listener stopped pid=$($process.Id)"
