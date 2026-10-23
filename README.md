# Model Hub

> Asennusohje: [INSTALL.md](INSTALL.md)

Yksi sovellus: malli haetaan (tai tuodaan paketista) ja viedään OpenShift AI:n PVC:lle. Toimii yhdistettynä ja air-gapped-ympäristössä.

## Välilehdet
1. **Hae malli** – lataus Hugging Facesta (haku hakusanalla tai repo ID käsin; piilossa offline-tilassa) tai tuonti `<malli>.tar` + `.sha256` -paketista (SHA-256 tarkistetaan).
2. **Vie OpenShift AI:hin** – `oc login`, projekti, PVC, loader-pod, kopiointi, tarkistus, siivous.

UI on rakennettu IBM Carbon Design Systemillä (`static/carbon.css`, IBM Plex -fontit `static/fonts/`; ei ulkoisia riippuvuuksia, toimii offline). Edistymispalkeissa näkyy kulunut aika, nopeus ja arvio jäljellä olevasta ajasta; deploy-vaiheilla on omat kestonsa.

## Yhdistetty kone
```powershell
pip install -r requirements-online.txt
start.bat
```
Avaa http://127.0.0.1:8767

## Air-gapped-paketti
Yhdistetyllä koneella (sama OS/arkkitehtuuri ja Python-minor kuin kohteessa):
```powershell
.\build_airgap_bundle.ps1 -OcPath C:\oc\oc.exe
```
→ `dist\model_hub_airgap.zip` + `.sha256`. Siirrä zip kohdekoneelle, ja kohteessa:
```powershell
Expand-Archive model_hub_airgap.zip .
cd model_hub
.\install_offline.ps1
start.bat offline
```
Paketti sisältää: sovellus, Python-wheelit (`wheels/`), `tools/oc.exe`. `rsync` valinnainen (`-RsyncPath`); ilman sitä `oc rsync` käyttää tar-varmistusta.

Mallit tuodaan siirtomedialta: kopioi `<malli>.tar` ja `<malli>.sha256` kansioon `models/` (tai käytä UI:n tuontia).

## Kontti (sisältää kaiken)
Mallit tallennetaan kontin sisään; kontti malleineen viedään air-gapped-ympäristöön image-arkistona. Täysi ohje: [KONTTI-OHJE.md](KONTTI-OHJE.md).
```powershell
# Vaihe 1 (internet): rakenna, käynnistä, lataa mallit UI:ssa, vie
docker build -t model-hub:latest .
docker run -d --name model-hub -p 127.0.0.1:8767:8767 model-hub:latest
.\export_with_models.ps1   # -> dist/model-hub-with-models.tar.gz + .sha256
# Vaihe 2 (air-gapped)
docker load -i model-hub-with-models.tar.gz
docker run -d --name model-hub -p 127.0.0.1:8767:8767 model-hub:airgap
```
Varoitus: `docker rm` ja `docker compose down` poistavat kontin ja mallit; pysäytä `docker stop`.

## Ympäristömuuttujat
| Muuttuja | Oletus |
|---|---|
| `MODEL_HUB_OFFLINE` | `0`; `start.bat offline` asettaa `1` |
| `MODEL_HUB_MODELS_DIR` | `models/` |
| `MODEL_HUB_LOADER_IMAGE` | `registry.access.redhat.com/ubi9/ubi:latest` |
| `OC_BIN` | `tools/oc.exe`, sitten PATH |
| `PORT` | `8767` |

## Rajoitteet
- `oc`-binäärin pitää vastata kohde-OS:ää (Linuxille tarvitaan Linux-`oc`, ei `.exe`).
- Loader-podin imagen (ubi9, sisältää `tar`) pitää olla peilattu clusterin rekisteriin (IDMS/ImageDigestMirrorSet); aseta `MODEL_HUB_LOADER_IMAGE`.
- Sovellus kuuntelee vain 127.0.0.1:ssä, eikä siinä ole omaa autentikointia.


