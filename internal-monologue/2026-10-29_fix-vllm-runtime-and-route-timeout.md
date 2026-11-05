# 2026-10-29 — Fix vLLM ServingRuntime and Route Timeout

## Discovery & Root Cause
- `qwen2-05b-instruct` failed with `No runtime found to support specified framework/version (NoSupportingRuntime)` because `vllm-cpu-x86` ServingRuntime was not deployed in namespace `demodemo`.
- RHOAI's built-in `vllm-cpu-runtime-template` is built only for ppc64le/s390x architectures, while the cluster runs x86_64 nodes.
- Initial chat completion via the OpenShift Route timed out (504 Gateway Timeout) on CPU due to default 30s route timeout.

## Changes Made
1. Created and applied `vllm-cpu-x86` ServingRuntime in namespace `demodemo`.
2. Added `ensure_vllm_runtime` in [`launcher.py`](model_hub/launcher.py:1130) to automatically deploy `vllm-cpu-x86` (CPU) or `vllm-cuda-runtime` (GPU) when deploying vLLM models or loading namespaces.
3. Updated `_ensure_route` in [`launcher.py`](model_hub/launcher.py:321) to annotate created Routes with `haproxy.router.openshift.io/timeout=300s`.
4. Created `qwen2-05b-instruct-sa`, RBAC, and `default-name-qwen2-05b-instruct-sa` secret with token authentication.
5. Re-synced and restarted WSL `model-hub` containers.
6. Verified `qwen2-05b-instruct` became `Ready: True` (2/2 Running) and responds to inference queries.
