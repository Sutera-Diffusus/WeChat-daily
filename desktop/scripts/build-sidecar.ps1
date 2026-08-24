$ErrorActionPreference = 'Stop'

$desktopRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $desktopRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Python environment not found: $python" }

$rustc = Get-Command rustc -ErrorAction SilentlyContinue
if (-not $rustc) { throw 'Rust stable-msvc is required to build the Tauri package.' }
$targetTriple = (& $rustc.Source --print host-tuple).Trim()
if (-not $targetTriple) { throw 'Unable to determine the Rust target triple.' }

$buildRoot = Join-Path $desktopRoot '.sidecar-build'
$distRoot = Join-Path $buildRoot 'dist'
$workRoot = Join-Path $buildRoot 'work'
$specRoot = Join-Path $buildRoot 'spec'
$binaryRoot = Join-Path $desktopRoot 'src-tauri\binaries'
New-Item -ItemType Directory -Force -Path $distRoot,$workRoot,$specRoot,$binaryRoot | Out-Null

$webSource = Join-Path $projectRoot 'src\wechat_bridge\web'
$uiaBin = Join-Path $projectRoot '.venv\Lib\site-packages\uiautomation\bin'
$entry = Join-Path $desktopRoot 'sidecar_entry.py'
& $python -m PyInstaller --noconfirm --clean --onefile --name wei-daily-backend `
  --paths (Join-Path $projectRoot 'src') `
  --add-data "$webSource;wechat_bridge/web" `
  --add-binary "$uiaBin\*.dll;." `
  --distpath $distRoot --workpath $workRoot --specpath $specRoot $entry
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$sourceExe = Join-Path $distRoot 'wei-daily-backend.exe'
$targetExe = Join-Path $binaryRoot "wei-daily-backend-$targetTriple.exe"
Copy-Item -LiteralPath $sourceExe -Destination $targetExe -Force
Write-Host "Sidecar ready: $targetExe"
