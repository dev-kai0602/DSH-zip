# Package the portable DSH payload into DSH-zip.zip.
# Excludes runtime state created on first launch and all installed modules.
param(
    [string]$Root = "D:\DSH\DSH-DSH-zip"
)

$ErrorActionPreference = 'Stop'
$payload = Join-Path $Root 'DSH-zip'
$zip = Join-Path $Root 'DSH-zip.zip'

if (-not (Test-Path $payload)) { throw "missing payload directory: $payload" }
if (Test-Path $zip) { Remove-Item $zip -Force }

Write-Host "building $zip"
$tarArgs = @(
    '-a', '-c', '-f', $zip, '-C', $Root,
    '--exclude=DSH-zip/runtime',
    '--exclude=DSH-zip/runtime/*',
    '--exclude=DSH-zip/logs',
    '--exclude=DSH-zip/logs/*',
    '--exclude=*/node_modules',
    '--exclude=*/node_modules/*',
    'DSH-zip'
)
& tar @tarArgs

if ($LASTEXITCODE -ne 0) { throw "tar failed with exit code $LASTEXITCODE" }
$zipItem = Get-Item $zip
Write-Host ("zip size: {0:N1} MB" -f ($zipItem.Length / 1MB))
