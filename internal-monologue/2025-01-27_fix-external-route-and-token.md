# Fix: External route + token smoke-testille

## Ongelma
- "External route" checkbox oli valittu mutta routea ei luotu
- Smoke test epäonnistui vaikka malli oli täysin valmis

## Juurisyy
OSAI RawDeployment-moodi EI luo OpenShift Routea automaattisesti.
Se luo vain:
- `<name>-predictor` Service (headless, port 8443, reencrypt TLS)  
- `<name>-metrics` Service (ClusterIP, port 8888)
- `status.url = https://<name>-predictor.demodemo.svc.cluster.local:8443` (cluster-internal)

## Korjaukset

### Uusi askel: "Luo ulkoinen reitti" (step 4)
`LAUNCH_STEPS` laajennettu 5 → 6 steppiä.
`_ensure_route(job, name, ns)`:
- Idempotent: jos route on jo olemassa → palauttaa sen URLin
- Luo `Route` jossa `spec.to.name = <name>-predictor`, `targetPort: https`, `tls.termination: reencrypt`
- Sama pattern kuin OSAI workbench-routet (`reencrypt` + `oauth-proxy`)
- Lukee takaisin generoidun hostnamen → palauttaa `https://<host>`

### `_get_token` — parannettu
Lisätty fallback SA-tokenin hakuun namespacen secreteistä.

### Manuaalisesti luotu route navy-ships-onnx:lle
`https://navy-ships-onnx-demodemo.techdemowxai1-...appdomain.cloud`
Testattu: `curl -H "Authorization: Bearer <token>" /v2/models/navy-ships-onnx`
→ `{"name":"navy-ships-onnx","versions":["1"],"platform":"OpenVINO",...}` ✅
