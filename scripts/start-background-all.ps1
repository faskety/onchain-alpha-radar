param(
  [int]$ScannerIntervalSeconds = 1800,
  [int]$ClassifierBatchLimit = 50,
  [int]$ClassifierIntervalSeconds = 300,
  [int]$ClassifierCatchupIntervalSeconds = 5,
  [int]$EnricherIntervalSeconds = 60,
  [int]$WebsiteVerifyLimit = 10,
  [int]$WebsiteTwitterBackfillLimit = 10,
  [int]$SourceUrlBackfillLimit = 10,
  [int]$RecoverProcessingOlderThanMinutes = 30,
  [switch]$WithScannerEnrichment,
  [switch]$WithMultichain,
  [int]$MultichainIntervalSeconds = 1800,
  [int]$MultichainScanMaxBlocks = 0,
  [int]$MultichainBaseScanMaxBlocks = 30,
  [int]$MultichainBscScanMaxBlocks = 0,
  [int]$MultichainMaxCatchupBatches = 30,
  [int]$MultichainEnrichLimit = 10,
  [switch]$SkipDashboard,
  [string]$DashboardHost = "127.0.0.1",
  [int]$DashboardPort = 8765,
  [int]$DashboardLimit = 100,
  [int]$DashboardRefreshSeconds = 15
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"
$recoverMinutes = [Math]::Max(0, $RecoverProcessingOlderThanMinutes)
python -m alpha_listener.cli recover-processing --workspace $root --older-than-minutes $recoverMinutes | Out-Null

$listenerArgs = @("--interval-seconds", $ScannerIntervalSeconds)
if ($WithScannerEnrichment) {
  $listenerArgs = @("--with-enrichment") + $listenerArgs
}

Write-Output "starting scanner"
& (Join-Path $PSScriptRoot "start-background-listener.ps1") @listenerArgs

Write-Output "starting classifier"
& (Join-Path $PSScriptRoot "start-background-classifier.ps1") `
  -BatchLimit $ClassifierBatchLimit `
  -IntervalSeconds $ClassifierIntervalSeconds `
  -CatchupIntervalSeconds $ClassifierCatchupIntervalSeconds `
  -RecoverProcessingOlderThanMinutes $RecoverProcessingOlderThanMinutes

Write-Output "starting enricher"
& (Join-Path $PSScriptRoot "start-background-enricher.ps1") `
  -RecoverProcessingOlderThanMinutes $RecoverProcessingOlderThanMinutes `
  -WebsiteVerifyLimit $WebsiteVerifyLimit `
  -WebsiteTwitterBackfillLimit $WebsiteTwitterBackfillLimit `
  -SourceUrlBackfillLimit $SourceUrlBackfillLimit `
  --interval-seconds $EnricherIntervalSeconds

if ($WithMultichain) {
  Write-Output "starting multichain"
  & (Join-Path $PSScriptRoot "start-background-multichain.ps1") `
    -IntervalSeconds $MultichainIntervalSeconds `
    -ScanMaxBlocks $MultichainScanMaxBlocks `
    -BaseScanMaxBlocks $MultichainBaseScanMaxBlocks `
    -BscScanMaxBlocks $MultichainBscScanMaxBlocks `
    -MaxCatchupBatches $MultichainMaxCatchupBatches `
    -EnrichLimit $MultichainEnrichLimit
}

if (-not $SkipDashboard) {
  Write-Output "starting dashboard"
  & (Join-Path $PSScriptRoot "start-background-dashboard.ps1") `
    -ListenHost $DashboardHost `
    -Port $DashboardPort `
    -Limit $DashboardLimit `
    -RefreshSeconds $DashboardRefreshSeconds
}

Write-Output "background workers requested"
