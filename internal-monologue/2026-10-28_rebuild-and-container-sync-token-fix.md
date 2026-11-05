# 2026-10-28 — Rebuild and Container Sync for OSAI Token Fix

## Root Cause for Deployment Issue
- The updated `launcher.py` was in the workspace repository but the running `model-hub` container in WSL (Rancher Desktop) was still executing the previous container image build.
- Additionally, manual patching of ownerReferences previously referenced an old ISVC UID that got garbage-collected when the user redeployed the model.

## Action Taken
1. Copied updated [`launcher.py`](model_hub/launcher.py:348) into running WSL containers `model-hub` and `model-hub-c` and restarted them.
2. Created the `ServiceAccount`, `Role`, `RoleBinding`, and `kubernetes.io/service-account-token` secret `default-name-navy-ships-onnx-sa` linked with current ISVC UID `e2d9ab24-6ab2-41fb-bdc4-a649178db22e` in namespace `demodemo`.
3. Verified token generation and external route curl authentication.
