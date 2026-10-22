<#
.SYNOPSIS
  Builds the Model Hub container image and exports it for air-gapped transfer.
.DESCRIPTION
  Runs on a CONNECTED machine with Docker or Podman. Produces
  dist/model-hub-image.tar.gz and a .sha256 file. On the target run:
  docker load -i model-hub-image.tar.gz
.PARAMETER Tag
  Image tag (default model-hub:latest).
.PARAMETER Engine
  docker or podman (auto-detected by default).
.PARAMETER NoExport
  Build only; skip creating the transfer archive.
#>
param(
    [string]$Tag = "model-hub:latest",
    [string]$Engine = "",
    [switch]$NoExport
)
$root = $PSScriptRoot
if (-not $Engine) {
    $Engine = @("docker", "podman") | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
}
if (-not $Engine) { throw "Neither docker nor podman found." }

# docker/podman write progress to stderr, so don't let that abort the script.
$ErrorActionPreference = "Continue"
& $Engine build -t $Tag $root 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { throw "Image build failed." }
if ($NoExport) { Write-Host "Built $Tag"; return }

$out = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$tar = Join-Path $out "model-hub-image.tar"
$gz = "$tar.gz"
Remove-Item $tar, $gz -ErrorAction SilentlyContinue
& $Engine save -o $tar $Tag 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { throw "Image export failed." }
$ErrorActionPreference = "Stop"

# gzip without extra tools
$in = [IO.File]::OpenRead($tar)
$dst = [IO.File]::Create($gz)
$gzs = New-Object IO.Compression.GZipStream($dst, [IO.Compression.CompressionLevel]::Optimal)
try { $in.CopyTo($gzs) } finally { $gzs.Dispose(); $dst.Dispose(); $in.Dispose() }
Remove-Item $tar

$hash = (Get-FileHash $gz -Algorithm SHA256).Hash.ToLower()
"$hash  model-hub-image.tar.gz" | Set-Content -Encoding ascii "$gz.sha256"
Write-Host "Image archive: $gz"
Write-Host "SHA256: $hash"
Write-Host "On the target: docker load -i model-hub-image.tar.gz"
