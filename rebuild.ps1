param(
    [string]$Container = "model-hub",
    [string]$Tag = "model-hub:latest",
    [string]$Engine = ""
)

if (-not $Engine) {
    $Engine = @("docker","podman") | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
}
if (-not $Engine) { throw "Neither docker nor podman found." }

$tmp = Join-Path $env:USERPROFILE "model-hub-models-$(Get-Random)"

# 1. Tarkista onko kontti olemassa
$ErrorActionPreference = "Continue"
& $Engine inspect $Container 2>&1 | Out-Null
$containerExists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"

if ($containerExists) {
    Write-Host "Tallennetaan mallit kontista '$Container' -> $tmp ..."
    New-Item -ItemType Directory -Path $tmp | Out-Null
    & $Engine cp "${Container}:/models/." $tmp
    if ($LASTEXITCODE -ne 0) { throw "Mallien kopiointi epaonnistui." }
    $count = (Get-ChildItem $tmp | Measure-Object).Count
    Write-Host "Mallit tallennettu ($count kansiota)."
} else {
    Write-Host "Konttia '$Container' ei loydy - kaynnistetaan ilman malleja."
}

# 2. Pysayta ja poista vanha kontti
$ErrorActionPreference = "Continue"
& $Engine stop $Container 2>&1 | Out-Null
& $Engine rm   $Container 2>&1 | Out-Null
$ErrorActionPreference = "Stop"

# 3. Buildaa uusi image
Write-Host "Buildataan image '$Tag' ..."
& $Engine build -t $Tag .
if ($LASTEXITCODE -ne 0) { throw "Build epaonnistui." }

# 4. Kaynnista uusi kontti
Write-Host "Kaynnistetaan uusi kontti ..."
& $Engine run -d --name $Container --restart unless-stopped -p 127.0.0.1:8767:8767 $Tag
if ($LASTEXITCODE -ne 0) { throw "Kontin kaynnistys epaonnistui." }

# 5. Kopioi mallit takaisin
if (Test-Path $tmp) {
    Write-Host "Kopioidaan mallit takaisin konttiin ..."
    Start-Sleep -Seconds 3
    & $Engine cp "${tmp}/." "${Container}:/models/"
    if ($LASTEXITCODE -ne 0) { throw "Mallien palautus epaonnistui." }
    Remove-Item -Recurse -Force $tmp
    Write-Host "Valmis: http://127.0.0.1:8767"
} else {
    Write-Host "Valmis (ei malleja palautettavana): http://127.0.0.1:8767"
}
