# 2025-01-28 — Fix model framework + version for OVMS deployments

## Problem
Section 3 "Deploy & Test" was missing model framework version info entirely:
- `modelFormat.version` was never auto-filled for OpenVINO/ONNX models
- `onnx` format has no `autoSelect: true` in the OVMS ServingRuntime, so KServe
  could not pick a runtime — deployment would fail silently
- The UI label said "valinnainen" (optional) for version, which is wrong for OVMS
- Result tile never showed what model_format/version was actually submitted

## Root cause (confirmed from cluster)
`oc get servingruntime ovms-runtime -n demodemo` revealed:
- `openvino_ir` → version `opset13`, `autoSelect: true` ✓
- `onnx` → version `1`, **no `autoSelect`** ← KServe can't pick runtime
- `tensorflow/paddle/pytorch` → `autoSelect: true`

## Changes

### launcher.py
1. Added `_OVMS_FORMAT_VERSIONS = {"openvino_ir": "opset13", "onnx": "1"}` — canonical
   version strings required by the OVMS ServingRuntime template (kserve-ovms)
2. Added `_FORMATS_REQUIRING_EXPLICIT_RUNTIME = {"onnx"}` — formats without autoSelect
3. `_isvc_manifest`:
   - `fmt_version = req.model_format_version or _OVMS_FORMAT_VERSIONS.get(fmt, "")` —
     auto-fills version when caller doesn't provide one
   - `resolved_runtime` — for `onnx`, always injects `runtime: ovms-runtime` in the
     ISVC spec when no explicit runtime was given; other formats left to autoSelect
4. `_run_launch` result dict now includes `model_format` + `model_format_version`
   (effective values that were actually sent to the cluster)
5. `list_serving_runtimes._parse` now emits `supported_model_formats` per runtime
   (list of `{name, version, autoSelect}`) — frontend can use this for smarter hints

### index.html
1. `FW_PRESETS` map in `onLaunchModelChange` — auto-sets both `lmodelFormat` and
   `lmodelFormatVersion` when a PVC model has known framework:
   - `openvino_ir` → format=`openvino_ir`, version=`opset13`
   - `onnx` → format=`onnx`, version=`1`
   Both show green "(PAKOLLINEN — esitäytetty automaattisesti)" label
2. `lmodelFormatVersionReq` span — dynamic label text + colour; resets in clearLaunch
3. `lmodelFormatVersionNote` — clarifies OVMS needs version; shows examples
4. `onnx` option text updated to mention it's mandatory for OVMS
5. Result tile now shows `Model framework: openvino_ir v opset13` in the meta row
6. `serving_runtime || "(automaattinen)"` — cleaner display when runtime was auto-selected

## Verified
- `oc get servingruntime ovms-runtime -n demodemo` confirmed format versions
- `docker exec model-hub-c python3 -c "import launcher; ..."` → launcher imports clean
- `_isvc_manifest(req, model_format='onnx')` → `{name:'onnx', version:'1'}` + `runtime:ovms-runtime`
- `_isvc_manifest(req, model_format='openvino_ir')` → `{name:'openvino_ir', version:'opset13'}` + no runtime (autoSelect)
- Both containers restarted healthy (docker ps shows Up + healthy)
