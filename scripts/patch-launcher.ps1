# Replace the launcher exe and README inside a portable zip.
param(
    [string]$Root = "D:\DSH\DSH-DSH-zip",
    [Parameter(Mandatory = $true)][string]$Zip
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$exe = Join-Path $Root '.build\dist-tk\DSH-Launcher.exe'
$readme = Join-Path $Root '.build\README.txt'
foreach ($f in @($exe, $readme)) { if (-not (Test-Path $f)) { throw "missing replacement source: $f" } }

$replacements = New-Object System.Collections.ArrayList
[void]$replacements.Add(@{ entry = 'DSH-zip/DSH-Launcher.exe'; source = $exe; store = $true })
[void]$replacements.Add(@{ entry = 'DSH-zip/README.txt'; source = $readme; store = $false })

$archive = [System.IO.Compression.ZipFile]::Open($Zip, [System.IO.Compression.ZipArchiveMode]::Update)
try {
    foreach ($item in $replacements) {
        $existing = $archive.GetEntry($item.entry)
        if ($existing) { $existing.Delete(); Write-Host "deleted $($item.entry)" } else { Write-Host "note: $($item.entry) was absent" }
        $level = [System.IO.Compression.CompressionLevel]::Optimal
        if ($item.store) { $level = [System.IO.Compression.CompressionLevel]::NoCompression }
        [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $item.source, $item.entry, $level)
        Write-Host "added $($item.entry)"
    }
}
finally { $archive.Dispose() }

$info = Get-Item $Zip
Write-Host ("zip bytes={0}" -f $info.Length)

$check = [System.IO.Compression.ZipFile]::OpenRead($Zip)
try {
    $check.Entries | Where-Object { $_.FullName -eq 'DSH-zip/DSH-Launcher.exe' -or $_.FullName -eq 'DSH-zip/README.txt' } | ForEach-Object { $_.FullName + '  ' + $_.Length }
} finally { $check.Dispose() }
Write-Host 'DONE'
