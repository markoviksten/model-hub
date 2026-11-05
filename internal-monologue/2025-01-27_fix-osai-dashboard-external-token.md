# Fix: OSAI dashboard External endpoint + token + Unknown Serving Runtime

## Oireet (kuvakaappaus)
- Inference endpoints: "Internal endpoint" vaikka External route -checkbox oli valittu
- API protocol: "Not defined"
- Serving runtime: "Unknown Serving Runtime"
- Token authentication: tyhjä

## Juurisyyt ja korjaukset

### 1. Internal endpoint → External endpoint
OSAI dashboard lukee `status.url`-kentän. Se pysyi cluster-internal osoitteena.
OSAI näyttää "External endpoint" vain kun ISVC:llä on annotaatio
`serving.kserve.io/externalRoute: "true"` JA `status.url` on julkinen URL.

**Korjaus `_ensure_route`:**
- Patcha ISVC metadata.annotations: `serving.kserve.io/externalRoute: "true"`
- Patcha status.url Route-URLiksi (`--subresource=status`)

### 2. Token authentication tyhjä
OSAI luo token-secretin vain omalla "Deploy model" -flowllaan. Meidän pitää luoda
se itse: `Secret/<name>-token` jossa `serving.kserve.io/isvc-auth-token: "true"`.

**Uusi `_ensure_token_secret`:**
- Luo Secret `<name>-token` base64-enkoodatulla session-tokenilla
- Näytetään Bearer token deployment-tuloksessa kopioitavana tekstinä

### 3. Unknown Serving Runtime + API protocol Not defined
Nämä ovat OSAI-dashboardin visuaalisia ongelmia jotka johtuvat siitä, että
`onnx`-format ei matchaa ServingRuntime-rekisteriin ilman version-kenttää.
→ Malli kuitenkin toimii (smoke test OK, `activeModelState: Loaded`)

### Manuaalisesti jo olemassa olevalle navy-mallille
- Route luotu: https://navy-ships-onnx-demodemo...appdomain.cloud
- ISVC patched: externalRoute=true, status.url=Route-URL
- Secret luotu: navy-ships-onnx-token
- Token: sha256~<REDACTED>

### Uusissa deploymenteissa
Step 4 kutsuu automaattisesti sekä `_ensure_route` että `_ensure_token_secret`.
Deployment-tulos näyttää: External endpoint URL + OVMS v2 endpoint + Bearer token.
