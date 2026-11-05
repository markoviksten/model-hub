# 2025-01-27 — Fix: PVC-kenttä ei saa arvata, vain klusterista

## Ongelma

Aiemmassa toteutuksessa `onLaunchModelChange` arvasi PVC-nimen mallinnimestä
(`k8sName(name) + "-pvc"`). Tämä johti siihen että sovellus yritti deployta
PVC:lle jota ei ole olemassa klusterissa — schedulointi epäonnistui välittömästi
`persistentvolumeclaim not found`.

## Korjaus

- `lpvc`-kenttä muutettu `readonly`-kentäksi — käyttäjä ei voi kirjoittaa siihen.
- Arvaava fallback-logiikka poistettu kokonaan `onLaunchModelChange`-funktiosta.
- PVC-nimi täytetään ainoastaan `parseLmodelValue`-funktion kautta, joka palauttaa
  klusterista haetun PVC-nimen (osio "Hae tiedot" → /api/pvcs).
- `lgo`-handlerissa: jos `actualPvc` on tyhjä → alert ja paluu ennen lähetystä.
- `clearLaunch` nollaa myös `lpvc`- ja `lpvcSource`-kentät.
