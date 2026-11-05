# Fix: PVC scan käytti väärää mountPath + fallback paikallisiin malleihin

## Oireet
- "5 PVC:tä löydetty, mutta mallikansioita ei havaittu" vaikka malli oli juuri siirretty
- Silti lmodel-dropdown listasi 3 mallia (paikallisesta imagesta)

## Juurisyyt

### 1. Väärä mountPath (`launcher.py`)
`_list_dirs` rakensi `pvc_to_pod` mapin (pvc → pod_name), mutta exec-kutsu ajoi
skriptin aina `/mnt/models` polkuun riippumatta siitä, mihin pod oikeasti mounttasi PVC:n.

`wb-0`-podissa PVC on mountattu johonkin muuhun polkuun (esim. `/workspace` tai `/data`),
joten `ls /mnt/models/*/` palautti tyhjää → models=[].

**Korjaus:** Rakennetaan `pvc_to_mounts: dict[pvc_name → list[(pod/container, mountPath)]]`
käymällä läpi `pod.spec.volumes` (volume → claimName) JA
`container.volumeMounts` (volume → mountPath). Exec ajetaan oikealla `mountPath`-arvolla
ja `-c <container>` flag lisätään kun tiedossa.

### 2. Fallback paikallisiin malleihin (`index.html`)
`populateLaunchModels()` käytti `models`-muuttujaa (osion 1 haettuja paikallisia malleja)
fallbackina kun `lPvcModels.length === 0`. Tämä näytti 3 imagessa olevaa mallia vaikka
niitä ei ole millään PVC:llä eikä voi deployata InferenceServicenä.

**Korjaus:** Fallback poistettu kokonaan. Kun `lNsLoaded` on asetettu (namespace haettu)
mutta malleja ei löydy → "Ei mallikansioita PVC:llä — siirrä malli ensin osiolla 2".
Ennen haun tekemistä → "Hae tiedot ensin".

## Muutokset
- `launcher.py`: `pvc_to_pod` → `pvc_to_mounts`, exec käyttää oikeaa mountPath + -c container
- `index.html`: `populateLaunchModels` — poistettu `else if (models.length)` haara
