[CmdletBinding()]
param(
    [string]$TargetTriple,
    [switch]$LaunchTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AbsolutePath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Require-File([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing $Description`: $Path. Build the release artifacts first, then run this script again."
    }
    $file = Get-Item -LiteralPath $Path
    if ($file.Length -le 0) {
        throw "$Description is empty: $Path"
    }
    return $file
}

function Resolve-TargetTriple([string]$RequestedTriple, [string]$BinaryRoot) {
    if (-not [string]::IsNullOrWhiteSpace($RequestedTriple)) {
        return $RequestedTriple.Trim()
    }

    $rustc = Get-Command rustc -ErrorAction SilentlyContinue
    if ($null -ne $rustc) {
        $rustInfo = (& $rustc.Source -vV 2>$null) -join "`n"
        $hostMatch = [regex]::Match($rustInfo, '(?m)^host:\s*(\S+)\s*$')
        if ($hostMatch.Success) {
            return $hostMatch.Groups[1].Value
        }
    }

    $candidates = @(Get-ChildItem -LiteralPath $BinaryRoot -Filter 'wei-daily-backend-*.exe' -File -ErrorAction SilentlyContinue)
    if ($candidates.Count -eq 1) {
        return $candidates[0].BaseName.Substring('wei-daily-backend-'.Length)
    }
    if ($candidates.Count -gt 1) {
        $names = ($candidates | ForEach-Object { $_.Name }) -join ', '
        throw "Multiple sidecar targets found; pass -TargetTriple explicitly: $names"
    }

    throw 'Unable to determine the Rust target triple; install Rust or pass -TargetTriple x86_64-pc-windows-msvc.'
}

function Write-Manifest([string]$BundleRoot, [string[]]$FileNames) {
    $lines = foreach ($name in $FileNames) {
        $path = Join-Path $BundleRoot $name
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $name"
    }
    $lines | Set-Content -LiteralPath (Join-Path $BundleRoot 'SHA256SUMS.txt') -Encoding UTF8
}

function Assert-Manifest([string]$BundleRoot, [string[]]$FileNames) {
    $manifestPath = Join-Path $BundleRoot 'SHA256SUMS.txt'
    Require-File $manifestPath 'SHA256 manifest' | Out-Null
    $manifest = @{}
    foreach ($line in Get-Content -LiteralPath $manifestPath) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split '\s{2,}', 2
        if ($parts.Count -ne 2) { throw "Invalid SHA256 manifest line: $line" }
        $manifest[$parts[1].Trim()] = $parts[0].Trim().ToLowerInvariant()
    }

    foreach ($name in $FileNames) {
        if (-not $manifest.ContainsKey($name)) { throw "SHA256 manifest is missing: $name" }
        $actual = (Get-FileHash -LiteralPath (Join-Path $BundleRoot $name) -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $manifest[$name]) {
            throw "File hash verification failed: $name"
        }
    }
}

function Assert-Zip([string]$ZipPath, [string[]]$FileNames) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entryNames = @($archive.Entries | ForEach-Object { $_.FullName.Replace('/', '\') })
        foreach ($name in $FileNames) {
            if ($entryNames -notcontains $name) {
                throw "zip is missing a required file: $name"
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}

$scriptDir = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$desktopRoot = (Resolve-Path -LiteralPath (Join-Path $scriptDir '..')).Path
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $desktopRoot '..')).Path
$tauriConfigPath = Join-Path $desktopRoot 'src-tauri\tauri.conf.json'
$tauriConfig = Get-Content -LiteralPath $tauriConfigPath -Encoding UTF8 -Raw | ConvertFrom-Json
$version = [string]$tauriConfig.version
if ([string]::IsNullOrWhiteSpace($version)) { throw "Unable to read version from $tauriConfigPath." }

$binaryRoot = Join-Path $desktopRoot 'src-tauri\binaries'
$targetRoot = Join-Path $desktopRoot 'src-tauri\target\release'
$mainSource = Join-Path $targetRoot 'wei-daily-desktop.exe'
$triple = Resolve-TargetTriple $TargetTriple $binaryRoot
$sidecarSource = Join-Path $binaryRoot "wei-daily-backend-$triple.exe"
Require-File $mainSource 'Tauri main executable' | Out-Null
Require-File $sidecarSource "sidecar for $triple" | Out-Null

$outputRoot = Join-Path $projectRoot 'output'
$portableRoot = Join-Path $outputRoot 'portable'
$portableRootFull = Get-AbsolutePath $portableRoot
$expectedPortableRoot = Get-AbsolutePath (Join-Path $projectRoot 'output\portable')
if ($portableRootFull -ne $expectedPortableRoot) {
    throw "Safety check failed: output directory is not the exact output/portable path: $portableRootFull"
}

$portableWord = -join ([char]0x4FBF, [char]0x643A, [char]0x7248)
$productName = [string]$tauriConfig.productName
if ([string]::IsNullOrWhiteSpace($productName)) { throw "Unable to read productName from $tauriConfigPath." }
$bundleName = "$productName-$portableWord-$version-win-x64"
$bundleRoot = Join-Path $portableRoot $bundleName
$zipPath = Join-Path $portableRoot "$bundleName.zip"
$readmeSource = Join-Path $scriptDir 'PORTABLE.md'
Require-File $readmeSource 'portable instructions' | Out-Null

if (Test-Path -LiteralPath $portableRoot) {
    Remove-Item -LiteralPath $portableRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $bundleRoot -Force | Out-Null

$mainName = 'wei-daily-desktop.exe'
$sidecarName = 'wei-daily-backend.exe'
$readmeName = (-join ([char]0x4F7F, [char]0x7528, [char]0x8BF4, [char]0x660E)) + '.md'
$versionName = 'version.txt'
$manifestNames = @($mainName, $sidecarName, $readmeName, $versionName)

Copy-Item -LiteralPath $mainSource -Destination (Join-Path $bundleRoot $mainName)
Copy-Item -LiteralPath $sidecarSource -Destination (Join-Path $bundleRoot $sidecarName)
Copy-Item -LiteralPath $readmeSource -Destination (Join-Path $bundleRoot $readmeName)
@(
    "Product: $($tauriConfig.productName)"
    "Version: $version"
    'Platform: Windows x64'
    "Rust target: $triple"
) | Set-Content -LiteralPath (Join-Path $bundleRoot $versionName) -Encoding UTF8

foreach ($name in $manifestNames) {
    Require-File (Join-Path $bundleRoot $name) "portable file $name" | Out-Null
}
Write-Manifest $bundleRoot $manifestNames
Assert-Manifest $bundleRoot $manifestNames

Compress-Archive -Path (Join-Path $bundleRoot '*') -DestinationPath $zipPath -CompressionLevel Optimal -Force
Require-File $zipPath 'portable zip' | Out-Null
Assert-Zip $zipPath ($manifestNames + 'SHA256SUMS.txt')

if ($LaunchTest) {
    $portableExe = Join-Path $bundleRoot $mainName
    $process = Start-Process -FilePath $portableExe -WorkingDirectory $bundleRoot -PassThru
    try {
        $deadline = (Get-Date).AddSeconds(12)
        do {
            Start-Sleep -Milliseconds 250
            $process.Refresh()
            if ($process.HasExited) {
                throw "Portable launch test failed; process exited with code $($process.ExitCode)."
            }
        } while ((Get-Date) -lt $deadline)
        Write-Host "Launch test passed: PID $($process.Id) remained alive for 12 seconds."
    }
    finally {
        $process.Refresh()
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $null = $process.WaitForExit(5000)
        }
    }
}

$bundleSize = (Get-ChildItem -LiteralPath $bundleRoot -File | Measure-Object -Property Length -Sum).Sum
$zipSize = (Get-Item -LiteralPath $zipPath).Length
Write-Host ''
Write-Host "Portable bundle: $bundleRoot"
Write-Host "Portable zip: $zipPath"
Write-Host "Files: $((Get-ChildItem -LiteralPath $bundleRoot -File).Count); folder size: $bundleSize bytes; zip size: $zipSize bytes"
Write-Host 'Verification passed: required files, SHA256 hashes, and zip entries are valid.'
