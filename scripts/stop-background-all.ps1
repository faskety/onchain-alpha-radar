$ErrorActionPreference = "Stop"

Write-Output "stopping scanner"
& (Join-Path $PSScriptRoot "stop-background-listener.ps1")

Write-Output "stopping classifier"
& (Join-Path $PSScriptRoot "stop-background-classifier.ps1")

Write-Output "stopping enricher"
& (Join-Path $PSScriptRoot "stop-background-enricher.ps1")

Write-Output "stopping multichain"
& (Join-Path $PSScriptRoot "stop-background-multichain.ps1")

Write-Output "background workers stop requested"
