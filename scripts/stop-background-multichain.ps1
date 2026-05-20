$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"
$data = Join-Path $root "data"
$pidFile = Join-Path $data "alpha-multichain.pid"

function Get-AlphaCommandChain {
  param([string]$CommandLine)
  if ($CommandLine -match '--chain(?:=|\s+)["'']?([^"''\s]+)') {
    return $Matches[1]
  }
  return $null
}

function Get-AlphaCommandRole {
  param([string]$CommandLine)
  $maxBlocksZeroPattern = '--max-blocks(?:=|\s+)["'']?0["'']?(?:\s|$)'
  if ($CommandLine -match "alpha_listener\.cli classify-backlog") {
    return "classifier"
  }
  if ($CommandLine -match "alpha_listener\.cli once" -and $CommandLine -match $maxBlocksZeroPattern) {
    return "enricher"
  }
  if ($CommandLine -match "alpha_listener\.cli once") {
    return "scanner"
  }
  return $null
}

function Mark-RuntimeInterrupted {
  param(
    [string]$Chain,
    [string]$Role,
    [int]$Pid
  )
  if (-not $Chain -or -not $Role) {
    return
  }
  python -m alpha_listener.cli runtime-interrupt --workspace $root --chain $Chain --role $Role --reason background_stop --pid $Pid | Out-Null
}

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
    $role = Get-AlphaCommandRole -CommandLine ([string]$child.CommandLine)
    $chain = Get-AlphaCommandChain -CommandLine ([string]$child.CommandLine)
    Mark-RuntimeInterrupted -Chain $chain -Role $role -Pid ([int]$child.ProcessId)
    Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Stop-Process -Id $process.Id -Force
  Write-Output "alpha-multichain stopped pid=$pidText"
} else {
  Write-Output "alpha-multichain pid=$pidText is not running"
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
