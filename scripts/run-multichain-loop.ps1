param(
  [string[]]$Chains = @("base", "bsc"),
  [int]$IntervalSeconds = 1800,
  [int]$ScanMaxBlocks = 0,
  [int]$BaseScanMaxBlocks = 30,
  [int]$BscScanMaxBlocks = 0,
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

$Chains = @(Normalize-Chains -Values $Chains)
if ($Chains.Count -eq 0) {
  throw "At least one chain is required."
}

Write-Output "multichain loop chains=$($Chains -join ',') interval_seconds=$IntervalSeconds scan_max_blocks=$ScanMaxBlocks base_scan_max_blocks=$BaseScanMaxBlocks bsc_scan_max_blocks=$BscScanMaxBlocks max_catchup_batches=$MaxCatchupBatches"

while ($true) {
  $started = Get-Date
  try {
    & (Join-Path $PSScriptRoot "run-multichain-once.ps1") `
      -Chains $Chains `
      -ScanMaxBlocks $ScanMaxBlocks `
      -BaseScanMaxBlocks $BaseScanMaxBlocks `
      -BscScanMaxBlocks $BscScanMaxBlocks `
      -MaxCatchupBatches $MaxCatchupBatches `
      -ClassifyLimit $ClassifyLimit `
      -EnrichLimit $EnrichLimit `
      -ReportLimit $ReportLimit `
      -WebsiteVerifyLimit $WebsiteVerifyLimit `
      -WebsiteTwitterBackfillLimit $WebsiteTwitterBackfillLimit `
      -SourceUrlBackfillLimit $SourceUrlBackfillLimit `
      -NoTwitter:$NoTwitter `
      -ScanOnly:$ScanOnly
  } catch {
    Write-Error $_
  }

  $elapsed = [int]((Get-Date) - $started).TotalSeconds
  $sleep = [Math]::Max(1, $IntervalSeconds - $elapsed)
  Write-Output "multichain loop sleeping ${sleep}s"
  Start-Sleep -Seconds $sleep
}
