# Model Hub – asennusohje

Model Hub hakee tai tuo mallin ja kopioi sen OpenShift AI:n PVC:lle. Ohjeessa on kaksi asennustapaa: **yhdistetty kone** (internet käytössä) ja **air-gapped** (ei internetiä).

## 1. Vaatimukset

| Vaatimus | Yhdistetty | Air-gapped (kohdekone) |
|---|---|---|
| Python 3.10+ (testattu 3.12) | kyllä | kyllä (sama minor-versio kuin paketin rakennuskoneella) |
| `oc` (OpenShift CLI) | kyllä, PATHissa tai `tools/` | mukana paketissa (`tools/oc.exe`) |
| Internet | Hugging Face + PyPI | ei tarvita |
| Oikeudet klusteriin | oc login -token, oikeus luoda PVC ja podi projektiin | sama |
| Levytila | vähintään mallin koko (tar-paketin kanssa 2x) | mallin koko |

Klusterin vaatimukset:
- OpenShift AI (RHOAI) ja StorageClass, joka tukee `ReadWriteOnce`-PVC:tä.
- Apupodin image (oletus `registry.access.redhat.com/ubi9/ubi:latest`) pitää olla haettavissa klusterista. Air-gapped-ympäristössä peilaa se omaan rekisteriin (ImageDigestMirrorSet) ja aseta osoite sovelluksen kenttään tai `MODEL_HUB_LOADER_IMAGE`.

## 2. Yhdistetty kone (Windows)

```powershell
git clone https://github.com/markoviksten/model-hub.git
cd model-hub
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-online.txt
.\start.bat
```

Avaa http://127.0.0.1:8767

`oc` löytyy automaattisesti, jos se on PATHissa. Muuten kopioi `oc.exe` kansioon `tools\` tai aseta `OC_BIN`:

```powershell
$env:OC_BIN = "C:\oc\oc.exe"
```

### Linux / macOS

```bash
git clone https://github.com/markoviksten/model-hub.git
cd model-hub
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-online.txt
MODEL_HUB_OFFLINE=0 python -m uvicorn main:app --host 127.0.0.1 --port 8767
```

## 3. Air-gapped-asennus

### 3.1 Rakenna paketti yhdistetyllä koneella

Koneen pitää olla sama OS ja arkkitehtuuri sekä sama Python-minor-versio kuin kohdekoneen (wheelit ovat alustakohtaisia).

```powershell
.\build_airgap_bundle.ps1 -OcPath C:\oc\oc.exe
```

Tulos: `dist\model_hub_airgap.zip` ja `dist\model_hub_airgap.zip.sha256`. Paketissa on sovellus, Python-wheelit (`wheels\`) ja `tools\oc.exe`. `huggingface_hub` ei ole mukana, koska lataus on offline-tilassa pois käytöstä.

### 3.2 Siirrä kohdekoneelle

Siirrä zip ja `.sha256`-tiedosto siirtomedialla. Tarkista summa kohteessa:

```powershell
(Get-FileHash model_hub_airgap.zip -Algorithm SHA256).Hash.ToLower()
# vertaa .sha256-tiedoston arvoon
```

### 3.3 Asenna ja käynnistä

```powershell
Expand-Archive model_hub_airgap.zip .
cd model_hub
python -m venv .venv
.\.venv\Scripts\Activate.ps1
.\install_offline.ps1
.\start.bat offline
```

### 3.4 Tuo malli

Lataa malli yhdistetyllä koneella Model Hubin "Hae malli" -välilehdellä ja valitse **Luo myös .tar ja SHA-256**. Siirrä tiedostot `<malli>.tar` ja `<malli>.tar.sha256` kohdekoneen kansioon `models\` ja paina välilehdellä **Tuo**. Sovellus tarkistaa SHA-256:n ja purkaa paketin.

## 3b. Konttiasennus (suositeltu: sisältää kaiken)

Kontti sisältää Pythonin, riippuvuudet, Linux-`oc`:n (SHA-256-tarkistettu), `tar`in ja `rsync`in. Hostille ei tarvita Pythonia eikä `oc`:ta, ja Linux-`oc` poistaa zip-paketin OS-yhteensopivuusongelman. Kontti ajaa ei-root-käyttäjänä (UID 1001, toimii myös satunnaisella UID:llä).

### Yhdistetty kone

```powershell
docker build -t model-hub:latest .
docker run --rm -p 127.0.0.1:8767:8767 -v ${PWD}/models:/models model-hub:latest
# tai
docker compose up
```

Podman: korvaa `docker` komennolla `podman` (esim. `podman run --rm -p 127.0.0.1:8767:8767 -v ./models:/models:Z model-hub:latest`). Avaa http://127.0.0.1:8767.

### Air-gapped

1. Yhdistetyllä koneella: `.\build_image.ps1` → `dist\model-hub-image.tar.gz` ja `.sha256` (noin 410 MB).
2. Siirrä molemmat kohteeseen ja tarkista summa (kuten kohdassa 3.2).
3. Kohteessa:
   ```powershell
   docker load -i model-hub-image.tar.gz      # podman load -i ...
   docker run --rm -p 127.0.0.1:8767:8767 -e MODEL_HUB_OFFLINE=1 `
     -e MODEL_HUB_LOADER_IMAGE=<oma-rekisteri>/ubi9/ubi:latest `
     -v ${PWD}/models:/models model-hub:latest
   ```
4. Peilaa apupodin image (`ubi9/ubi`, sisältää `tar`) clusterin rekisteriin ja aseta se `MODEL_HUB_LOADER_IMAGE`-muuttujalla.
5. Kontti voidaan myös työntää sisäiseen rekisteriin (`docker tag` + `docker push`).

Mallit ja `.tar`-paketit ovat hostin `models/`-kansiossa (volume `/models`).

### OpenShiftissä ajaminen (valinnainen)

Image toimii OpenShiftin satunnaisella UID:llä. Sovelluksessa ei ole autentikointia, joten älä luo Routea; käytä `oc port-forward`ia.

## 4. Ensimmäinen käyttö

1. **1. Hae malli**: lataa (yhdistetty) tai tuo paketti (air-gapped). Mallin voi valita valmiista listasta, hakea Hugging Facesta hakusanalla (**Hae**, max 20 tulosta latausmäärän mukaan; **Valitse** täyttää repo ID:n, **Lataa** aloittaa latauksen) tai kirjoittaa repo ID:n käsin. Haku vaatii yhteyden huggingface.co:hon eikä toimi offline-tilassa.
2. **2. Vie OpenShift AI:hin**: liitä OpenShift-konsolin *Copy login command* -komento ja paina **Kirjaudu**.
3. Valitse malli, projekti, PVC:n nimi ja koko. Koko ehdotetaan automaattisesti.
4. Paina **Kopioi malli PVC:lle** ja seuraa etenemistä (aika, nopeus ja arvio jäljellä).
5. Kun valmis, tee deployment OpenShift AI:ssa sovelluksen näyttämien ohjeiden mukaan (*Models → Deploy model*, Cluster storage, vLLM-runtime).

## 5. Asetukset

| Muuttuja | Oletus | Kuvaus |
|---|---|---|
| `PORT` | `8767` | Portti (`start.bat`) |
| `MODEL_HUB_OFFLINE` | `0` | `1` poistaa latauksen käytöstä |
| `MODEL_HUB_MODELS_DIR` | `models\` | Mallikansio |
| `MODEL_HUB_LOADER_IMAGE` | `registry.access.redhat.com/ubi9/ubi:latest` | Apupodin image |
| `OC_BIN` | `tools\oc.exe`, sitten PATH | `oc`-binäärin polku |

## 6. Vianmääritys

| Oire | Syy ja korjaus |
|---|---|
| Selain: *NetworkError when attempting to fetch resource* | Palvelin ei ole käynnissä tai se pysäytettiin. Käynnistä `start.bat` uudelleen ja päivitä sivu. |
| Lataus-napit harmaina | Offline-tila tai `huggingface_hub` puuttuu. Asenna `requirements-online.txt` ja käynnistä ilman `offline`-parametria. |
| `oc-työkalua ei löydy` | Kopioi `oc` kansioon `tools\` tai aseta `OC_BIN`. |
| `ImagePullBackOff` apupodissa | Image ei ole haettavissa. Peilaa image ja aseta osoite kenttään tai `MODEL_HUB_LOADER_IMAGE`. Imagessa pitää olla `tar`. |
| `Permission denied` PVC:llä | Valitse **Aja apupodi rootina** (vaatii `anyuid`-SCC:n). |
| PVC jo käytössä | RWO-PVC voi olla vain yhden podin käytössä. Pysäytä esim. workbench tai käytä toista PVC:tä. |
| `SHA-256 ei täsmää` | Siirto vioittui. Kopioi tar uudelleen. |
| `pip install --no-index` epäonnistuu | Python-versio tai OS ei vastaa rakennuskonetta. Rakenna paketti uudelleen oikealla versiolla. |
| Linux-kohde | Paketoi Linuxille sopiva `oc`, ei `.exe`. |

## 7. Turvallisuus

- Sovellus kuuntelee vain osoitteessa `127.0.0.1` eikä sisällä omaa tunnistautumista. Älä avaa porttia verkkoon.
- `oc login` -token tallennetaan vain väliaikaiseen kubeconfigiin ja poistetaan uloskirjauksessa. Token ei näy lokeissa.
- Vaihda token, jos se on päätynyt chattiin tai lokiin.

## 8. Poisto

Sulje palvelin ja poista kansio (ja halutessasi `models\`). Klusterista voi poistaa PVC:n komennolla `oc delete pvc <nimi> -n <projekti>`.
