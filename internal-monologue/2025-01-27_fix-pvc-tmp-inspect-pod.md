# Fix: PVC-skannaus vaatii väliaikaisen inspect-podin

## Juurisyy
`model-storage` PVC:tä ei mountata mihinkään podiin `demodemo`-namespacessa.
`wb-0` mounttaa vain `wb-storage`:n polkuun `/opt/app-root/src/` — ei `/mnt/models`.

Vanha koodi yritti `oc exec` olemassa oleviin podeihin mutta silloin listaus oli tyhjä.
Uusi koodi (mountPath-korjaus) ei auttanut koska yhtään mounttaavaa podia ei ole.

## Ratkaisu
`_inspect_via_tmp_pod(pvc_name)`:
1. Luo pod `mh-insp-<pvc>` (`registry.access.redhat.com/ubi9/ubi:latest`) joka mounttaa PVC:n `/mnt/models`
2. `oc wait --for=condition=Ready ... --timeout=60s`
3. Ajaa `_INSPECT_SH`-skriptin → parse entries
4. `oc delete pod ... --wait=false` (finally-lohkossa, aina pyyhitään)

`_list_dirs` kutsuu `_inspect_via_tmp_pod` vasta kun olemassa olevat podit eivät tuota tuloksia.

## Live patch
- `docker cp launcher.py model-hub-c:/app/launcher.py`
- `docker restart model-hub-c`
- `index.html` oli jo päivitetty aiemmassa patchissa (fallback poistettu)

## Testattu
- `oc run` UBI9-imagella `sleep 120` → Running-tila OK demodemo-namespacessa
- `mh-insp-<pvc>` nimi truncataan 63-merkiksi `[:63]`
