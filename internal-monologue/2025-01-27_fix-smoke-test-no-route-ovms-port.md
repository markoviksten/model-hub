# Fix: smoke test epäonnistui — ei ulkoista reittiä, OVMS portissa 8888

## Oireet
- navy-ships-onnx deployment onnistui (ISVC Ready, model Loaded)
- Smoke test epäonnistui

## Juurisyyt

### 1. Ei ulkoista reittiä
`status.url = https://navy-ships-onnx-predictor.demodemo.svc.cluster.local:8443`
→ cluster-internal URL, ei kutsuttavissa model-hub-containerista
→ `_get_route` palautti `None` → vanha koodi ohitti smoke-testin kokonaan

### 2. OVMS kuuntelee portissa 8888, ei 8443
`kserve-container` portit: 8888 (HTTP)
`oauth-proxy` portit: 8443 (HTTPS/TLS)
→ smoke-testi piti kutsua `localhost:8888`, ei `localhost:8443`

## Korjaukset

### `_get_route` — etsii ensin OpenShift Routea
`oc get route <name> -n <ns>` → jos löytyy → `https://<host>`
Fallback: `status.url` vain jos ei sisällä `svc.cluster.local`

### `_find_predictor_pod` — uusi funktio
Löytää Running predictor-podin labelilla `serving.kserve.io/inferenceservice=<name>`
Lukee `kserve-container`-kontin portit podista, ohittaa 8443 (TLS)
Palauttaa `(pod_name, port)`

### `_smoke` — oc exec fallback kun ei reittiä
`use_exec = route_url is None`
→ `_curl_exec(pod, "kserve-container", "http://localhost:8888/...")` via `oc exec`
→ curl ajetaan suoraan podista jolloin auth ei tarvita

### `_run_launch` — kutsuu `_smoke` aina
Ei enää `if route_url:` -ehto — `_smoke` itse päättää strategian

## Testattu manuaalisesti
`oc exec navy-ships-onnx-predictor-... -c kserve-container -- curl -sk http://localhost:8888/v2/models/navy-ships-onnx`
→ `{"name":"navy-ships-onnx","versions":["1"],"platform":"OpenVINO",...}` ✅

## Live patch
Molemmat containerit päivitetty: `model-hub` (8767) ja `model-hub-c` (8080)
