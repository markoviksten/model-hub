# 2025-01-28 — Fix OSAI dashboard: Real root cause via controller logs

## Discovery
Previous fix attempts based on annotations/ownerRefs didn't work. The key was reading
`odh-model-controller` logs directly:

```
InferenceService does not have label 'networking.kserve.io/visibility' annotation
set to 'exposed'. Skipping route creation
```

## Root causes (definitive)

### "Internal endpoint" + no Route
The `odh-model-controller` (not us) owns Route creation for OSAI ISVCs.
It only creates the Route when ISVC has **label** `networking.kserve.io/visibility: exposed`.
Without it, it skips Route creation → `status.url` stays cluster-internal → OSAI shows "Internal endpoint".
Our hand-crafted Route was also wrong — the controller would reconcile/delete competing resources.

### "Unknown Serving Runtime"
ISVC needs annotations:
- `opendatahub.io/template-name: kserve-ovms`
- `opendatahub.io/template-display-name: OpenVINO Model Server`
These were already fixed in the previous session (in `_isvc_manifest`).

### "Token authentication" empty
Secret needs `data.name` key (base64 token display name) in addition to `data.token`.
OSAI dashboard reads both keys to render the token row.

## Code changes (launcher.py)

### _isvc_manifest
Added `labels["networking.kserve.io/visibility"] = "exposed"` when `req.external_route`.
This single label makes `odh-model-controller` auto-create the Route AND set
`status.url` to the public address within seconds.

### _ensure_route
Replaced entire Route-creation logic with a 60s poller that waits for the
controller-created Route to appear. We must NOT create the Route ourselves.

### _ensure_token_secret
Added `data.name` (base64 of `{name}-token`) alongside `data.token`.
OSAI dashboard needs both keys to render the token row.

## Cluster patches applied

1. `oc patch inferenceservice navy-ships-onnx` → label `networking.kserve.io/visibility: exposed`
   → controller created Route within 8s
   → `status.url` now = `https://navy-ships-onnx-demodemo.*.eu-de.containers.appdomain.cloud`
2. Token secret recreated with `data.name` + `data.token`

## Verified
- Route created by controller: `navy-ships-onnx` reencrypt/Redirect ✓
- `status.url` = external public URL ✓
- Controller logs: no more "Skipping route creation" ✓
- Both containers healthy
