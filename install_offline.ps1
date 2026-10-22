<#
.SYNOPSIS
  Installs Model Hub Python dependencies from the bundled wheels (no internet).
#>
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
python -m pip install --no-index --find-links (Join-Path $root "wheels") -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Offline install failed (Python version / OS must match the build machine)." }
$oc = Join-Path $root "tools\oc.exe"
if (Test-Path $oc) { & $oc version --client } else { Write-Warning "tools\oc.exe missing - install oc or set OC_BIN." }
Write-Host "Done. Start with start.bat"
