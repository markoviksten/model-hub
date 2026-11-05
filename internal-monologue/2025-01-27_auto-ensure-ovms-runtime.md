# 2025-01-27 — Auto-ensure OVMS runtime + robust framework detection

## Ongelma
ovms-runtime ServingRuntime ei pysy klusterissa kontin uudelleenkäynnistyksen yli
koska se luodaan session aikana käsin eikä automaattisesti. Lisäksi framework-tunnistus
PVC-malleille perustui pelkkään kansionimeen (liian heikko — "navy-ships-ONNX" ei
sisällä sanaa "onnx" pienillä).

## Muutokset

### launcher.py

**_find_ovms_source()** — etsii klusterista parhaan OVMS ServingRuntimen
hakemalla järjestyksessä: atest, redhat-ods-applications, opendatahub.

**POST /api/ensure-ovms-runtime?namespace=<ns>** — idempotent:
- Jos "ovms-runtime" löytyy → {"status": "exists"}
- Jos ei → kopioi lähdenamespaceista, poistaa cluster-spesifiset metadatat,
  asettaa targetnamespace → {"status": "created", "source_namespace": "atest"}
- Jos lähdettä ei löydy → HTTP 404

**_list_dirs() → paranneltu framework-tunnistus** — exec-skripti tarkistaa
jokaiselle kansiolla: onko .onnx-tiedostoja, .xml-tiedostoja, numeraalinen
versioalikansio (OVMS-layout). Ei enää pelkästään kansionimeä.

### index.html

**fetchNsData()** — kutsuu /api/ensure-ovms-runtime joka kerta kun "Hae tiedot"
painetaan. Endpoint on idempotent joten turhat kutsut eivät tee mitään.
- Jos runtime luotiin → vihreä ilmoitus käyttäjälle
- Jos klusterissa ei lähdettä → keltainen varoitus (ei blokkaa)
- Runtime-dropdown haetaan uudelleen luonnin jälkeen
