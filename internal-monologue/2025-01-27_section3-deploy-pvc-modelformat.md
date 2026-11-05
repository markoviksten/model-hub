# 2025-01-27 — Section 3 Deploy & testi: PVC listing + model framework fix

## What was requested

1. "Hae tiedot" in section 3 should also fetch PVCs in the namespace and their model folders (placed there by section 2).
2. The source model dropdown should be populated from PVC contents, not local `/models`.
3. The deployment form was missing the `model_format` (name + version) field for OpenVINO — it's mandatory for OVMS (`openvino_ir` / `onnx`).
4. Remove WIP label from tab 3 since it's now functional.

## Changes made

### `launcher.py`

- Added `model_format_version: str` field to `LaunchRequest` (KServe modelFormat.version).
- Updated `_isvc_manifest` to include `modelFormat.version` when set.
- Updated `_smoke` to detect OVMS also via `model_format in ("openvino_ir", "onnx")`.
- Added `GET /api/pvcs` endpoint:
  - Lists all PVCs in the namespace via `oc get pvc -o json`.
  - For each PVC, tries to exec into a running pod that mounts it and `ls -1d /mnt/models/*/`.
  - Falls back to predictor pods if no direct-mount pod is found.
  - Returns `{items: [{name, capacity, phase, models: [{pvc, model_path, framework}]}]}`.
  - Framework is heuristically inferred from folder name; actual manifest reading is not possible without exec.

### `static/index.html`

- Removed WIP styling and `[WIP]` label from tab 3.
- Added `lPvcModels` state variable (`[{pvc, model_path, framework}]`).
- `fetchNsData()` now also calls `/api/pvcs` and flattens results into `lPvcModels`, then calls `populateLaunchModels()` and shows a status hint.
- `populateLaunchModels()` now prefers PVC models; option values encoded as `pvc::model_path`. Falls back to local models prefixed `local::name`.
- Added `parseLmodelValue(val)` helper to decode the encoded select value.
- Added `lmodelIsOvms()` helper that checks framework from PVC model or advisor cache.
- `onLaunchModelChange()`: auto-fills PVC name from selected PVC, auto-sets `lmodelFormat` to `openvino_ir`/`onnx`/`vLLM` with color-coded note, only loads advisor for local models.
- `fillRuntimeSelect()` and `checkRuntimeMismatch()` use `lmodelIsOvms()` instead of reading `lAdviceCache` directly.
- Added `lmodelFormatVersion` text input field in the form (optional, usually empty).
- `lgo` submit handler: parses model value to extract actual `pvc_name`/`model_path`, sends `model_format_version`, uses `lmodelIsOvms()` for runtime guard.
- `clearLaunch()` resets `lPvcModels`, `lmodelFormatVersion`, `lmodelFormat`.

## Key design decisions

- PVC model values are encoded as `pvc::model_path` to preserve both PVC name and folder path in a single `<select>` value without needing a parallel data structure lookup (though `lPvcModels` is still used for framework lookup).
- Framework detection from folder names is best-effort. When the folder name doesn't signal OVMS (e.g. user named it `my-model`), framework is `null` and defaults to vLLM. Users can override manually.
- `model_dir` in `LaunchRequest` is kept but for PVC models is set to `model_path` (not validated against local disk — `_validate_launch` doesn't call `_model_path`).
