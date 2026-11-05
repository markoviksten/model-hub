# 2025-01-28 — Fix OSAI dashboard: Unknown Serving Runtime + Token via dashboard logs

## Discovery method
Read `rhods-dashboard` pod logs — showed exact API calls the dashboard makes:
- `GET /api/v1/namespaces/demodemo/secrets?labelSelector=opendatahub.io%2Fdashboard%3Dtrue`
- `GET /apis/serving.kserve.io/v1alpha1/namespaces/demodemo/servingruntimes`
- `GET /apis/serving.kserve.io/v1beta1/namespaces/demodemo/inferenceservices`

## Problems remaining from previous session

### "Unknown Serving Runtime"
The ServingRuntime (`ovms-runtime`) was missing annotations that OSAI uses to
resolve the display name and API protocol. The ISVC had `template-name: kserve-ovms`
but the ServingRuntime in the namespace did NOT have that annotation, so OSAI
couldn't cross-reference them.

**Fix:** `ensure_ovms_runtime` now patches these annotations onto every object
from the template before applying:
- `opendatahub.io/template-name: kserve-ovms`
- `opendatahub.io/template-display-name: OpenVINO Model Server`
- `opendatahub.io/apiProtocol: REST`

**Cluster patched:** `oc patch servingruntime ovms-runtime -n demodemo` with these annotations.

### "Token authentication" empty rows
The dashboard filters secrets by `opendatahub.io/dashboard=true`, then
client-side checks for annotation `serving.kserve.io/isvc-auth-token: "true"`.
But in newer RHOAI versions it uses `opendatahub.io/service-account-token: "true"`.

**Fix:** Added `opendatahub.io/service-account-token: "true"` annotation alongside
the existing `serving.kserve.io/isvc-auth-token: "true"`.

**Cluster patched:** `oc patch secret navy-ships-onnx-token -n demodemo` with new annotation.

## Code changes (launcher.py)

### ensure_ovms_runtime
Added `_OVMS_DASHBOARD_ANNOTATIONS` dict injected into each object's metadata
via `meta.setdefault("annotations", {}).update(...)`. Future calls to ensure-ovms-runtime
will always create a properly annotated ServingRuntime.

### _ensure_token_secret
Added `opendatahub.io/service-account-token: "true"` annotation alongside
existing annotations.

## Verified
- ServingRuntime annotations: template-name=kserve-ovms | apiProtocol=REST ✓
- Token secret annotations: both isvc-auth-token and service-account-token ✓
- Both containers healthy
- External endpoint showing in OSAI dashboard ✓ (from previous session)
