# 2025-01-27 — Sovellus pyörii vanhalla koodilla, klusteritila korjattu manuaalisesti

## Tilanne
Käyttäjä raportoi toistuvat Failed-tilat navy-ships-onnx ISVC:lle. Syy:
sovelluksen Docker-image on rakennettu ennen kaikkia tämän session muutoksia
(ensure-ovms-runtime, PVC readonly, framework-tunnistus jne.). Kontti pyörii
vanhalla koodilla joka ei osaa varmistaa runtimea eikä valita oikeaa PVC:tä.

## Klusteriin tehty korjaus (manuaalisesti)
- ovms-runtime ServingRuntime luotu uudelleen demodemo-namespaceen (lähde: atest)
- navy-ships-onnx ISVC poistettu ja luotu oikeilla arvoilla:
  - storageUri: pvc://model-storage/navy-ships-ONNX  (oikea PVC)
  - runtime: ovms-runtime  (eksplisiittinen runtime)
  - memory: 4Gi (kasvatettu 2→4 Gi)
- Tulos: state=Loaded, ready=True, pod Running 2/2

## Pysyvä ratkaisu
Rebuild image + restart kontti järjestelmänvalvojana:
  cd model_hub
  docker build -t model-hub:latest .
  docker stop model-hub && docker rm model-hub
  docker run -d --name model-hub --restart unless-stopped -p 127.0.0.1:8767:8767 model-hub:latest
