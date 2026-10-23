# Model Hub – konttiohje: lataus verkossa, vienti air-gapped-ympäristöön

Mallit tallennetaan **kontin sisään** (`/models`, ei isäntäkoneen kansiota eikä volumea). Kun kontti viedään image-arkistona, mallit tulevat mukana.

```
VAIHE 1 (internet)                              VAIHE 2 (air-gapped)
-------------------------                      ---------------------------
build -> run -> lataa mallit UI:ssa            docker load -> run
      -> export_with_models.ps1   --siirto-->   -> Vie OpenShift AI:hin
      (model-hub-with-models.tar.gz + .sha256)  (oc rsync PVC:lle)
```

## 0. Vaatimukset

| | Vaihe 1 | Vaihe 2 |
|---|---|---|
| Docker tai Podman | kyllä | kyllä |
| Internet | kyllä (huggingface.co, image-rekisterit) | ei |
| Levytila | mallit + sama uudelleen exportissa (noin 2–3 × mallien koko) | arkiston koko + purettu image (noin 2 × mallien koko) |
| Pääsy OpenShiftiin | ei tarvita | kyllä (`oc login`) |

## Vaihe 1 – lataus yhdistetyllä koneella

### 1.1 Rakenna ja käynnistä

```powershell
cd model_hub
docker build -t model-hub:latest .
docker run -d --name model-hub -p 127.0.0.1:8767:8767 model-hub:latest
```

**Älä käytä `-v`/`--rm` mallikansiolle.** `--rm` poistaisi kontin (ja mallit) pysäytettäessä, ja volume/bind mount jäisi pois exportista. `docker compose up -d` toimii samoin (compose-tiedostossa ei ole volumea).

Avaa http://127.0.0.1:8767.

### 1.2 Lataa mallit

Välilehti **1. Hae malli**: hae malli Hugging Facesta hakusanalla (tulokset sivutettu, koko GB ja päivämäärät näkyvät) tai kirjoita repo ID. Seuraa levytilaa sivun alareunasta (*Levytila ja tallennetut mallit*). Tarpeettomat mallit voi poistaa **Poista**-napilla.

- Gated-mallit: anna HF-token (se ei tallennu).

### 1.3 Vie kontti malleineen

Kontin tulee olla olemassa (käynnissä tai pysäytettynä), ja siinä ei saa olla mallikansion mounttia.

```powershell
.\export_with_models.ps1
```

Skripti:
1. tekee `docker commit` -komennon (mallit mukaan),
2. asettaa uuden imagen oletukseksi `MODEL_HUB_OFFLINE=1`,
3. tallentaa imagen tiedostoon `dist\model-hub-with-models.tar.gz` (tai `.tar` valinnalla `-NoCompress`),
4. luo SHA-256-tiedoston `dist\model-hub-with-models.tar.gz.sha256`.

Valinnat: `-Container <nimi>`, `-Tag <nimi:tagi>` (oletus `model-hub:airgap`), `-Engine podman`, `-NoCompress`, `-KeepOnline`.

Mallipainot pakkautuvat huonosti, joten arkisto on lähes yhtä suuri kuin mallit. `-NoCompress` säästää aikaa.

### 1.4 Siirrä

Kopioi **molemmat** tiedostot siirtomedialle:
- `model-hub-with-models.tar.gz`
- `model-hub-with-models.tar.gz.sha256`

Lisäksi air-gapped-ympäristön tarvitsema **loader-image** (`ubi9/ubi`, sisältää `tar`) pitää olla peilattuna clusterin rekisteriin (ks. kohta Vaihe 2.4).

## Vaihe 2 – käyttö air-gapped-ympäristössä

### 2.1 Tarkista ja lataa image

```powershell
(Get-FileHash model-hub-with-models.tar.gz -Algorithm SHA256).Hash.ToLower()
# vertaa .sha256-tiedostoon; jos ei täsmää, kopioi uudelleen
docker load -i model-hub-with-models.tar.gz        # podman load -i ...
```

Linux: `sha256sum -c model-hub-with-models.tar.gz.sha256`.

### 2.2 Käynnistä

```powershell
docker run -d --name model-hub --restart unless-stopped -p 127.0.0.1:8767:8767 `
  -e MODEL_HUB_LOADER_IMAGE=<oma-rekisteri>/ubi9/ubi:latest `
  model-hub:airgap
```

Avaa http://127.0.0.1:8767. Etusivun rivillä lukee, että lataus on pois käytöstä (offline). Sivun alareunassa näkyvät kontin mallit.

### 2.3 Vie malli OpenShift AI:hin

1. Välilehti **2. Vie OpenShift AI:hin**.
2. Liitä OpenShift-konsolin *Copy login command* -komento ja paina **Kirjaudu**.
3. Valitse malli, projekti, PVC:n nimi ja koko (koko ehdotetaan).
4. Paina **Kopioi malli PVC:lle** ja seuraa etenemistä.
5. Tee deployment OpenShift AI:ssa sovelluksen näyttämien ohjeiden mukaan (*Models → Deploy model*, Cluster storage, vLLM-runtime).

### 2.4 Loader-image ja rekisteri

Kopiointi käyttää clusterissa apupodia (`MODEL_HUB_LOADER_IMAGE`, oletus `registry.access.redhat.com/ubi9/ubi:latest`). Air-gapped-clusterissa sitä ei pääse hakemaan internetistä, joten:

1. Peilaa image vaiheessa 1 (`skopeo copy` / `oc image mirror`) sisäiseen rekisteriin ja siirrä se samalla medialla.
2. Lisää tarvittaessa `ImageDigestMirrorSet`.
3. Anna osoite `-e MODEL_HUB_LOADER_IMAGE=...` tai UI:n kenttään.

### 2.5 Tilan vapauttaminen

Kun malli on PVC:llä, sen voi poistaa kontista UI:n **Poista**-napilla (vapauttaa tilaa kontin kirjoituskerroksessa). Image-arkisto itse ei pienene, se poistetaan käsin.

## Mallien lisääminen myöhemmin

Toista vaihe 1 samalla kontilla: `docker start model-hub`, lataa lisää malleja, aja `.\export_with_models.ps1` ja siirrä uusi arkisto. Vanha image kannattaa poistaa kohteesta (`docker rmi model-hub:airgap`) ennen uuden latausta, jotta levytila ei lopu.

## Tärkeät varoitukset

| Toiminto | Seuraus |
|---|---|
| `docker rm`, `docker rm -f`, `docker run --rm` + pysäytys, `docker compose down` | **Kontti ja sen mallit poistuvat.** Pysäytä komennolla `docker stop` / `docker compose stop`. |
| `docker run -v ...:/models` | Mallit ovat volumella eivätkä tule exportiin (skripti kieltäytyy). |
| Kontin pysäyttäminen ja käynnistys (`stop`/`start`, uudelleenkäynnistys) | Mallit säilyvät. |
| Image-päivitys (`docker build` uudelleen) | Uusi kontti on tyhjä. Tee exportin jälkeen uusi kontti vain, jos mallit on talletettu arkistoon. |

## Levytila

- Mallit asuvat kontin kirjoituskerroksessa Docker/Podman-tallennustilassa (Rancher Desktopissa VM:n levy). Jos tilaa ei riitä, kasvata VM:n levyä (Rancher/Docker Desktop → asetukset) tai siirrä Docker data-root isommalle levylle.
- Sovellus näyttää kontin sisäisen vapaan tilan ja laskee, mahtuuko uusi malli.
- Poista turhat mallit **Poista**-napilla ennen exportia, jotta arkisto pysyy pienenä.

## Vianmääritys

| Oire | Syy ja korjaus |
|---|---|
| `export_with_models.ps1`: "has /models on a volume" | Kontti ajettiin `-v`:llä. Aja se ilman, lataa mallit uudelleen. |
| Mallit puuttuvat kohteessa | Export tehtiin uudesta tyhjästä kontista tai väärästä containerista. Tarkista `-Container`. |
| `ImagePullBackOff` apupodissa | Loader-imagea ei ole peilattu. Ks. 2.4. |
| `Permission denied` PVC:llä | Valitse **Aja apupodi rootina** (vaatii `anyuid`-SCC:n). |
| PVC jo käytössä | RWO-PVC voi olla vain yhden podin käytössä. Pysäytä käyttäjä tai käytä toista PVC:tä. |
| Selain: *NetworkError* | Kontti ei ole käynnissä: `docker ps -a`, `docker start model-hub`. |

## Turvallisuus

- Sovellus kuuntelee vain osoitteessa `127.0.0.1` (portti julkaistu `127.0.0.1:8767`) eikä sisällä tunnistautumista. Älä avaa porttia verkkoon.
- `oc login` -token tallennetaan vain kontin väliaikaiseen kubeconfigiin ja poistetaan uloskirjauksessa. Vaihda token, jos se on päätynyt lokiin tai chattiin.
- Kontti ajaa ei-root-käyttäjänä (UID 1001).
