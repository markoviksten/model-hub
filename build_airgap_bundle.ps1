<#
.SYNOPSIS
  Builds the air-gapped Model Hub bundle on a CONNECTED Windows machine.
.DESCRIPTION
  Downloads Python wheels (offline requirements), copies oc.exe (and optional rsync.exe)
  into tools/, and zips everything into dist/model_hub_airgap.zip.
  The target machine must run the same OS/architecture and Python minor version
  (wheels such as pydantic-core are platform specific).
.PARAMETER OcPath
  Path to oc.exe to bundle. Defaults to the one found in PATH.
.PARAMETER RsyncPath
  Optional rsync.exe (+ its DLLs must be placed in tools/ manually).
.PARAMETER Out
  Output directory (default: dist next to this script).
#>
param(
    [string]$OcPath = "",
    [string]$RsyncPath = "",
    [string]$Out = ""
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if (-not $Out) { $Out = Join-Path $root "dist" }

if (-not $OcPath) {
    $cmd = Get-Command oc -ErrorAction SilentlyContinue
    if ($cmd) { $OcPath = $cmd.Source }
}
if (-not $OcPath -or -not (Test-Path $OcPath)) { throw "oc.exe not found. Pass -OcPath <path>." }

$stage = Join-Path $Out "model_hub"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage, "$stage\static", "$stage\tools", "$stage\wheels", "$stage\models" | Out-Null

# App files
foreach ($f in "main.py","settings.py","downloader.py","packages.py","deployer.py","requirements.txt","start.bat","install_offline.ps1","README.md") {
    $src = Join-Path $root $f
    if (Test-Path $src) { Copy-Item $src $stage }
}
Copy-Item (Join-Path $root "static\*") "$stage\static\" -Recurse

# External tools
Copy-Item $OcPath "$stage\tools\oc.exe"
if ($RsyncPath) {
    if (-not (Test-Path $RsyncPath)) { throw "rsync not found: $RsyncPath" }
    Copy-Item $RsyncPath "$stage\tools\rsync.exe"
}

# Python wheels (offline requirements only - no huggingface_hub)
python -m pip download -r (Join-Path $root "requirements.txt") -d "$stage\wheels" --only-binary=:all:
if ($LASTEXITCODE -ne 0) { throw "pip download failed" }

$zip = Join-Path $Out "model_hub_airgap.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $stage -DestinationPath $zip
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
"$hash  model_hub_airgap.zip" | Set-Content -Encoding ascii (Join-Path $Out "model_hub_airgap.zip.sha256")
Write-Host "Bundle: $zip"
Write-Host "SHA256: $hash"
Write-Host "Python used for wheels: $(python --version)"
