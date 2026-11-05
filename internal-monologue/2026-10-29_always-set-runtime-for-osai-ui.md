# 2026-10-29 — Always Set Explicit Runtime on InferenceService for OSAI UI

## Discovery & Root Cause
OpenShift AI dashboard resolves the **Serving runtime** name and **API protocol** columns by looking up the ServingRuntime resource whose `metadata.name` matches `inferenceservice.spec.predictor.model.runtime`.
Previously, `runtime` was left empty when no explicit runtime was selected, relying on KServe's `autoSelect: true`. While KServe controller resolved it internally, the OSAI frontend could not look up the runtime object, displaying `Serving runtime: Unknown` and `API protocol: Not defined`.

## Changes Made
1. Updated `_isvc_manifest` in [`launcher.py`](model_hub/launcher.py:183) so `spec.predictor.model.runtime` is always populated explicitly:
   - OVMS models default to `ovms-runtime`.
   - vLLM models default to `vllm-cpu-x86` (CPU) or `vllm-cuda-runtime` (GPU).
2. Patched live `qwen2-05b-instruct` on the cluster with `spec.predictor.model.runtime: vllm-cpu-x86`.
3. Updated and restarted WSL container deployments.
