# 2026-10-29 — Fix PVC Model Listing Fallthrough Bug and Container Sync

## Root Cause Identified
In `launcher.py`'s `_list_dirs` function:
1. When checking `wb-storage` (workbench storage), `_exec_inspect` correctly found no model files in `/opt/app-root/src`.
2. However, a fallback check `oc get pods -l component=predictor` searched for any predictor pod across the namespace and executed `_exec_inspect` on `navy-ships-onnx-predictor`'s mount point `/mnt/models` (which had `1/` from subPath).
3. This erroneously attributed `1` as a model inside `wb-storage`.
4. Furthermore, KServe predictor pods mount PVCs via `subPath: <model-dir>`, which was not handled for single-model framework detection.

## Fix Applied
1. Updated `_list_dirs` in [`launcher.py`](model_hub/launcher.py:1040):
   - For PVC mounts with `subPath`, inspects the mounted directory directly with `_INSPECT_SINGLE_SH` and tags the model name as `subPath`.
   - For full PVC root mounts, inspects all subdirectories with `_INSPECT_SH`.
   - If a root mount is inspected and contains no models, returns `[]` immediately without false fallback.
   - Removed arbitrary namespace-wide predictor pod search fallthrough.
   - Harvests known models from existing `InferenceService` `storageUri` definitions as fallback.
2. Synchronized updated [`launcher.py`](model_hub/launcher.py) and [`static/index.html`](model_hub/static/index.html) into running Rancher Desktop containers (`model-hub` and `model-hub-c`) and restarted containers.
3. Verified `/api/pvcs?namespace=demodemo` returns:
   - `model-storage`: `[{'pvc': 'model-storage', 'model_path': 'navy-ships-ONNX', 'framework': 'onnx'}]`
   - `wb-storage`: `[]` (no false `1` model).
