# Fix: ensure_ovms_runtime — oc process instead of namespace copy

## What changed

### launcher.py
- Deleted `_OVMS_SOURCE_NAMESPACES`, `_OVMS_FORMATS`, `_find_ovms_source()` entirely.
- Added `_OVMS_TEMPLATE_NS = "redhat-ods-applications"` and `_OVMS_TEMPLATE_NAME = "kserve-ovms"`.
- `ensure_ovms_runtime()` now:
  1. Fast-path: if `ovms-runtime` already exists → return `{"status":"exists"}`.
  2. Runs `oc process template/kserve-ovms -n redhat-ods-applications --local=false -o json`.
  3. Patches name → `ovms-runtime`, namespace → target on every returned item.
  4. Pipes a `List` manifest to `oc apply -n <ns> -f -`.
  5. Returns `{"status":"created","name":"ovms-runtime","namespace":"..."}` (no `source_namespace`).

### static/index.html
- Removed `(lähde: ${ovmsRes.source_namespace})` from the success note.
- `pvcErr=false` branch now always shows PVC names (`1 PVC: <code>model-storage</code>`)
  regardless of whether models were found — fixes the case where models exist but the
  user had no feedback about which PVCs were scanned.
- Error wording updated: "OVMS ServingRuntimen automaattinen luonti epäonnistui" (was "ei löydy klusterista").

## Why this is correct
- Copying a ServingRuntime from another namespace (e.g. `atest`) is wrong — it may carry
  wrong image refs, labels, or webhook configs tied to that namespace.
- `oc process template/kserve-ovms -n redhat-ods-applications` produces the official
  OSAI-signed ServingRuntime object, identical to what OSAI's own UI would create.
- The cluster confirmed this template exists with `oc get templates -n redhat-ods-applications`.

## Rebuild needed
Docker requires elevation on this machine. Run in an elevated PowerShell:
```
cd model_hub
docker stop model-hub-c  # or whatever the container name is
docker build -t model-hub:latest .
docker run -d --name model-hub-c --rm -p 8080:8080 model-hub:latest
```
Then restore model files if needed (they are not baked into the image):
```
docker cp <host_path>/navy-ships-ONNX model-hub-c:/app/models/
```
