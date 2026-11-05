# 2025-01-28 — Fix OSAI token authentication — filterTokens uses runtime name

## Discovery
Read the actual RHODS dashboard compiled JavaScript from the running pod.
Found the token filter logic in `8015-96ff3c3438f01c1b6beb.js`:

```javascript
const o = l((0,S.br)(e) ? e.spec.predictor.model?.runtime : e.metadata.name)
```

`S.br(e)` returns true for KServe RawDeployment ISVCs (checks deploymentMode).
`l` is `filterTokens`. So for RawDeployment:

`filterTokens("ovms-runtime")` — filtered by runtime name, NOT ISVC name.

`filterTokens` filters the secrets list by:
`secret.metadata.labels["serving.kserve.io/inferenceservice"] === "ovms-runtime"`

Our secret had `serving.kserve.io/inferenceservice: "navy-ships-onnx"` → never matched.

## Fix

### _ensure_token_secret (launcher.py)
- Added `runtime_name: str = ""` parameter
- Label value changed to `runtime_name or name` 
  - When runtime name is provided (ovms-runtime) → use that
  - Fallback to ISVC name (for ModelMesh or unknown)

### _run_launch (launcher.py)  
- Passes `runtime_name=req.serving_runtime` to `_ensure_token_secret`

## Cluster patched immediately
- Deleted old `navy-ships-onnx-token` secret (wrong label value `navy-ships-onnx`)
- Created new secret with label `serving.kserve.io/inferenceservice: "ovms-runtime"`
- Verified: `oc get secret navy-ships-onnx-token ... -o jsonpath=... → ovms-runtime` ✓

## Summary of all OSAI dashboard fixes (this session)
1. External endpoint: label `networking.kserve.io/visibility: exposed` → controller creates Route ✓
2. Serving Runtime name: annotations `template-name/template-display-name` on ISVC + ServingRuntime ✓
3. API protocol REST: annotation `opendatahub.io/apiProtocol: REST` on ServingRuntime ✓  
4. Token authentication: label value must be runtime name (`ovms-runtime`), not ISVC name ✓
