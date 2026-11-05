# 2025-01-28 — Fix OSAI dashboard: Unknown Runtime, Internal endpoint, Token empty

## Screenshot analysis (navy-ships-onnx)
- Serving runtime: "Unknown Serving Runtime v2025.2.1" ← template annotation missing
- Inference endpoints: "Internal endpoint" ← Route was gone (created 50min ago for old ISVC)
- API protocol: "Not defined" ← consequence of unknown runtime
- Token authentication: empty rows ← secret lacks ownerReferences

## Root causes

### 1. Unknown Serving Runtime
OSAI dashboard resolves runtime display name from ISVC annotations:
- `opendatahub.io/template-name` → which template (kserve-ovms / vllm-runtime-2.x)
- `opendatahub.io/template-display-name` → shown label in UI
These were not in our `_isvc_manifest` output.

### 2. Internal endpoint
Route `navy-ships-onnx` was gone — only `wb` route existed.
The ISVC was re-created at 09:19 but the route step ran at 08:31 for the old ISVC.

### 3. Token authentication empty
Secret `navy-ships-onnx-token` existed with correct labels+annotations BUT lacked
`ownerReferences` pointing to the ISVC. OSAI uses ownerReferences to associate
token secrets with deployments in its UI.

## Code fixes (launcher.py)

### _isvc_manifest
Added OSAI template annotations for all deployments:
- OVMS formats (openvino_ir, onnx, tensorflow, paddle, pytorch):
  `opendatahub.io/template-name: kserve-ovms`
  `opendatahub.io/template-display-name: OpenVINO Model Server`
- vLLM/default:
  `opendatahub.io/template-name: vllm-runtime-2.x`
  `opendatahub.io/template-display-name: vLLM`

### _ensure_route
Added service port detection: checks `oc get svc` ports to choose
`http` (edge TLS) vs `https` (reencrypt). The `navy-ships-onnx-predictor`
service only exposes `https:8443` (oauth-proxy front), so stays reencrypt.

### _get_isvc_uid (new)
Returns ISVC UID needed for ownerReferences in the token secret.

### _ensure_token_secret
- Fetches ISVC UID before creating secret
- Deletes existing secret and recreates (to refresh ownerReferences + token)
- Adds `ownerReferences` block when UID is available
- Logs UID in append message for debugging

## Cluster patches applied

1. ISVC `navy-ships-onnx`: patched with template-name + template-display-name
2. Route `navy-ships-onnx`: created (reencrypt, targetPort=https)
   URL: https://navy-ships-onnx-demodemo.techdemowxai1-...eu-de.containers.appdomain.cloud
3. Secret `navy-ships-onnx-token`: deleted + recreated with ownerReferences
   ownerRef UID: 1189d17c-266d-4eed-a7a5-8dd1e802ca40

## Verified
- ISVC annotations: template-name=kserve-ovms, template-display-name=OpenVINO Model Server ✓
- Route: host=navy-ships-onnx-demodemo.*.eu-de.containers.appdomain.cloud, tls=reencrypt ✓
- Secret ownerReferences: [{kind:InferenceService, name:navy-ships-onnx, uid:1189d...}] ✓
- Both containers healthy after restart
