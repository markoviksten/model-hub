# 2025-07-28 — Continue: Rebuild still pending, token leaked again

## Tilanne
Käyttäjä kirjoittaa "continue" ja liittää viestiinsä uuden OC-tokenin
(sha256~GICUPYq3...). Se on vuodettu julkiseen chattiin.

Lähdekoodit ovat oikeassa tilassa:
- deployer.py: _rsync() käyttää oc cp (file-by-file), ei oc rsync
- launcher.py: kaikki Phase 3 -logiikka paikallaan

Container EI ole rebuildettu — dockerissa pyörii vanha koodi joka käyttää
oc rsync → tar pipe → I/O error 28 GB tiedostolle.

## Vika (I/O error selitys)
`oc rsync` Windows/Docker-ympäristössä:
1. Ei löydä paikallista rsync-binääriä → käyttää tar-puttea
2. Tar pipe lukee tiedoston host-filesystemistä (Docker mount tai lokaali)
3. 28 GB tiedosto → OS read-ahead puskuri tyhjenee → "input/output error"
4. oc cp -komento lähettää yhden tiedoston kerrallaan exec-websocketin yli
   → ei tar pipea, toimii luotettavasti

## Seuraava askel
1. Kierrätä vuotunut token välittömästi
2. Aja rebuild.ps1 elevatoituina (Admin PowerShell)
3. Kokeile kopiointi uudelleen

## Ei tarvita koodimuutoksia
Kaikki tarvittavat korjaukset ovat jo lähdekoodissa. Pelkkä rebuild riittää.
