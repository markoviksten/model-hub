# Fix: PVC-skannaus timeout + rinnakkaisuus + progress bar

## Ongelmat
1. `subprocess.TimeoutExpired` — `oc exec` timeout 30s liian lyhyt suurille PVC:ille
2. 5 PVC:tä × peräkkäin = 5+ min odotus
3. Käyttäjälle ei näytetty mitään odotellessa → "Pyyntö epäonnistui"

## Korjaukset

### launcher.py
- `_exec_inspect` timeout: 30s → 90s
- `sleep 120` → `sleep 180` inspect-podissa
- `oc wait --timeout`: 60s → 90s
- **`concurrent.futures.ThreadPoolExecutor(max_workers=4)`** — kaikki PVC:t rinnakkain
  → kokonaisaika = max(yksittäinen) eikä sum(kaikki)

### index.html — progress bar
- `<div id="lnsProgress">` spinner + tekstilabel + progress bar (CSS transition)
- `lnsSetProgress(pct, text)` — päivittää molemmat
- Askeleet: 5% → 20% → 35% (PVC-haku alkaa) → 80% → 100% "Valmis"
- Piiloutuu automaattisesti 1.2s jälkeen kun valmis
- `$("lnsProgress").classList.add("hidden")` myös funktion lopussa varmuuden vuoksi
