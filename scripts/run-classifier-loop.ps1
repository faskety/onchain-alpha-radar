param(
  [int]$BatchLimit = 50,
  [int]$IntervalSeconds = 300,
  [int]$CatchupIntervalSeconds = 5
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"

while ($true) {
  $startedAt = (Get-Date).ToUniversalTime().ToString("o")
  Write-Output "[$startedAt] classify-backlog batch limit=$BatchLimit"
  $output = & python -m alpha_listener.cli classify-backlog --workspace $root --limit $BatchLimit
  $exitCode = $LASTEXITCODE
  $output | Write-Output
  if ($exitCode -ne 0) {
    throw "classify-backlog failed with exit code $exitCode"
  }

  $sleepSeconds = [Math]::Max(1, $IntervalSeconds)
  try {
    $result = ($output -join [Environment]::NewLine) | ConvertFrom-Json
    $remaining = [int]($result.remaining_classification_deferred_pending)
    $reserved = [int]($result.reserved)
    if ($remaining -gt 0 -or $reserved -ge $BatchLimit) {
      $sleepSeconds = [Math]::Max(1, $CatchupIntervalSeconds)
    }
    $sleepAt = (Get-Date).ToUniversalTime().ToString("o")
    Write-Output "[$sleepAt] classifier sleep seconds=$sleepSeconds remaining=$remaining reserved=$reserved"
  } catch {
    $sleepAt = (Get-Date).ToUniversalTime().ToString("o")
    Write-Output "[$sleepAt] classifier could not parse batch result; sleeping seconds=$sleepSeconds"
  }

  Start-Sleep -Seconds $sleepSeconds
}
