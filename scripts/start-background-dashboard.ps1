param(
  [string]$ListenHost = "127.0.0.1",
  [int]$Port = 8765,
  [int]$Limit = 100,
  [int]$RefreshSeconds = 15
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$logs = Join-Path $root "logs"
$data = Join-Path $root "data"
$pidFile = Join-Path $data "alpha-dashboard.pid"
New-Item -ItemType Directory -Force -Path $logs, $data | Out-Null

$rootPattern = [regex]::Escape($root)

if (Test-Path $pidFile) {
  $existingPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
  $parsedPid = 0
  if ([int]::TryParse($existingPid, [ref]$parsedPid) -and (Get-Process -Id $parsedPid -ErrorAction SilentlyContinue)) {
    Write-Output "alpha-dashboard already running pid=$existingPid"
    exit 0
  }
}

$existing = Get-CimInstance Win32_Process | Where-Object {
  (
    $_.Name -eq "powershell.exe" -and
    $_.CommandLine -match "run-dashboard\.ps1" -and
    $_.CommandLine -match $rootPattern
  ) -or (
    $_.Name -eq "python.exe" -and
    $_.CommandLine -match "alpha_listener\.cli dashboard" -and
    $_.CommandLine -match $rootPattern
  )
} | Sort-Object { if ($_.Name -eq "powershell.exe") { 0 } else { 1 } }, ProcessId | Select-Object -First 1

if ($existing) {
  Set-Content -Path $pidFile -Value $existing.ProcessId -Encoding ASCII
  Write-Output "alpha-dashboard already running pid=$($existing.ProcessId)"
  exit 0
}

$stdout = Join-Path $logs "alpha-dashboard.out.log"
$stderr = Join-Path $logs "alpha-dashboard.err.log"
$script = Join-Path $PSScriptRoot "run-dashboard.ps1"
$arguments = @(
  "-NoProfile",
  "-ExecutionPolicy",
  "Bypass",
  "-File",
  $script,
  "--host",
  $ListenHost,
  "--port",
  $Port,
  "--limit",
  $Limit,
  "--refresh-seconds",
  $RefreshSeconds
)

$process = Start-Process -FilePath "powershell.exe" `
  -ArgumentList $arguments `
  -WorkingDirectory $root `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden `
  -PassThru

Set-Content -Path $pidFile -Value $process.Id -Encoding ASCII
Write-Output "alpha-dashboard started pid=$($process.Id)"
Write-Output "stdout=$stdout"
Write-Output "stderr=$stderr"
