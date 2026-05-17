param(
  [string[]]$Chains = @("base", "bsc"),
  [int]$ScanMaxBlocks = 0,
  [int]$MaxCatchupBatches = 10,
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
$env:PYTHONPATH = Join-Path $root "src"

foreach ($chain in $Chains) {
  Write-Output "[$chain] scan"
  $catchupBatch = 0
  do {
    $scanArgs = @(
      "--workspace", $root,
      "--chain", $chain,
      "--no-twitter",
      "--enrich-limit", "0",
      "--report-limit", "0"
    )
    if ($ScanMaxBlocks -gt 0) {
      $scanArgs += @("--max-blocks", $ScanMaxBlocks)
    }
    $scanOutput = python -m alpha_listener.cli once @scanArgs
    $scanText = $scanOutput -join [Environment]::NewLine
    Write-Output $scanText
    $remaining = 0
    try {
      $scanJson = $scanText | ConvertFrom-Json
      $remaining = [int]($scanJson.catchup_remaining_blocks)
    } catch {
      $remaining = 0
    }
    $catchupBatch += 1
  } while ($remaining -gt 0 -and $catchupBatch -lt $MaxCatchupBatches)

  if ($ScanOnly) {
    continue
  }

  Write-Output "[$chain] classify"
  python -m alpha_listener.cli classify-backlog `
    --workspace $root `
    --chain $chain `
    --limit $ClassifyLimit

  if ($NoTwitter) {
    continue
  }

  Write-Output "[$chain] enrich"
  python -m alpha_listener.cli once `
    --workspace $root `
    --chain $chain `
    --max-blocks 0 `
    --enrich-limit $EnrichLimit `
    --report-limit $ReportLimit `
    --verify-websites-limit $WebsiteVerifyLimit `
    --backfill-website-twitter-limit $WebsiteTwitterBackfillLimit `
    --backfill-source-urls-limit $SourceUrlBackfillLimit
}
