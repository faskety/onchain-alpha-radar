param(
  [string[]]$Chains = @("base", "bsc"),
  [int]$IntervalSeconds = 1800,
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

while ($true) {
  $started = Get-Date
  try {
    & (Join-Path $PSScriptRoot "run-multichain-once.ps1") `
      -Chains $Chains `
      -ScanMaxBlocks $ScanMaxBlocks `
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
