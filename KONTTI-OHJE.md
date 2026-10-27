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

## Pikakomennot (bash)

Kansiossa `model_hub` (Git Bash, Linux, macOS, WSL):

| Komento | Mitä tekee |
|---|---|
| `./start.sh` | Rakentaa imagen tarvittaessa, luo kontin ensimmäisellä kerralla, muuten käynnistää olemassa olevan (mallit säilyvät). |
| `./stop.sh` | Pysäyttää kontin (`docker stop`). Mallit säilyvät. Ei koskaan poista konttia. |
| `./export.sh` | Tekee kopion siirtoa varten: `dist/model-hub-with-models.tar.gz` + `.sha256`. Kopioi **molemmat** tikulle. Valinnat: `--no-compress`, `--keep-online`, `-c <kontti>`, `-t <image:tagi>`. |

Air-gapped-päässä: `MODEL_HUB_IMAGE=model-hub:airgap ./start.sh` (apupodin image annetaan vasta UI:ssa, kohta 2.4) tai `docker run` kuten alla. Portin vaihto: `MODEL_HUB_PORT=8790 ./start.sh`. Podman: `ENGINE=podman ./start.sh`.

Windowsissa shellin ja Rancher/Docker Desktopin täytyy ajaa samalla oikeustasolla (molemmat tavallisena käyttäjänä tai molemmat järjestelmänvalvojana), muuten `docker` antaa virheen *Access is denied*.

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

Välilehti **1. Hae malli (internet)**: hae malli Hugging Facesta hakusanalla (tulokset sivutettu, koko GB ja päivämäärät näkyvät) tai kirjoita repo ID. Käynnissä olevan latauksen voi pysäyttää **Keskeytä lataus** -napilla; keskeneräiset tiedostot poistetaan. Seuraa levytilaa sivun alareunasta (*Levytila ja tallennetut mallit*). Tarpeettomat mallit voi poistaa **Poista**-napilla.

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

### 1.4 Kopioi tikulle

(Sovelluksen välilehden **2. Vie malli (on-prem)** alussa oleva **i**-ikoni avaa **Vie air-gappediin** -ohjeen, jossa näyttää export-komennon kopioitavana. Komento ajetaan silti isäntäkoneen terminaalissa, koska kontti ei voi paketoida itseään.)

Vaiheen 1 koneella (internet) kansiossa `model_hub/dist/` on kaksi tiedostoa. Kopioi ne tikulle mihin tahansa kansioon (esim. tikun juureen tai kansioon `model-hub-siirto`):

| Tiedosto | Pakollinen | Mihin tarvitaan |
|---|---|---|
| `dist/model-hub-with-models.tar.gz` | kyllä | Kontti-image malleineen. |
| `dist/model-hub-with-models.tar.gz.sha256` | kyllä | Tarkistussumma, jolla varmistetaan, ettei tiedosto vioittunut siirrossa. |
| `start.sh` (kansiosta `model_hub`) | suositus | Käynnistää kontin yhdellä komennolla. Ilman sitä käytä `docker run` -komentoa kohdassa 2.2. |
| `ubi9.tar` | jos klusterissa ei ole tar-imagea | Apupodin image. Teet sen näin: `docker pull registry.access.redhat.com/ubi9/ubi:latest` ja `docker save -o ubi9.tar registry.access.redhat.com/ubi9/ubi:latest`. Käyttö: kohta 2.4. |

Älä kopioi muuta: sovelluskoodi ja mallit ovat jo imagen sisällä. Tiedostot ovat isoja (arkisto on lähes yhtä suuri kuin mallit), joten tarkista, että tikulla on tarpeeksi tilaa ja että tikun tiedostojärjestelmä sallii yli 4 GB:n tiedostot (exFAT tai NTFS, ei FAT32).

## Vaihe 2 - käyttö air-gapped-ympäristössä

Koneella, jossa Model Hub ajetaan, on oltava Docker tai Podman sekä verkkoyhteys OpenShift-klusterin API:in (`oc`-kirjautumista varten; `oc` on kontin sisällä).

### 2.1 Kopioi tikulta koneelle, tarkista ja lataa

1. Kopioi tikun tiedostot air-gapped-koneelle omaan kansioon, esim. `~/model-hub-siirto` (Windows: `C:\model-hub-siirto`). Tarkista, että levyllä on vähintään arkiston koko kertaa kaksi vapaata (arkisto + ladattu image).
2. Mene kansioon ja tarkista summa:

```bash
cd ~/model-hub-siirto
sha256sum -c model-hub-with-models.tar.gz.sha256   # tulosteen pitää olla "model-hub-with-models.tar.gz: OK"
```

Windows PowerShell: `(Get-FileHash model-hub-with-models.tar.gz -Algorithm SHA256).Hash.ToLower()` ja vertaa `.sha256`-tiedoston alkuun. Jos summa ei täsmää, kopioi tiedosto uudelleen.

3. Lataa image Dockeriin:

```bash
docker load -i model-hub-with-models.tar.gz        # podman load -i ...
docker images | grep model-hub                      # näyttää model-hub:airgap
```

Latauksen jälkeen `.tar.gz`-tiedoston voi poistaa levyltä tilan säästämiseksi.

### 2.2 Käynnistä

Helpoin tapa (`start.sh` samassa kansiossa):

```bash
MODEL_HUB_IMAGE=model-hub:airgap bash ./start.sh
```

(`bash ./start.sh` toimii, vaikka tikulta kopioitu tiedosto ei olisi suoritettava.)

Tai ilman skriptiä:

```bash
docker run -d --name model-hub --restart unless-stopped -p 127.0.0.1:8767:8767 \
  model-hub:airgap
```

Käynnistyksessä ei anneta osoitteita. Apupodin image ja projekti annetaan vasta UI:n välilehdellä 2 (*Apupodin image* -kenttä); mitä siihen kirjoitetaan, selitetään kohdassa 2.4.

Avaa http://127.0.0.1:8767. Etusivun rivillä lukee, että lataus on pois käytöstä (offline). Oikeassa sarakkeessa näkyvät kontin mallit.

### 2.3 Vie malli OpenShift AI:hin

1. Välilehti **2. Vie malli (on-prem)**.
2. Liitä OpenShift-konsolin *Copy login command* -komento ja paina **Kirjaudu**.
3. Valitse malli, projekti, PVC:n nimi ja koko (koko ehdotetaan).
4. Paina **Kopioi malli PVC:lle** ja seuraa etenemistä.
5. Tee deployment OpenShift AI:ssa sovelluksen näyttämien ohjeiden mukaan (*Models → Deploy model*, Cluster storage, vLLM-runtime).

### 2.4 Loader-image: mikä se on ja mitä siihen kirjoitetaan

**Mikä se on:** kun malli kopioidaan PVC:lle, OpenShiftiin käynnistyy hetkeksi pieni apupodi, joka mounttaa PVC:n. Podille tarvitaan **konttiimage, jossa on `tar`** (UBI9 kelpaa). Oletus on `registry.access.redhat.com/ubi9/ubi:latest`, jonka klusteri hakee internetistä. Air-gapped-klusteri ei pääse internetiin, joten image täytyy tuoda **klusterin omaan rekisteriin**, ja `MODEL_HUB_LOADER_IMAGE` kertoo, mistä osoitteesta klusteri sen sieltä hakee.

**Mikä on "oma rekisteri":** sen rekisterin osoite, jota *klusterisi* (ei oma koneesi) voi käyttää imagejen hakuun. Kysy se klusterin ylläpitäjältä tai katso alta. Tyypillisiä arvoja:

| Tilanne | `MODEL_HUB_LOADER_IMAGE` |
|---|---|
| Klusteri pääsee internetiin (ei air-gapped) | jätä asettamatta (oletus toimii) |
| OpenShiftin oma sisäinen rekisteri | `image-registry.openshift-image-registry.svc:5000/<projekti>/ubi9:latest` |
| Yrityksen rekisteri (Quay, Artifactory, Harbor) | `quay.mycorp.local/mirror/ubi9/ubi:latest` |

`<projekti>` on se OpenShift-projekti, johon malli kopioidaan (sama kuin UI:n Projekti-kenttä).

**Tarkista ensin, onko sopiva image jo klusterissa** (silloin mitään ei tarvitse peilata):

```powershell
oc get is -n openshift                       # sisäisen rekisterin valmiit imaget
oc run tartest --rm -it --restart=Never --image=<kokeiltava-image> -- tar --version
```

Jos `tar --version` tulostaa version, image kelpaa. Voit käyttää sen osoitetta sellaisenaan.

**Jos sopivaa imagea ei ole, tuo ubi9 klusterin sisäiseen rekisteriin:**

```powershell
# Vaihe 1 (internet): hae ja tallenna image, kopioi tiedosto samalle medialle
docker pull registry.access.redhat.com/ubi9/ubi:latest
docker save -o ubi9.tar registry.access.redhat.com/ubi9/ubi:latest

# Vaihe 2 (air-gapped): lataa, merkitse ja työnnä klusterin rekisteriin
docker load -i ubi9.tar
oc login ...                                 # klusteriin
oc get route default-route -n openshift-image-registry   # rekisterin ulkoinen osoite (<reitti>)
docker login -u $(oc whoami) -p $(oc whoami -t) <reitti>
docker tag  registry.access.redhat.com/ubi9/ubi:latest <reitti>/<projekti>/ubi9:latest
docker push <reitti>/<projekti>/ubi9:latest
```

Jos reittiä ei ole, ylläpitäjä ottaa sen käyttöön: `oc patch configs.imageregistry.operator.openshift.io/cluster --type merge -p '{"spec":{"defaultRoute":true}}'`.

Tämän jälkeen kirjoita UI:n *Apupodin image* -kenttään podin sisäinen osoite `image-registry.openshift-image-registry.svc:5000/<projekti>/ubi9:latest`. Vaihtoehtoisesti sen voi asettaa oletukseksi käynnistyksessä:

```powershell
docker run -d --name model-hub --restart unless-stopped -p 127.0.0.1:8767:8767 `
  -e MODEL_HUB_LOADER_IMAGE=image-registry.openshift-image-registry.svc:5000/<projekti>/ubi9:latest `
  model-hub:airgap
```

Osoite kirjoitetaan UI:n *Apupodin image* -kenttään (projekti tiedetään vasta kopioinnin yhteydessä). Jos image on eri projektissa kuin kohdeprojekti, anna kohdeprojektille lupa hakea se: `oc policy add-role-to-group system:image-puller system:serviceaccounts:<kohdeprojekti> -n <imagen-projekti>`.

Jos yrityksellänne on jo oma peilirekisteri (`oc-mirror`, ImageDigestMirrorSet), pyydä ylläpitoa lisäämään `ubi9/ubi` peiliin ja käytä sen osoitetta.

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
| `ImagePullBackOff` apupodissa | Loader-imagea ei löydy klusterin rekisteristä tai osoite on väärä. Ks. 2.4. |
| `Permission denied` PVC:llä | Valitse **Aja apupodi rootina** (vaatii `anyuid`-SCC:n). |
| PVC jo käytössä | RWO-PVC voi olla vain yhden podin käytössä. Pysäytä käyttäjä tai käytä toista PVC:tä. |
| Selain: *NetworkError* | Kontti ei ole käynnissä: `docker ps -a`, `docker start model-hub`. |

## Turvallisuus

- Sovellus kuuntelee vain osoitteessa `127.0.0.1` (portti julkaistu `127.0.0.1:8767`) eikä sisällä tunnistautumista. Älä avaa porttia verkkoon.
- `oc login` -token tallennetaan vain kontin väliaikaiseen kubeconfigiin ja poistetaan uloskirjauksessa. Vaihda token, jos se on päätynyt lokiin tai chattiin.
- Kontti ajaa ei-root-käyttäjänä (UID 1001).
