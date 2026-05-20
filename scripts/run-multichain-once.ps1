param(
  [string[]]$Chains = @("base", "bsc"),
  [int]$ScanMaxBlocks = 0,
  [int]$BaseScanMaxBlocks = 30,
  [int]$BscScanMaxBlocks = 30,
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
$env:PYTHONPATH = Join-Path $root "src"

function Normalize-Chains {
  param([string[]]$Values)

  $normalized = @()
  foreach ($value in @($Values)) {
    foreach ($part in ($value -split ",")) {
      $chain = $part.Trim()
      if ($chain) {
        $normalized += $chain
      }
    }
  }
  return $normalized
}

function Get-ChainScanMaxBlocks {
  param([string]$Chain)
  if ($ScanMaxBlocks -gt 0) {
    return $ScanMaxBlocks
  }
  if ($Chain -eq "base" -and $BaseScanMaxBlocks -gt 0) {
    return $BaseScanMaxBlocks
  }
  if (($Chain -eq "bsc" -or $Chain -eq "bnb") -and $BscScanMaxBlocks -gt 0) {
    return $BscScanMaxBlocks
  }
  return 0
}

function Invoke-ChainPostScan {
  param([string]$Chain)

  if ($ScanOnly) {
    return
  }

  Write-Output "[$Chain] classify"
  python -m alpha_listener.cli classify-backlog `
    --workspace $root `
    --chain $Chain `
    --limit $ClassifyLimit

  if ($NoTwitter) {
    return
  }

  Write-Output "[$Chain] enrich"
  python -m alpha_listener.cli once `
    --workspace $root `
    --chain $Chain `
    --max-blocks 0 `
    --enrich-limit $EnrichLimit `
    --report-limit $ReportLimit `
    --verify-websites-limit $WebsiteVerifyLimit `
    --backfill-website-twitter-limit $WebsiteTwitterBackfillLimit `
    --backfill-source-urls-limit $SourceUrlBackfillLimit
}

$Chains = @(Normalize-Chains -Values $Chains)
if ($Chains.Count -eq 0) {
  throw "At least one chain is required."
}

Write-Output "multichain once chains=$($Chains -join ',') scan_max_blocks=$ScanMaxBlocks base_scan_max_blocks=$BaseScanMaxBlocks bsc_scan_max_blocks=$BscScanMaxBlocks max_catchup_batches=$MaxCatchupBatches"

$remainingByChain = @{}
$scanRounds = [Math]::Max(1, $MaxCatchupBatches)
for ($catchupBatch = 0; $catchupBatch -lt $scanRounds; $catchupBatch += 1) {
  $anyRemaining = $false
  foreach ($chain in $Chains) {
    if ($catchupBatch -gt 0 -and -not ($remainingByChain[$chain])) {
      continue
    }

    $batchNumber = $catchupBatch + 1
    Write-Output "[$chain] scan batch $batchNumber/$scanRounds"
    $scanArgs = @(
      "--workspace", $root,
      "--chain", $chain,
      "--no-twitter",
      "--enrich-limit", "0",
      "--report-limit", "0"
    )
    $chainScanMaxBlocks = Get-ChainScanMaxBlocks -Chain $chain
    if ($chainScanMaxBlocks -gt 0) {
      $scanArgs += @("--max-blocks", $chainScanMaxBlocks)
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
    $remainingByChain[$chain] = $remaining -gt 0
    if ($remaining -gt 0) {
      $anyRemaining = $true
    }

    Invoke-ChainPostScan -Chain $chain
  }

  if (-not $anyRemaining) {
    break
  }
}
