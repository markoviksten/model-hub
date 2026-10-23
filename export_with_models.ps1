<#
.SYNOPSIS
  Turns a running/stopped Model Hub container (with downloaded models) into a transferable image archive.
.DESCRIPTION
  Phase 1 (connected machine): after downloading models in the Model Hub UI, run this script.
  It commits the container (models included), sets MODEL_HUB_OFFLINE=1 as the default,
  saves the image to dist/model-hub-with-models.tar(.gz) and writes a .sha256 file.
  On the air-gapped target: docker load -i model-hub-with-models.tar.gz
.PARAMETER Container
  Container name (default model-hub).
.PARAMETER Tag
  Tag for the new image (default model-hub:airgap).
.PARAMETER Engine
  docker or podman (auto-detected by default).
.PARAMETER NoCompress
  Skip gzip (model weights compress poorly; saves time, the archive is a plain .tar).
.PARAMETER KeepOnline
  Do not set MODEL_HUB_OFFLINE=1 as the default in the new image.
#>
param(
    [string]$Container = "model-hub",
    [string]$Tag = "model-hub:airgap",
    [string]$Engine = "",
    [switch]$NoCompress,
    [switch]$KeepOnline
)
$root = $PSScriptRoot
if (-not $Engine) {
    $Engine = @("docker", "podman") | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
}
if (-not $Engine) { throw "Neither docker nor podman found." }
if ($Container -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$') { throw "Invalid container name." }
if ($Tag -notmatch '^[A-Za-z0-9][A-Za-z0-9_.:/-]*$') { throw "Invalid image tag." }

# docker/podman write progress to stderr, so don't let that abort the script.
$ErrorActionPreference = "Continue"
& $Engine inspect $Container 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Container '$Container' not found." }

$mounts = & $Engine inspect $Container --format '{{range .Mounts}}{{.Destination}} {{end}}'
if ($mounts -match '/models') {
    throw "Container '$Container' has /models on a volume or bind mount; those files are NOT included in a commit. Run it without -v /models."
}

Write-Host "Committing $Container -> $Tag (this includes /models)..."
$commitArgs = @("commit")
if (-not $KeepOnline) { $commitArgs += @("--change", "ENV MODEL_HUB_OFFLINE=1") }
$commitArgs += @($Container, $Tag)
& $Engine @commitArgs 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { throw "Commit failed." }

$out = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$tar = Join-Path $out "model-hub-with-models.tar"
$final = if ($NoCompress) { $tar } else { "$tar.gz" }
Remove-Item $tar, "$tar.gz", "$final.sha256" -ErrorAction SilentlyContinue

Write-Host "Saving image (large models take a few minutes)..."
& $Engine save -o $tar $Tag 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { throw "Image export failed." }
$ErrorActionPreference = "Stop"

if (-not $NoCompress) {
    $in = [IO.File]::OpenRead($tar)
    $dst = [IO.File]::Create($final)
    $gzs = New-Object IO.Compression.GZipStream($dst, [IO.Compression.CompressionLevel]::Optimal)
    try { $in.CopyTo($gzs) } finally { $gzs.Dispose(); $dst.Dispose(); $in.Dispose() }
    Remove-Item $tar
}

$hash = (Get-FileHash $final -Algorithm SHA256).Hash.ToLower()
$name = Split-Path $final -Leaf
"$hash  $name" | Set-Content -Encoding ascii "$final.sha256"
Write-Host ("Archive: {0} ({1:N2} GB)" -f $final, ((Get-Item $final).Length / 1GB))
Write-Host "SHA256:  $hash"
Write-Host "On the target: verify the checksum, then:  $Engine load -i $name"
