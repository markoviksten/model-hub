# 2026-10-29 — Fix OSAI Token Authentication Display and PVC Model Discovery

## Problem Summary
1. In OpenShift AI (RHODS / RHOAI) Project Models view (`Data Science Projects > demodemo > Models`), the "Token authentication" table section under `navy-ships-onnx` displayed blank rows (no Token name or Token secret).
2. The deployed model's secret (`default-name-navy-ships-onnx-sa`) and ServiceAccount linking were examined against OpenShift AI dashboard UI source code (`ProjectDetailsContext.tsx`, `useServingRuntimeSecrets.ts`, `auth.ts`, `TokensDescriptionItem.tsx`, `ServingRuntimeTokenDisplay.tsx`).
3. In `launcher.py`'s PVC model discovery (`list_pvcs`), running pods mounting PVCs with KServe `subPath` (e.g. `subPath: navy-ships-ONNX`) previously inspected only the mounted subdirectory, discovering `"1"` instead of `"navy-ships-ONNX"`.

## Root Cause Analysis
1. **OSAI Dashboard Token Binding:**
   - `auth.ts` calls `getTokenNames(isvc_name, namespace)` which yields `serviceAccountName = f"{isvc_name}-sa"`.
   - `ProjectDetailsContext` fetches secrets with label `opendatahub.io/dashboard=true` in the project namespace.
   - It filters secrets matching `secret.metadata.annotations["kubernetes.io/service-account.name"] === f"{isvc_name}-sa"`.
   - It displays token name from `secret.metadata.annotations["openshift.io/display-name"]` (e.g. `default-name`) and token secret from `secret.data.token` decoded with a copy button.
   - Owner references on ServiceAccount, Role, RoleBinding, and Secret ensure automatic garbage collection when the InferenceService is deleted.
2. **PVC Model Discovery:**
   - KServe predictor pods mount PVCs using `volumeMounts[].subPath = "<model-name>"`.
   - `_INSPECT_SH` previously ran on `/mnt/models/*`, matching the numeric version folder `1/`.
   - Updated `list_pvcs` to detect `subPath` mounts, run single-directory inspections with `_INSPECT_SINGLE_SH`, and query existing `InferenceService` `storageUri`s in the namespace.

## Validation
- Live token secret `default-name-navy-ships-onnx-sa` verified and tested with `curl.exe` Bearer auth against `https://navy-ships-onnx-demodemo.techdemowxai1-6ba605898783d0cf0dd66fd0aeba415b-0000.eu-de.containers.appdomain.cloud/v2/models/navy-ships-onnx` returning `HTTP 200 OK` and model metadata.
- Tested `list_pvcs('demodemo')` returning `'models': [{'pvc': 'model-storage', 'model_path': 'navy-ships-ONNX', 'framework': 'onnx'}]`.
