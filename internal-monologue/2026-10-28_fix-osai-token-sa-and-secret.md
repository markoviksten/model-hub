# 2026-10-28 — Fix OSAI Token Authentication Display

## Discovery & Root Cause
In OpenShift AI (RHODS / RHOAI) Project Models view (`Data Science Projects > <project> > Models`):
- The dashboard frontend (`5181-35da6cb17847753d796d.js`) resolves the service account name as `{isvc_name}-sa` via `oH(name, namespace)`.
- It filters namespace secrets using `secret.metadata.annotations["kubernetes.io/service-account.name"] === "{isvc_name}-sa"`.
- It displays the token name from `secret.metadata.annotations["openshift.io/display-name"]` and the token secret from `secret.data.token`.

Previously, only an Opaque secret `{name}-token` with the user session token was created, lacking the required ServiceAccount, Role, RoleBinding, and `kubernetes.io/service-account-token` secret format.

## Changes Made
1. Updated `_ensure_token_secret` in [`launcher.py`](model_hub/launcher.py:348) to create:
   - ServiceAccount: `{name}-sa`
   - Role: `{name}-view-role` (`get` on `inferenceservices/{name}`)
   - RoleBinding: `{name}-view`
   - Secret: `default-name-{name}-sa` of type `kubernetes.io/service-account-token` with annotations `kubernetes.io/service-account.name: "{name}-sa"` and `openshift.io/display-name: "default-name"`, plus labels for global Model Serving view.
   - Waits for OpenShift token controller to populate `data.token` and returns the generated SA Bearer token.
2. Updated `_get_token` and `_smoke` in [`launcher.py`](model_hub/launcher.py:442) to prioritize reading the model's ServiceAccount token for external smoke tests.
3. Applied the ServiceAccount, Role, RoleBinding, and Secret to the live cluster for `navy-ships-onnx` in namespace `demodemo`.
