$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "logs"
$data = Join-Path $root "data"
$pidFile = Join-Path $data "alpha-listener.pid"
New-Item -ItemType Directory -Force -Path $logs, $data | Out-Null

if (Test-Path $pidFile) {
  $existingPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
  if ($existingPid -and (Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue)) {
    Write-Output "alpha-listener already running pid=$existingPid"
    exit 0
  }
}

$stdout = Join-Path $logs "alpha-listener.out.log"
$stderr = Join-Path $logs "alpha-listener.err.log"
$script = Join-Path $PSScriptRoot "run-listener.ps1"
$passArgs = @($args | Where-Object { $_ -ne "--with-enrichment" })
if ($args -contains "--with-enrichment") {
  $listenerArgs = $passArgs
} else {
  $listenerArgs = @("--no-twitter", "--enrich-limit", "0") + $passArgs
}
$arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $script) + $listenerArgs

$process = Start-Process -FilePath "powershell.exe" `
  -ArgumentList $arguments `
  -WorkingDirectory $root `
  -RedirectStandardOutput $stdout `
  -RedirectStandardError $stderr `
  -WindowStyle Hidden `
  -PassThru

Set-Content -Path $pidFile -Value $process.Id -Encoding ASCII
Write-Output "alpha-listener started pid=$($process.Id)"
Write-Output "stdout=$stdout"
Write-Output "stderr=$stderr"
