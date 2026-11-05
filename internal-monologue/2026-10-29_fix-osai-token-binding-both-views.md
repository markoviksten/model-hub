# 2026-10-29 — Fix OpenShift AI Token Authentication Display across Views

## Root Cause
1. OpenShift AI (RHODS / ODH Dashboard) displays the "Token authentication" table in two different places with differing lookup keys:
   - **Project Details > Models view** (`5181-*.js`): Looks up secrets with label `opendatahub.io/dashboard=true` where `kubernetes.io/service-account.name === "{isvc_name}-sa"` (e.g. `navy-ships-onnx-sa`).
   - **Global Model Serving view** (`5339-*.js`): In RawDeployment mode, queries secrets where `kubernetes.io/service-account.name === "{runtime_name}-sa"` (e.g. `ovms-runtime-sa`) and label `serving.kserve.io/inferenceservice === runtime_name`.
2. In the user's previous deployment attempt, `navy-ships-onnx` was deployed before the automated token Secret creation was in place, or without matching both SA bindings.

## Solution
1. In `model_hub/launcher.py`, updated `_ensure_token_secret` to create and populate secrets for both `{name}-sa` and `{runtime_name}-sa` (e.g. `navy-ships-onnx-sa` and `ovms-runtime-sa`).
2. Created `default-name-navy-ships-onnx-sa` and `default-name-ovms-runtime-sa` in namespace `demodemo` with label `opendatahub.io/dashboard=true` and valid tokens.
3. Refreshing the OpenShift AI dashboard UI (F5) will now display the token secret under Token authentication.
