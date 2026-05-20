$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$data = Join-Path $root "data"
$pidFile = Join-Path $data "alpha-dashboard.pid"
$rootPattern = [regex]::Escape($root)

function Stop-DashboardProcess {
  param([int]$TargetPid)

  $process = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
  if (-not $process) {
    return $false
  }

  $children = Get-CimInstance Win32_Process | Where-Object {
    $_.ParentProcessId -eq $TargetPid -or (
      $_.Name -eq "python.exe" -and
      $_.CommandLine -match "alpha_listener\.cli dashboard" -and
      $_.CommandLine -match $rootPattern
    )
  }
  foreach ($child in $children) {
    Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
  return $true
}

if (Test-Path $pidFile) {
  $pidText = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
  $targetPid = 0
  if ([int]::TryParse($pidText, [ref]$targetPid)) {
    if (Stop-DashboardProcess -TargetPid $targetPid) {
      Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
      Write-Output "alpha-dashboard stopped pid=$targetPid"
      exit 0
    }
  }
  Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

$fallbacks = Get-CimInstance Win32_Process | Where-Object {
  (
    $_.Name -eq "powershell.exe" -and
    $_.CommandLine -match "run-dashboard\.ps1" -and
    $_.CommandLine -match $rootPattern
  ) -or (
    $_.Name -eq "python.exe" -and
    $_.CommandLine -match "alpha_listener\.cli dashboard" -and
    $_.CommandLine -match $rootPattern
  )
} | Sort-Object { if ($_.Name -eq "powershell.exe") { 0 } else { 1 } }, ProcessId

$stopped = @()
foreach ($item in $fallbacks) {
  if (Stop-DashboardProcess -TargetPid ([int]$item.ProcessId)) {
    $stopped += [int]$item.ProcessId
  }
}

if ($stopped.Count -gt 0) {
  Write-Output "alpha-dashboard stopped pid=$($stopped -join ',')"
} else {
  Write-Output "alpha-dashboard process not running"
}
