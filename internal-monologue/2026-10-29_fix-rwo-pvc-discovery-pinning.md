# 2026-10-29 — Fix Multi-Node RWO Pod Pinning for Full PVC Model Discovery

## Root Cause Identified
1. `model-storage` is a `ReadWriteOnce` (RWO) IBM Cloud block storage PVC, currently attached to node `10.231.0.31` where `navy-ships-onnx-predictor` is running.
2. The predictor pod only mounts a single subdirectory via `subPath: navy-ships-ONNX`.
3. When `_inspect_via_tmp_pod` previously created an inspect pod without `nodeName`, the default Kubernetes scheduler placed it on a different worker node (e.g. `10.231.0.33`), resulting in a `Multi-Attach error` and failing to inspect the volume root.
4. Consequently, only the single subPath model (`navy-ships-ONNX`) was visible.

## Solution Applied
1. In [`launcher.py`](model_hub/launcher.py:868):
   - Track `pvc_to_node` mapping each PVC to the node of any running pod mounting it.
   - When spawning the temporary inspect pod, pass `node_name` to pin the pod directly to the active node via `pod_spec["nodeName"] = node_name`.
   - Kubernetes allows multiple pods on the same node to mount the same RWO volume simultaneously.
2. Updated `_INSPECT_SH` and `_determine_fw` in [`launcher.py`](model_hub/launcher.py:900) to detect safetensors (`vLLM`), ONNX (`onnx`), and OpenVINO IR (`openvino_ir`).
3. Verified `list_pvcs('demodemo')` discovers all 3 models on `model-storage`:
   - `Qwen2-0.5B-Instruct` (vLLM)
   - `bge-small-en-v1.5` (onnx)
   - `navy-ships-ONNX` (onnx)
4. Synchronized updated [`launcher.py`](model_hub/launcher.py) into `model-hub` and `model-hub-c` containers and restarted them.
