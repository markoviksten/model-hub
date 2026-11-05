# 2026-11-05 — aatest: malli ei näy OSAI-dashboardissa

- Syy: namespacelta puuttui label `modelmesh-enabled=false`. Dashboard näytti "Select model serving type" eikä Models-välilehden deploymentia. ISVC oli Ready ja smoke-testi OK.
- Korjaus klusteriin: `oc label namespace aatest modelmesh-enabled=false`.
- Koodi: `launcher.py` `_run_launch` asettaa labelin; `deployer.py` lisää sen uusille namespaceille.
