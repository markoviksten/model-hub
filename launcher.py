"""OpenShift AI model deployment (InferenceService) and smoke-test endpoints.

Phase 3 of the Model Hub workflow:
  1. List existing InferenceServices and ServingRuntimes in a namespace.
  2. Build a pre-filled InferenceService manifest from advisor data.
  3. Deploy asynchronously and poll for Ready status.
  4. Run a smoke-test against the external route.
"""

import json
import logging
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import advisor as adv
import deployer  # reuse _oc, _env, _oc_bin, _redact, _log, DNS_RE
import settings

log = logging.getLogger("model_hub.launcher")

# ── Kubernetes name sanitiser ──────────────────────────────────────────────
_K8S_RE = re.compile(r"[^a-z0-9-]")

def _k8s(name: str) -> str:
    """Convert a model folder name to a valid Kubernetes resource name."""
    return _K8S_RE.sub("", name.lower().replace("_", "-").replace(".", "-")).strip("-")[:63]


# ── Job store (same pattern as deployer) ─────────────────────────────────
MAX_JOBS = 20
_jobs: dict[str, dict] = {}
_lock = threading.Lock()

LAUNCH_STEPS = [
    "Tarkista yhteys",
    "Hae käytettävissä olevat runtimet",
    "Luo InferenceService",
    "Odota Ready-tilaa",
    "Luo ulkoinen reitti",
    "Smoke-testi",
]

router = APIRouter(prefix="/api")


# ── Pydantic models ───────────────────────────────────────────────────────

class LaunchRequest(BaseModel):
    """Request body for starting an OSAI InferenceService deployment."""

    model_config = ConfigDict(extra="forbid")

    model_dir: str = Field(..., max_length=128)
    namespace: str = Field(..., max_length=63)
    deployment_name: str = Field(..., max_length=63)
    pvc_name: str = Field(..., max_length=63)
    model_path: str = Field(..., max_length=256)  # path inside PVC
    serving_runtime: str = Field(default="", max_length=128)      # empty = let OSAI auto-select
    model_format: str = Field(default="vLLM", max_length=64)      # KServe modelFormat.name
    model_format_version: str = Field(default="", max_length=32)  # KServe modelFormat.version (optional)
    # hardware
    memory_gi: int = Field(default=8, ge=1, le=2000)
    cpu: int = Field(default=4, ge=1, le=512)
    # optional explicit CPU request in millicores (limit stays `cpu`); None = auto
    cpu_request_m: Optional[int] = Field(default=None, ge=50, le=512000)
    gpu_count: int = Field(default=0, ge=0, le=16)
    gpu_memory_gi: int = Field(default=0, ge=0, le=2000)
    # vLLM args & env
    runtime_args: list[str] = Field(default_factory=list)
    runtime_env: dict[str, str] = Field(default_factory=dict)
    # options
    token_auth: bool = True
    external_route: bool = True
    run_smoke_test: bool = True


class LaunchStep(BaseModel):
    name: str
    state: Literal["pending", "running", "done", "error"]
    elapsed_s: Optional[float] = None


class LaunchStatus(BaseModel):
    id: str
    state: Literal["pending", "running", "done", "error"]
    steps: list[LaunchStep]
    log: list[str]
    message: Optional[str] = None
    result: Optional[dict] = None
    elapsed_s: Optional[float] = None


# ── Internal helpers ──────────────────────────────────────────────────────

def _status(job: dict) -> LaunchStatus:
    """Build the public status from a job dict."""
    steps = [LaunchStep(name=s["name"], state=s["state"],
                        elapsed_s=settings.elapsed(s.get("started_at"), s.get("ended_at")))
             for s in job["steps"]]
    return LaunchStatus(
        id=job["id"], state=job["state"], steps=steps, log=job["log"],
        message=job["message"], result=job["result"],
        elapsed_s=settings.elapsed(job["started_at"], job["ended_at"]),
    )


def _set(job: dict, idx: int, state: str) -> None:
    """Update step state and timestamps."""
    step = job["steps"][idx]
    if state == "running":
        step["started_at"] = time.time()
    elif state in ("done", "error"):
        step["ended_at"] = time.time()
    step["state"] = state


def _fail(msg: str) -> None:
    raise RuntimeError(msg)


def _append(job: dict, text: str) -> None:
    """Append redacted text lines to the job log (bounded)."""
    deployer._log(job, text)  # noqa: SLF001 — reuse redact+trim logic


def _oc(args: list[str], *, timeout: int = 120,
        input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run `oc` in the deployer session context."""
    return deployer._oc(args, input_text=input_text, timeout=timeout)  # noqa: SLF001


def _check(job: dict, res: subprocess.CompletedProcess, msg: str) -> None:
    """Log output; fail with `msg` on non-zero exit."""
    _append(job, res.stdout)
    _append(job, res.stderr)
    if res.returncode != 0:
        _fail(msg)


# ── OSAI manifest builder ─────────────────────────────────────────────────

# Canonical modelFormat.version required by the OVMS ServingRuntime template
# (kserve-ovms).  These are the only values OVMS accepts; mismatches produce
# "No runtime found to support specified framework".
_OVMS_FORMAT_VERSIONS: dict[str, str] = {
    "openvino_ir": "opset13",
    "onnx": "1",
}

# Formats that have autoSelect=False in the OVMS ServingRuntime — the ISVC
# must explicitly name the runtime, otherwise KServe cannot pick one.
_FORMATS_REQUIRING_EXPLICIT_RUNTIME: set[str] = {"onnx"}


def _model_format(serving_runtime: str, override: str = "") -> str:
    """Return the KServe modelFormat.name.

    Uses the explicit `override` when provided (user-selected in the UI).
    Falls back to `vLLM` for everything else — OSAI clusters that only have
    vLLM ServingRuntimes must use modelFormat=vLLM even for ONNX/OVMS models.
    OVMS-specific formats (openvino_ir, onnx) are only used when the cluster
    actually has an OVMS ServingRuntime, which the caller must confirm.
    """
    if override:
        return override
    return "vLLM"


def _isvc_manifest(req: LaunchRequest) -> dict:
    """Build an InferenceService manifest compatible with OSAI 1.x and 2.x."""
    limits: dict = {"memory": f"{req.memory_gi}Gi", "cpu": str(req.cpu)}
    cpu_req_m = req.cpu_request_m or max(1, req.cpu // 2) * 1000
    cpu_req_m = min(cpu_req_m, req.cpu * 1000)  # request must not exceed limit
    requests: dict = {"memory": f"{max(1, req.memory_gi // 2)}Gi", "cpu": f"{cpu_req_m}m"}
    if req.gpu_count:
        limits["nvidia.com/gpu"] = str(req.gpu_count)
        requests["nvidia.com/gpu"] = str(req.gpu_count)

    env_list = [{"name": k, "value": v} for k, v in req.runtime_env.items()]
    fmt = _model_format(req.serving_runtime, override=req.model_format)

    _OVMS_FORMATS = {"openvino_ir", "onnx", "tensorflow", "paddle", "pytorch"}
    if fmt in _OVMS_FORMATS:
        tpl_name, tpl_display = "kserve-ovms", "OpenVINO Model Server"
        default_runtime = _OVMS_RUNTIME_NAME
    else:
        # vLLM / default
        tpl_name, tpl_display = "vllm-runtime-2.x", "vLLM"
        default_runtime = _VLLM_CUDA_TEMPLATE_NAME if req.gpu_count > 0 else _VLLM_CPU_RUNTIME_NAME

    # OSAI dashboard resolves ServingRuntime & API protocol by matching
    # ISVC spec.predictor.model.runtime to the ServingRuntime's metadata.name.
    # Therefore runtime must always be set explicitly.
    builtin_names = {r["name"] for r in _OSAI_BUILTIN_RUNTIMES}
    explicit_runtime = req.serving_runtime and req.serving_runtime not in builtin_names
    resolved_runtime = req.serving_runtime if explicit_runtime else default_runtime

    # Auto-fill modelFormat.version when not provided by the caller:
    # OVMS runtimes require a specific version string; leaving it empty causes
    # "No runtime found to support specified framework".
    fmt_version = req.model_format_version or _OVMS_FORMAT_VERSIONS.get(fmt, "")

    fmt_obj: dict = {"name": fmt}
    if fmt_version:
        fmt_obj["version"] = fmt_version

    model_spec: dict = {
        "modelFormat": fmt_obj,
        "storageUri": f"pvc://{req.pvc_name}/{req.model_path}",
        "resources": {"limits": limits, "requests": requests},
    }
    if resolved_runtime:
        model_spec["runtime"] = resolved_runtime

    predictor: dict = {
        "minReplicas": 1,
        "model": model_spec,
    }
    if req.runtime_args:
        predictor["model"]["args"] = req.runtime_args
    if env_list:
        predictor["model"]["env"] = env_list

    annotations: dict = {
        "openshift.io/display-name": req.deployment_name,
        "security.opendatahub.io/enable-auth": "true" if req.token_auth else "false",
        "opendatahub.io/template-name": tpl_name,
        "opendatahub.io/template-display-name": tpl_display,
    }
    if req.external_route:
        annotations["serving.kserve.io/deploymentMode"] = "RawDeployment"

    labels: dict = {"opendatahub.io/dashboard": "true"}
    if req.external_route:
        # This label tells odh-model-controller to create the OpenShift Route
        # AND set status.url to the public address.  Without it the controller
        # logs "Skipping route creation" and the dashboard shows "Internal endpoint".
        labels["networking.kserve.io/visibility"] = "exposed"

    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {
            "name": req.deployment_name,
            "namespace": req.namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {"predictor": predictor},
    }


# ── Capacity preflight ────────────────────────────────────────────────────

_MIN_CPU_REQUEST_M = 250  # below this the model server is not worth starting


def _cpu_m(v: str) -> int:
    """Parse a Kubernetes CPU quantity ("2", "500m") to millicores."""
    v = str(v or "0")
    return int(float(v[:-1])) if v.endswith("m") else int(float(v) * 1000)


def _mem_b(v: str) -> int:
    """Parse a Kubernetes memory quantity ("4Gi", "512Mi", "1000000") to bytes."""
    v = str(v or "0")
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
             "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
    for suf, mul in units.items():
        if v.endswith(suf):
            return int(float(v[:-len(suf)]) * mul)
    return int(float(v))


def _free_worker_capacity() -> Optional[list[dict]]:
    """Return free CPU(m)/memory(bytes) per schedulable node, or None if unreadable.

    Needs permission to list nodes and pods cluster-wide; otherwise returns None
    so the caller can skip the preflight.
    """
    nodes_r = _oc(["get", "nodes", "-o", "json"], timeout=60)
    pods_r = _oc(["get", "pods", "-A", "-o", "json",
                  "--field-selector", "status.phase!=Succeeded,status.phase!=Failed"], timeout=90)
    if nodes_r.returncode != 0 or pods_r.returncode != 0:
        return None
    try:
        nodes = json.loads(nodes_r.stdout).get("items", [])
        pods = json.loads(pods_r.stdout).get("items", [])
    except ValueError:
        return None
    used: dict[str, list[int]] = {}
    for p in pods:
        node = p.get("spec", {}).get("nodeName")
        if not node:
            continue
        u = used.setdefault(node, [0, 0])
        for c in p.get("spec", {}).get("containers", []):
            r = (c.get("resources") or {}).get("requests") or {}
            u[0] += _cpu_m(r.get("cpu", "0"))
            u[1] += _mem_b(r.get("memory", "0"))
    out = []
    for n in nodes:
        spec, st = n.get("spec", {}), n.get("status", {})
        if spec.get("unschedulable"):
            continue
        if any(t.get("effect") in ("NoSchedule", "NoExecute") for t in spec.get("taints", [])):
            continue
        if not any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in st.get("conditions", [])):
            continue
        alloc = st.get("allocatable", {})
        u = used.get(n["metadata"]["name"], [0, 0])
        out.append({"name": n["metadata"]["name"],
                    "cpu_m": _cpu_m(alloc.get("cpu", "0")) - u[0],
                    "mem_b": _mem_b(alloc.get("memory", "0")) - u[1]})
    return out


def _preflight_capacity(job: dict, req: LaunchRequest) -> LaunchRequest:
    """Check the cluster has room for the pod; lower the CPU request if needed.

    Returns the (possibly adjusted) request. Fails early with a clear message
    when no schedulable node can fit even the minimum CPU request.
    """
    if req.gpu_count:
        return req  # GPU nodes are usually tainted; scheduling checked by pod watcher
    free = _free_worker_capacity()
    if not free:
        _append(job, "Kapasiteettitarkistus ohitettu (solmuja/podeja ei voitu listata).")
        return req
    want_m = min(req.cpu_request_m or max(1, req.cpu // 2) * 1000, req.cpu * 1000)
    mem_b = max(1, req.memory_gi // 2) * 2**30
    fits = [f for f in free if f["mem_b"] >= mem_b]
    best = max(fits, key=lambda f: f["cpu_m"], default=None)
    if best is None:
        most = max(f["mem_b"] for f in free) / 2**30
        _fail(f"Klusterissa ei ole riittävästi vapaata muistia: tarvitaan {mem_b / 2**30:.0f} Gi, "
              f"suurin vapaa {most:.1f} Gi yhdellä worker-solmulla. Pienennä muistia.")
    if best["cpu_m"] >= want_m:
        _append(job, f"Kapasiteetti OK: CPU request {want_m}m mahtuu (vapaa {best['cpu_m']}m solmulla {best['name']}).")
        return req
    new_m = best["cpu_m"] - 50  # small safety margin
    if new_m < _MIN_CPU_REQUEST_M:
        _fail(f"Klusterissa ei ole riittävästi vapaata CPU:ta: tarvitaan {want_m}m, "
              f"suurin vapaa {best['cpu_m']}m (minimi {_MIN_CPU_REQUEST_M}m). "
              f"Vapauta resursseja tai lisää worker-solmuja.")
    _append(job, f"Varoitus: CPU request {want_m}m ei mahdu (vapaa {best['cpu_m']}m). "
                 f"Lasketaan requestiksi {new_m}m (limit {req.cpu} CPU pysyy).")
    return req.model_copy(update={"cpu_request_m": new_m})


# ── Ready poller ──────────────────────────────────────────────────────────

def _pod_problem(name: str, ns: str, age_s: float) -> Optional[str]:
    """Return a fatal pod-level problem for the InferenceService, or None."""
    res = _oc(["get", "pods", "-n", ns, "-l", f"serving.kserve.io/inferenceservice={name}",
               "-o", "json"], timeout=30)
    if res.returncode != 0:
        return None
    try:
        pods = json.loads(res.stdout).get("items", [])
    except ValueError:
        return None
    for p in pods:
        st = p.get("status", {})
        for c in st.get("conditions", []):
            if (c.get("type") == "PodScheduled" and c.get("status") == "False"
                    and c.get("reason") == "Unschedulable" and age_s > 90):
                return f"Pod ei ole ajoitettavissa: {c.get('message', '')}"
        for cs in st.get("containerStatuses", []) + st.get("initContainerStatuses", []):
            w = (cs.get("state") or {}).get("waiting") or {}
            if w.get("reason") in ("ImagePullBackOff", "ErrImagePull") and age_s > 120:
                return f"Imagen veto epäonnistui ({cs.get('name')}): {w.get('message', '')}"
            if w.get("reason") == "CrashLoopBackOff" and cs.get("restartCount", 0) >= 3:
                return f"Kontti kaatuilee ({cs.get('name')}, CrashLoopBackOff)."
    return None


def _dump_events(job: dict, name: str, ns: str) -> None:
    """Log recent events of the InferenceService and its pods."""
    ev = _oc(["get", "events", "-n", ns, "--sort-by=.lastTimestamp", "-o",
              "custom-columns=OBJ:.involvedObject.name,REASON:.reason,MSG:.message",
              "--no-headers"], timeout=30)
    lines = [ln for ln in ev.stdout.splitlines() if ln.startswith(name)]
    _append(job, "\n".join(lines[-15:]))


def _wait_ready(job: dict, name: str, ns: str, timeout_s: int = 900) -> bool:
    """Poll InferenceService until Ready=True or timeout. Returns True on success.

    Raises RuntimeError early on unschedulable pod, image pull errors or crash loops.
    """
    start = time.time()
    deadline = start + timeout_s
    while time.time() < deadline:
        problem = _pod_problem(name, ns, time.time() - start)
        if problem:
            _dump_events(job, name, ns)
            _fail(problem)
        res = _oc(["get", "inferenceservice", name, "-n", ns, "-o",
                   "jsonpath={.status.conditions}"], timeout=30)
        if res.returncode == 0 and res.stdout.strip():
            try:
                conds = json.loads(res.stdout.strip())
                for c in (conds or []):
                    if c.get("type") == "Ready" and c.get("status") == "True":
                        return True
                    if c.get("type") == "Ready" and c.get("status") == "False":
                        reason = c.get("reason", "")
                        msg = c.get("message", "")
                        if reason not in ("", "Initializing", "Loading"):
                            _append(job, f"InferenceService status: {reason} – {msg}")
            except (ValueError, KeyError):
                pass
        time.sleep(8)
    return False


# ── Smoke-test ────────────────────────────────────────────────────────────

def _get_route(name: str, ns: str) -> Optional[str]:
    """Return the public HTTPS URL for the InferenceService Route.

    Checks ``oc get route <name>`` first (preferred).
    Falls back to ``status.url`` only if it is already a public address
    (does not contain ``svc.cluster.local``).
    Returns None when no external route exists yet.
    """
    route_r = _oc(["get", "route", name, "-n", ns,
                   "-o", "jsonpath={.spec.host}"], timeout=20)
    if route_r.returncode == 0 and route_r.stdout.strip():
        return "https://" + route_r.stdout.strip()

    isvc_r = _oc(["get", "inferenceservice", name, "-n", ns,
                  "-o", "jsonpath={.status.url}"], timeout=30)
    url = isvc_r.stdout.strip()
    if url.startswith("https://") and "svc.cluster.local" not in url:
        return url
    return None


def _ensure_route(job: dict, name: str, ns: str) -> Optional[str]:
    """Wait for odh-model-controller to create the OpenShift Route and return its URL.

    When the ISVC has label ``networking.kserve.io/visibility=exposed`` the
    odh-model-controller automatically creates the Route and sets ``status.url``
    to the public address.  We must NOT create the Route ourselves — the
    controller owns it and will reconcile/delete any competing resource.

    Polls up to 60 s for the Route to appear; returns the URL or None.
    """
    # Check if already there (controller may have created it before step 4 runs)
    url = _get_route(name, ns)
    if url:
        _oc(["annotate", "route", name, "-n", ns, "haproxy.router.openshift.io/timeout=300s", "--overwrite"], timeout=15)
        _append(job, f"Ulkoinen reitti valmis: {url}")
        return url

    _append(job, "Odotetaan odh-model-controllerin luomaa reittiä (enintään 60 s)…")
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(5)
        url = _get_route(name, ns)
        if url:
            _oc(["annotate", "route", name, "-n", ns, "haproxy.router.openshift.io/timeout=300s", "--overwrite"], timeout=15)
            _append(job, f"Ulkoinen reitti valmis: {url}")
            return url

    _append(job, "Reitti ei ilmestynyt 60 s kuluessa — tarkista odh-model-controller.")
    return None


def _get_isvc_uid(name: str, ns: str) -> Optional[str]:
    """Return the UID of an InferenceService, needed for ownerReferences."""
    r = _oc(["get", "inferenceservice", name, "-n", ns,
             "-o", "jsonpath={.metadata.uid}"], timeout=15)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _create_single_sa_and_token(job: dict, target_name: str, ns: str,
                                runtime_label: str, isvc_name: str,
                                isvc_uid: Optional[str] = None) -> Optional[str]:
    """Helper: create SA, Role, RoleBinding and token Secret for a specific SA name."""
    import base64  # noqa: PLC0415
    import time  # noqa: PLC0415

    sa_name = f"{target_name}-sa"
    role_name = f"{target_name}-view-role"
    rb_name = f"{target_name}-view"
    secret_name = f"default-name-{target_name}-sa"

    owner_ref = [{
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "name": isvc_name,
        "uid": isvc_uid,
        "blockOwnerDeletion": True,
        "controller": False,
    }] if isvc_uid else []

    sa_manifest = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": sa_name,
            "namespace": ns,
            "labels": {"opendatahub.io/dashboard": "true"},
            **({"ownerReferences": owner_ref} if owner_ref else {}),
        },
    }

    role_manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": role_name,
            "namespace": ns,
            "labels": {"opendatahub.io/dashboard": "true"},
            **({"ownerReferences": owner_ref} if owner_ref else {}),
        },
        "rules": [
            {
                "verbs": ["get"],
                "apiGroups": ["serving.kserve.io"],
                "resources": ["inferenceservices"],
                "resourceNames": [isvc_name],
            },
        ],
    }

    rb_manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": rb_name,
            "namespace": ns,
            "labels": {"opendatahub.io/dashboard": "true"},
            **({"ownerReferences": owner_ref} if owner_ref else {}),
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": role_name,
        },
        "subjects": [
            {"kind": "ServiceAccount", "name": sa_name},
        ],
    }

    for m in [sa_manifest, role_manifest, rb_manifest]:
        res = _oc(["apply", "-f", "-"], input_text=json.dumps(m), timeout=20)
        if res.returncode != 0:
            _append(job, f"{m['kind']} '{m['metadata']['name']}' varoitus: {deployer._redact(res.stderr[:200])}")  # noqa: SLF001

    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": ns,
            "annotations": {
                "kubernetes.io/service-account.name": sa_name,
                "openshift.io/display-name": "default-name",
                "opendatahub.io/service-account-token": "true",
                "serving.kserve.io/isvc-auth-token": "true",
            },
            "labels": {
                "opendatahub.io/dashboard": "true",
                "serving.kserve.io/inferenceservice": runtime_label or isvc_name,
            },
            **({"ownerReferences": owner_ref} if owner_ref else {}),
        },
        "type": "kubernetes.io/service-account-token",
    }

    res_sec = _oc(["get", "secret", secret_name, "-n", ns, "-o", "jsonpath={.data.token}"], timeout=10)
    if res_sec.returncode != 0 or not res_sec.stdout.strip():
        _oc(["delete", "secret", secret_name, "-n", ns, "--ignore-not-found"], timeout=15)
        _oc(["apply", "-f", "-"], input_text=json.dumps(secret_manifest), timeout=20)
    else:
        _oc(["apply", "-f", "-"], input_text=json.dumps(secret_manifest), timeout=20)

    token: Optional[str] = None
    for _ in range(16):
        t = _oc(["get", "secret", secret_name, "-n", ns,
                 "-o", "jsonpath={.data.token}"], timeout=15)
        if t.returncode == 0 and t.stdout.strip():
            try:
                token = base64.b64decode(t.stdout.strip()).decode()
                break
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.5)

    if not token:
        tok_res = _oc(["create", "token", sa_name, "-n", ns, "--duration=87600h"], timeout=15)
        if tok_res.returncode == 0 and tok_res.stdout.strip():
            token = tok_res.stdout.strip()
            b64_tok = base64.b64encode(token.encode()).decode()
            patch_payload = {"data": {"token": b64_tok}}
            _oc(["patch", "secret", secret_name, "-n", ns, "-p", json.dumps(patch_payload)], timeout=15)

    return token


def _ensure_token_secret(job: dict, name: str, ns: str,
                         runtime_name: str = "") -> Optional[str]:
    """Create OSAI-compatible ServiceAccount, Role, RoleBinding and token Secret.

    OpenShift AI (RHODS / RHOAI) Project Models view filters secrets by:
    ``secret.metadata.annotations["kubernetes.io/service-account.name"] === "{name}-sa"``.

    Global Model Serving view filters secrets by:
    ``secret.metadata.annotations["kubernetes.io/service-account.name"] === "{runtime_name}-sa"``
    and ``secret.metadata.labels["serving.kserve.io/inferenceservice"] === runtime_name``.

    Ensuring both guarantees token display across all OpenShift AI dashboard screens.

    Returns the decoded token string, or None if creation failed.
    """
    isvc_uid = _get_isvc_uid(name, ns)

    # 1. Primary SA & Token bound to ISVC deployment name (used in Project Details > Models view)
    primary_token = _create_single_sa_and_token(
        job, target_name=name, ns=ns,
        runtime_label=runtime_name or name,
        isvc_name=name, isvc_uid=isvc_uid,
    )

    # 2. Secondary SA & Token bound to ServingRuntime name (used in Global Model Serving view)
    if runtime_name and runtime_name != name:
        _create_single_sa_and_token(
            job, target_name=runtime_name, ns=ns,
            runtime_label=runtime_name,
            isvc_name=name, isvc_uid=isvc_uid,
        )

    if primary_token:
        _append(job, f"Token-secret 'default-name-{name}-sa' (SA: {name}-sa) luotu ja sidottu OSAI-dashboardiin.")
    else:
        _append(job, f"Varoitus: Tokenia ei voitu luoda ServiceAccountille '{name}-sa'.")

    return primary_token


def _get_token(ns: str, name: str = "") -> Optional[str]:
    """Get a Bearer token for smoke-testing the InferenceService.

    Tries in order:
    1. ServiceAccount token from ``default-name-{name}-sa`` secret.
    2. ServiceAccount token via ``oc create token {name}-sa``.
    3. Any service-account token secret in the namespace.
    4. Current oc session token (``oc whoami -t``).
    """
    import base64  # noqa: PLC0415

    if name:
        r = _oc(["get", "secret", f"default-name-{name}-sa", "-n", ns,
                 "-o", "jsonpath={.data.token}"], timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            try:
                return base64.b64decode(r.stdout.strip()).decode()
            except Exception:  # noqa: BLE001
                pass
        tok_res = _oc(["create", "token", f"{name}-sa", "-n", ns, "--duration=87600h"], timeout=15)
        if tok_res.returncode == 0 and tok_res.stdout.strip():
            return tok_res.stdout.strip()

    # SA token from any secret in the namespace
    sa_r = _oc(["get", "secret", "-n", ns,
                "-o", "jsonpath={.items[?(@.type==\"kubernetes.io/service-account-token\")].data.token}"],
               timeout=15)
    if sa_r.returncode == 0 and sa_r.stdout.strip():
        try:
            return base64.b64decode(sa_r.stdout.strip().split()[0]).decode()
        except Exception:  # noqa: BLE001
            pass

    # Current session token (last resort)
    res = _oc(["whoami", "-t"], timeout=15)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()

    return None


def _find_predictor_pod(name: str, ns: str) -> Optional[tuple[str, int]]:
    """Find the first Running predictor pod and its HTTP port for *name*.

    Returns ``(pod_name, port)`` or ``None`` if nothing is found.
    OVMS KServe container listens on port 8888 by default; we verify by
    reading the container port list from the pod spec.
    """
    res = _oc(["get", "pods", "-n", ns, "-l",
               f"serving.kserve.io/inferenceservice={name}",
               "-o", "json"], timeout=20)
    if res.returncode != 0:
        return None
    try:
        for pod in json.loads(res.stdout).get("items", []):
            if pod.get("status", {}).get("phase") != "Running":
                continue
            pname = pod["metadata"]["name"]
            # Find the kserve-container and its first HTTP (non-HTTPS) port
            for c in pod["spec"].get("containers", []):
                if c.get("name") != "kserve-container":
                    continue
                for p in c.get("ports", []):
                    port = p.get("containerPort", 0)
                    if port not in (8443,):  # skip oauth-proxy TLS port
                        return pname, port
    except (ValueError, KeyError):
        pass
    return None


def _is_embed(req: LaunchRequest) -> bool:
    """True when the model is an embedding model (from args, env or model name)."""
    args_str = " ".join(req.runtime_args).lower()
    if "--task=embed" in args_str or "--runner=pooling" in args_str or "task embed" in args_str:
        return True
    # Common embedding model name patterns
    name = req.model_dir.lower()
    embed_patterns = ("bge-", "e5-", "gte-", "embed", "minilm", "instructor", "sentence")
    return any(p in name for p in embed_patterns)


def _smoke(job: dict, req: LaunchRequest, route_url: Optional[str]) -> dict:
    """Run an HTTP smoke-test against the deployed model.

    When a public route URL is available, calls it directly with a Bearer token.
    When no external route exists (RawDeployment without ingress), falls back to
    ``oc exec`` into the predictor pod and runs curl from inside the cluster.
    Returns a result dict with keys: ok, url, runtime, body_snippet, chat.
    """
    k8s_name = req.deployment_name
    ns = req.namespace

    is_ovms = ("ovms" in req.serving_runtime.lower() or "openvino" in req.serving_runtime.lower()
               or req.model_format in ("openvino_ir", "onnx"))
    embed = _is_embed(req)

    # ── curl via public route (external) ────────────────────────────────
    token = _get_token(ns, k8s_name)
    ext_headers = ["-H", "Authorization: Bearer " + token] if token else []

    def _curl_ext(url: str, extra: list[str] | None = None, timeout_s: int = 30) -> tuple[str, int]:
        """curl from the model-hub container to a public URL."""
        cmd = ["curl", "-sk", "--max-time", str(timeout_s),
               "-w", "\n%{http_code}", url] + (extra or []) + ext_headers
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout_s + 10, env=settings.tool_env())
            parts = r.stdout.rsplit("\n", 1)
            body = parts[0].strip()
            code = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 0
            return body, code
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            return "", 0

    # ── curl via oc exec (cluster-internal fallback) ─────────────────────
    def _curl_exec(pod: str, cont: str, url: str,
                   extra: list[str] | None = None, timeout_s: int = 30) -> tuple[str, int]:
        """curl from inside the predictor pod (no auth needed — same pod)."""
        curl_cmd = (
            f"curl -sk --max-time {timeout_s} -w '\\n%{{http_code}}' '{url}'"
            + (" " + " ".join(f"'{a}'" for a in (extra or [])) if extra else "")
        )
        res = _oc(["exec", pod, "-n", ns, "-c", cont,
                   "--", "sh", "-c", curl_cmd], timeout=timeout_s + 15)
        if res.returncode not in (0, 22):  # 22 = curl HTTP error
            return "", 0
        parts = res.stdout.rsplit("\n", 1)
        body = parts[0].strip()
        raw_code = parts[1].strip() if len(parts) > 1 else ""
        code = int(raw_code) if raw_code.isdigit() else 0
        return body, code

    # Decide curl strategy
    use_exec = route_url is None
    base_url = route_url or ""
    exec_pod: Optional[tuple[str, int]] = None
    exec_base = ""

    if use_exec:
        exec_pod = _find_predictor_pod(k8s_name, ns)
        if exec_pod:
            exec_base = f"http://localhost:{exec_pod[1]}"
            _append(job, f"Ei ulkoista reittiä — smoke-testi podista {exec_pod[0]} portissa {exec_pod[1]}")
        else:
            _append(job, "Ei ulkoista reittiä eikä Running-podeja — smoke-testi ohitettu.")
            return {"ok": False, "url": "", "runtime": "ovms" if is_ovms else ("embed" if embed else "vllm"),
                    "body_snippet": "", "chat": None, "skipped": True}

    def _call(path: str, extra: list[str] | None = None, timeout_s: int = 30) -> tuple[str, int]:
        """Route HTTP call to external curl or oc exec curl."""
        if use_exec and exec_pod:
            return _curl_exec(exec_pod[0], "kserve-container", exec_base + path, extra, timeout_s)
        return _curl_ext(base_url + path, extra, timeout_s)

    ok = False
    url = (exec_base if use_exec else base_url)
    body = ""
    chat_result: Optional[dict] = None

    if is_ovms:
        # OVMS v2 inference protocol: GET /v2/models/<name>
        path = f"/v2/models/{k8s_name}"
        _append(job, f"Smoke-testi (OVMS v2): GET {url}{path}")
        body, code = _call(path)
        _append(job, f"HTTP {code}: {body[:400]}")
        ok = code == 200 and '"name"' in body

    elif embed:
        path = "/v1/embeddings"
        payload = json.dumps({"model": k8s_name, "input": "test"})
        _append(job, f"Smoke-testi (embedding): POST {url}{path}")
        body, code = _call(path, ["-X", "POST", "-H", "Content-Type: application/json", "-d", payload], timeout_s=60)
        _append(job, f"HTTP {code}: {body[:400]}")
        ok = code == 200 and ('"data"' in body or '"embedding"' in body or '"object"' in body) and '"error"' not in body

    else:
        # vLLM: GET /v1/models first
        path = "/v1/models"
        _append(job, f"Smoke-testi (vLLM): GET {url}{path}")
        body, code = _call(path)
        _append(job, f"HTTP {code}: {body[:400]}")
        ok = code == 200 and ('"data"' in body or '"object"' in body) and '"error"' not in body
        if ok:
            chat_path = "/v1/chat/completions"
            payload = json.dumps({
                "model": k8s_name,
                "messages": [{"role": "user", "content": "Reply with one word: working"}],
                "max_tokens": 10,
            })
            _append(job, f"Chat smoke: POST {url}{chat_path}")
            cbody, ccode = _call(chat_path, ["-X", "POST", "-H", "Content-Type: application/json", "-d", payload], timeout_s=60)
            _append(job, f"HTTP {ccode}: {cbody[:400]}")
            try:
                cd = json.loads(cbody)
                reply = cd.get("choices", [{}])[0].get("message", {}).get("content", "")
                chat_result = {"ok": bool(reply), "reply": reply[:200]}
            except (ValueError, IndexError, KeyError):
                chat_result = {"ok": False, "raw": cbody[:200]}

    return {
        "ok": ok,
        "url": url + (f"/v2/models/{k8s_name}" if is_ovms else "/v1/models"),
        "runtime": "ovms" if is_ovms else ("embed" if embed else "vllm"),
        "body_snippet": body[:300],
        "chat": chat_result,
    }


# ── Worker thread ─────────────────────────────────────────────────────────

def _run_launch(job: dict, req: LaunchRequest) -> None:
    """Worker: deploy InferenceService and optionally smoke-test it."""
    ns, idx, failed = req.namespace, 0, True
    try:
        # Step 0: connection check
        _set(job, 0, "running")
        res = _oc(["get", "namespace", ns, "-o", "name"], timeout=30)
        if res.returncode != 0:
            _fail(f"Projektia '{ns}' ei löydy tai ei oikeuksia.")
        # Without modelmesh-enabled the OSAI dashboard shows "Select model serving type"
        # and hides KServe (single-model) deployments from the Models tab.
        lab = _oc(["label", "namespace", ns, "modelmesh-enabled=false"])
        if lab.returncode != 0 and "already has a value" not in lab.stderr:
            _append(job, "Huom: modelmesh-enabled=false -labelia ei voitu asettaa (oikeudet?).")
        _set(job, 0, "done")

        # Step 1: fetch available runtimes (informational – stored in result)
        idx = 1
        _set(job, 1, "running")
        rtm_res = _oc(["get", "servingruntimes,clusterservingruntimes",
                       "-n", ns, "-o", "json"], timeout=60)
        available_runtimes: list[str] = []
        if rtm_res.returncode == 0:
            try:
                for item in json.loads(rtm_res.stdout).get("items", []):
                    available_runtimes.append(item["metadata"]["name"])
            except (ValueError, KeyError):
                pass
        _append(job, f"Löydettiin runtimet: {available_runtimes or '(ei listattuja)'}")
        _set(job, 1, "done")

        # Step 2: apply InferenceService
        idx = 2
        _set(job, 2, "running")
        _OVMS_FORMATS_SET = {"openvino_ir", "onnx", "tensorflow", "paddle", "pytorch"}
        fmt_check = _model_format(req.serving_runtime, override=req.model_format)
        if fmt_check in _OVMS_FORMATS_SET:
            try:
                ensure_ovms_runtime(ns)
            except Exception as e:  # noqa: BLE001
                _append(job, f"Varoitus: OVMS runtimen tarkistus: {e}")
        else:
            try:
                ensure_vllm_runtime(ns, req.gpu_count)
            except Exception as e:  # noqa: BLE001
                _append(job, f"Varoitus: vLLM runtimen tarkistus: {e}")

        req = _preflight_capacity(job, req)
        manifest = _isvc_manifest(req)
        _append(job, f"Luodaan InferenceService '{req.deployment_name}' namespaceen '{ns}'…")
        _check(job, _oc(["apply", "-f", "-"], input_text=json.dumps(manifest)),
               "InferenceService:n luonti epäonnistui.")
        _set(job, 2, "done")

        # Step 3: wait for Ready
        idx = 3
        _set(job, 3, "running")
        _append(job, "Odotetaan Ready-tilaa (enintään 15 min)…")
        ready = _wait_ready(job, req.deployment_name, ns)
        if not ready:
            _dump_events(job, req.deployment_name, ns)
            _fail("Deployment ei saavuttanut Ready-tilaa 15 minuutissa. Tarkista tapahtumat.")
        _set(job, 3, "done")

        # Step 4: ensure external route + token secret
        idx = 4
        _set(job, 4, "running")
        route_url: Optional[str] = None
        isvc_token: Optional[str] = None
        if req.external_route:
            route_url = _ensure_route(job, req.deployment_name, ns)
        else:
            _append(job, "Ulkoinen reitti ohitettu (checkbox ei valittu).")
            route_url = _get_route(req.deployment_name, ns)
        if req.token_auth:
            _OVMS_FORMATS_CHECK = {"openvino_ir", "onnx", "tensorflow", "paddle", "pytorch"}
            fmt_eff_check = _model_format(req.serving_runtime, override=req.model_format)
            if fmt_eff_check in _OVMS_FORMATS_CHECK:
                default_rt = _OVMS_RUNTIME_NAME
            else:
                default_rt = _VLLM_CUDA_TEMPLATE_NAME if req.gpu_count > 0 else _VLLM_CPU_RUNTIME_NAME
            builtin_names_set = {r["name"] for r in _OSAI_BUILTIN_RUNTIMES}
            explicit_runtime = req.serving_runtime and req.serving_runtime not in builtin_names_set
            resolved_rt = req.serving_runtime if explicit_runtime else default_rt

            isvc_token = _ensure_token_secret(
                job, req.deployment_name, ns,
                runtime_name=resolved_rt,
            )
        _set(job, 4, "done")

        # Step 5: smoke-test
        idx = 5
        _set(job, 5, "running")
        smoke: Optional[dict] = None
        if req.run_smoke_test:
            smoke = _smoke(job, req, route_url)
            if smoke.get("skipped"):
                _append(job, "Smoke-testi ohitettu — ei reittiä eikä Running-podeja.")
            elif not smoke["ok"]:
                _append(job, "Smoke-testi ei onnistunut (malli ei vastannut odotetusti).")
        _set(job, 5, "done")

        # Compute the effective format values that were actually sent to the cluster
        _eff_fmt = _model_format(req.serving_runtime, override=req.model_format)
        _eff_ver = req.model_format_version or _OVMS_FORMAT_VERSIONS.get(_eff_fmt, "")
        job["result"] = {
            "deployment_name": req.deployment_name,
            "namespace": ns,
            "pvc": req.pvc_name,
            "model_path": req.model_path,
            "serving_runtime": req.serving_runtime,
            "model_format": _eff_fmt,
            "model_format_version": _eff_ver,
            "route_url": route_url,
            "isvc_token": isvc_token,
            "smoke": smoke,
            "available_runtimes": available_runtimes,
        }
        failed = False

    except subprocess.TimeoutExpired:
        job["message"] = "Komento aikakatkaistiin."
        _set(job, idx, "error")
    except Exception as exc:  # noqa: BLE001
        log.exception("Launch failed")
        job["message"] = deployer._redact(str(exc)) if isinstance(exc, RuntimeError) else "Odottamaton virhe."  # noqa: SLF001
        _set(job, idx, "error")

    job["ended_at"] = time.time()
    job["state"] = "error" if failed else "done"


# ── API routes ─────────────────────────────────────────────────────────────

# Known OSAI built-in runtime definitions (display name, k8s name, type).
# OSAI manages these internally — they never appear as Kubernetes resources.
# Source: RHOAI/RHODS product documentation and UI inspection.
_OSAI_BUILTIN_RUNTIMES: list[dict] = [
    {"name": "vllm-cpu-x86-64",            "display_name": "vLLM CPU (x86_64) ServingRuntime for KServe",      "type": "vllm"},
    {"name": "vllm-cuda",                   "display_name": "vLLM CUDA GPU ServingRuntime for KServe",          "type": "vllm"},
    {"name": "vllm-gaudi",                  "display_name": "vLLM Gaudi ServingRuntime for KServe",             "type": "vllm"},
    {"name": "ovms",                        "display_name": "OpenVINO Model Server",                            "type": "ovms"},
    {"name": "tgis-grpc-runtime",           "display_name": "Text Generation Inference Server (TGIS) gRPC",     "type": "vllm"},
    {"name": "caikit-tgis-runtime",         "display_name": "Caikit TGIS ServingRuntime for KServe",           "type": "vllm"},
    {"name": "caikit-standalone-runtime",   "display_name": "Caikit Standalone ServingRuntime for KServe",     "type": "vllm"},
    {"name": "triton",                      "display_name": "NVIDIA Triton Model Server",                       "type": "triton"},
    {"name": "mlserver-0.x",                "display_name": "MLServer",                                        "type": "mlserver"},
]


@router.get("/pvcs")
def list_pvcs(namespace: str) -> dict:
    """List PVCs in a namespace and attempt to enumerate model folders on each.

    For each PVC we try two strategies to list top-level directories:
    1. ``oc exec`` into any running pod that already mounts the PVC.
    2. If no pod is available, spawn a temporary UBI loader pod (same image as the
       deployer uses), list the contents, then immediately delete the pod.

    Returns a list of PVC objects; each has a ``models`` list of
    ``{"pvc": pvc_name, "model_path": folder_name, "framework": str|None}``.
    """
    if not deployer.DNS_RE.match(namespace):
        raise HTTPException(422, "Virheellinen namespace.")
    deployer._env()  # noqa: SLF001

    # ── 1. Fetch all PVCs ────────────────────────────────────────────────
    r = deployer._oc(["get", "pvc", "-n", namespace, "-o", "json"], timeout=60)  # noqa: SLF001
    if r.returncode != 0:
        raise HTTPException(502, "PVC-listaus epäonnistui (tarkista projekti ja oikeudet).")

    pvcs: list[dict] = []
    try:
        for it in json.loads(r.stdout).get("items", []):
            meta = it.get("metadata", {})
            spec = it.get("spec", {})
            status = it.get("status", {})
            pvcs.append({
                "name": meta.get("name", ""),
                "capacity": status.get("capacity", {}).get("storage", "?"),
                "storage_class": spec.get("storageClassName", ""),
                "phase": status.get("phase", ""),
                "access_modes": spec.get("accessModes", []),
            })
    except (ValueError, KeyError):
        pass

    if not pvcs:
        return {"namespace": namespace, "items": []}

    # ── 2. For each PVC, build a map: pvc_name → [(pod_name, mountPath)] ────
    # We must use the *actual* mountPath from the pod spec so the exec shell
    # script looks in the right directory.
    pods_r = deployer._oc(["get", "pods", "-n", namespace, "-o", "json"], timeout=60)  # noqa: SLF001
    # pvc_to_mounts: pvc_name → list of (pod_name/container_name, mountPath, subPath) tuples
    pvc_to_mounts: dict[str, list[tuple[str, str, str]]] = {}
    pvc_to_node: dict[str, str] = {}
    if pods_r.returncode == 0:
        try:
            for pod in json.loads(pods_r.stdout).get("items", []):
                if pod.get("status", {}).get("phase") not in ("Running", "Succeeded"):
                    continue
                pname = pod["metadata"]["name"]
                node_name = pod.get("spec", {}).get("nodeName", "")
                # Map volume name → PVC claim name
                vol_to_claim: dict[str, str] = {}
                for vol in pod["spec"].get("volumes", []):
                    claim = vol.get("persistentVolumeClaim", {}).get("claimName", "")
                    if claim:
                        vol_to_claim[vol["name"]] = claim
                # Walk all containers to find the mountPath for each volume
                for container in pod["spec"].get("containers", []):
                    cname = container.get("name", "")
                    for vm in container.get("volumeMounts", []):
                        claim = vol_to_claim.get(vm.get("name", ""))
                        if not claim:
                            continue
                        mount_path = vm.get("mountPath", "")
                        if not mount_path:
                            continue
                        sub_path = vm.get("subPath", "")
                        entry = (f"{pname}/{cname}", mount_path, sub_path)
                        pvc_to_mounts.setdefault(claim, []).append(entry)
                        if node_name:
                            pvc_to_node[claim] = node_name
        except (ValueError, KeyError):
            pass

    # Inspect script for full PVC root mounts (iterates over direct child directories)
    _INSPECT_SH = (
        "BASE=${1:-/mnt/models}; "
        "for d in \"$BASE\"/*/; do "
        "  [ -d \"$d\" ] || continue; "
        "  name=$(basename \"$d\"); "
        "  has_ver=0; has_onnx=0; has_xml=0; has_st=0; "
        "  for sub in \"$d\"*/; do [ -d \"$sub\" ] && "
        "    case $(basename \"$sub\") in [0-9]*) has_ver=1;; esac; done; "
        "  [ -n \"$(find \"$d\" -maxdepth 3 -name '*.safetensors' 2>/dev/null)\" ] && has_st=1; "
        "  [ -n \"$(find \"$d\" -maxdepth 3 -name '*.onnx' 2>/dev/null)\" ] && has_onnx=1; "
        "  [ -n \"$(find \"$d\" -maxdepth 3 -name '*.xml' 2>/dev/null)\" ] && has_xml=1; "
        "  echo \"$name|$has_ver|$has_onnx|$has_xml|$has_st\"; "
        "done"
    )

    # Inspect script for subPath mounts (inspects single directory $BASE directly)
    _INSPECT_SINGLE_SH = (
        "BASE=${1:-/mnt/models}; "
        "has_ver=0; has_onnx=0; has_xml=0; has_st=0; "
        "for sub in \"$BASE\"/*/; do [ -d \"$sub\" ] && "
        "  case $(basename \"$sub\") in [0-9]*) has_ver=1;; esac; done; "
        "  [ -n \"$(find \"$BASE\" -maxdepth 3 -name '*.safetensors' 2>/dev/null)\" ] && has_st=1; "
        "  [ -n \"$(find \"$BASE\" -maxdepth 3 -name '*.onnx' 2>/dev/null)\" ] && has_onnx=1; "
        "  [ -n \"$(find \"$BASE\" -maxdepth 3 -name '*.xml' 2>/dev/null)\" ] && has_xml=1; "
        "echo \"$has_ver|$has_onnx|$has_xml|$has_st\""
    )

    def _determine_fw(name: str, has_ver: str, has_onnx: str, has_xml: str, has_st: str = "0") -> Optional[str]:
        nl = name.lower()
        if has_onnx == "1" or "onnx" in nl:
            return "onnx"
        if has_xml == "1" or "openvino" in nl:
            return "openvino_ir"
        if has_st == "1" or any(k in nl for k in ("qwen", "llama", "mistral", "gemma", "instruct", "deepseek", "phi")):
            return "vLLM"
        if has_ver == "1":
            return "openvino_ir"
        return "vLLM"

    def _parse_inspect(stdout: str) -> list[dict]:
        """Parse ``_INSPECT_SH`` output into {name, framework} dicts."""
        entries: list[dict] = []
        for line in stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) < 4:
                continue
            name, has_ver, has_onnx, has_xml = parts[:4]
            has_st = parts[4] if len(parts) > 4 else "0"
            name = name.strip()
            if not name or name == "lost+found":
                continue
            fw = _determine_fw(name, has_ver, has_onnx, has_xml, has_st)
            entries.append({"name": name, "framework": fw})
        return entries

    _loader_image: str = settings.DEFAULT_LOADER_IMAGE

    def _exec_inspect(pod: str, container: str, mount: str) -> list[dict]:
        """Run _INSPECT_SH inside an existing pod/container; return parsed entries."""
        cmd = ["exec", pod, "-n", namespace]
        if container:
            cmd += ["-c", container]
        cmd += ["--", "sh", "-c", _INSPECT_SH, "--", mount]
        res = deployer._oc(cmd, timeout=90)  # noqa: SLF001  — large PVCs need time
        if res.returncode != 0 or not res.stdout.strip():
            return []
        return _parse_inspect(res.stdout)

    def _exec_inspect_single(pod: str, container: str, mount: str, subpath: str) -> Optional[dict]:
        """Run _INSPECT_SINGLE_SH inside a subPath-mounted container."""
        cmd = ["exec", pod, "-n", namespace]
        if container:
            cmd += ["-c", container]
        cmd += ["--", "sh", "-c", _INSPECT_SINGLE_SH, "--", mount]
        res = deployer._oc(cmd, timeout=30)  # noqa: SLF001
        if res.returncode != 0 or not res.stdout.strip():
            return {"name": subpath, "framework": None}
        parts = res.stdout.strip().splitlines()[-1].split("|")
        if len(parts) >= 3:
            has_ver, has_onnx, has_xml = parts[:3]
            has_st = parts[3] if len(parts) > 3 else "0"
            fw = _determine_fw(subpath, has_ver, has_onnx, has_xml, has_st)
            return {"name": subpath, "framework": fw}
        return {"name": subpath, "framework": None}

    def _inspect_via_tmp_pod(pvc_name: str, node_name: Optional[str] = None) -> list[dict]:
        """Spawn a short-lived pod that mounts *pvc_name*, run inspect, then delete it.

        Pins to *node_name* if specified to allow mounting ReadWriteOnce PVCs already in use.
        """
        pod_name = _k8s(f"mh-insp-{pvc_name}")[:63]
        sec = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
               "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
        pod_spec: dict = {
            "restartPolicy": "Never",
            "containers": [{
                "name": "inspect",
                "image": _loader_image,
                "command": ["sleep", "180"],
                "volumeMounts": [{"name": "pvc", "mountPath": deployer.MOUNT}],
                "securityContext": sec,
            }],
            "volumes": [{"name": "pvc", "persistentVolumeClaim": {"claimName": pvc_name}}],
        }
        if node_name:
            pod_spec["nodeName"] = node_name

        manifest = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": namespace,
                         "labels": {"app": "mh-inspect"}},
            "spec": pod_spec,
        }
        deployer._oc(["delete", "pod", pod_name, "-n", namespace,  # noqa: SLF001
                      "--ignore-not-found", "--wait=false"], timeout=20)
        cr = deployer._oc(["apply", "-f", "-"], input_text=json.dumps(manifest), timeout=20)  # noqa: SLF001
        if cr.returncode != 0:
            log.warning("inspect pod create failed for %s: %s", pvc_name,
                        deployer._redact(cr.stderr[:200]))  # noqa: SLF001
            return []
        try:
            wr = deployer._oc(  # noqa: SLF001
                ["wait", "--for=condition=Ready", f"pod/{pod_name}",
                 "-n", namespace, "--timeout=45s"], timeout=50)
            if wr.returncode != 0:
                log.warning("inspect pod not ready for %s: %s", pvc_name, wr.stderr[:200])
                return []
            return _exec_inspect(pod_name, "inspect", deployer.MOUNT)
        finally:
            deployer._oc(["delete", "pod", pod_name, "-n", namespace,  # noqa: SLF001
                          "--ignore-not-found", "--wait=false"], timeout=20)

    # Query existing InferenceServices in the namespace to harvest storageUri model paths
    known_isvc_models: dict[str, list[dict]] = {}
    isvc_res = deployer._oc(["get", "isvc", "-n", namespace, "-o", "json"], timeout=20)  # noqa: SLF001
    if isvc_res.returncode == 0 and isvc_res.stdout.strip():
        try:
            for item in json.loads(isvc_res.stdout).get("items", []):
                storage_uri = item.get("spec", {}).get("predictor", {}).get("model", {}).get("storageUri", "")
                if storage_uri.startswith("pvc://"):
                    pvc_and_path = storage_uri[len("pvc://"):]
                    pvc_part, _, model_path = pvc_and_path.partition("/")
                    if pvc_part and model_path:
                        fmt = item.get("spec", {}).get("predictor", {}).get("model", {}).get("modelFormat", {}).get("name")
                        known_isvc_models.setdefault(pvc_part, []).append({
                            "name": model_path,
                            "framework": fmt,
                        })
        except Exception:  # noqa: BLE001
            pass

    def _list_dirs(pvc_name: str) -> list[dict]:
        """Return {name, framework} dicts for top-level model dirs on *pvc_name*.

        1. Inspects existing pod mounting PVC root directly if present.
        2. Spawns temporary inspect pod (pinned to current node) to enumerate all folders.
        3. Falls back to subPath mounts and known InferenceService specs.
        """
        mounts = pvc_to_mounts.get(pvc_name, [])

        # 1. Check existing pod mounting PVC root directly
        for pod_cname, mpath, subpath in mounts:
            pod, _, cname = pod_cname.partition("/")
            if not subpath:
                entries = _exec_inspect(pod, cname, mpath)
                if entries:
                    return entries

        # 2. Try temporary inspect pod pinned to the node hosting this PVC
        tmp_entries = _inspect_via_tmp_pod(pvc_name, node_name=pvc_to_node.get(pvc_name))
        if tmp_entries:
            return tmp_entries

        # 3. Fallback: subPath mounts + known ISVC models
        found: dict[str, dict] = {}
        for pod_cname, mpath, subpath in mounts:
            pod, _, cname = pod_cname.partition("/")
            if subpath:
                entry = _exec_inspect_single(pod, cname, mpath, subpath)
                if entry and entry["name"] not in found:
                    found[entry["name"]] = entry

        if pvc_name in known_isvc_models:
            for e in known_isvc_models[pvc_name]:
                if e["name"] not in found:
                    found[e["name"]] = e

        return list(found.values())

    # ── Inspect all PVCs in parallel ────────────────────────────────────
    import concurrent.futures  # noqa: PLC0415
    items: list[dict] = [None] * len(pvcs)  # type: ignore[list-item]

    def _inspect_one(idx_pvc: tuple[int, dict]) -> None:
        """Worker: inspect one PVC and store result at the correct index."""
        i, pvc = idx_pvc
        pname = pvc["name"]
        dir_entries = _list_dirs(pname)
        models: list[dict] = [
            {"pvc": pname, "model_path": e["name"], "framework": e["framework"]}
            for e in dir_entries
        ]
        items[i] = {**pvc, "models": models}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_inspect_one, enumerate(pvcs)))

    return {"namespace": namespace, "items": items}


# The name we give the OVMS runtime we create in the target namespace.
_OVMS_RUNTIME_NAME = "ovms-runtime"
# OSAI template name and namespace that carries the official OVMS ServingRuntime.
_OVMS_TEMPLATE_NS   = "redhat-ods-applications"
_OVMS_TEMPLATE_NAME = "kserve-ovms"


@router.post("/ensure-ovms-runtime")
def ensure_ovms_runtime(namespace: str) -> dict:
    """Ensure an OVMS ServingRuntime exists in *namespace*.

    If ``ovms-runtime`` is already present → returns ``{"status": "exists"}``.
    Otherwise instantiates the official OSAI template ``kserve-ovms`` from
    ``redhat-ods-applications`` via ``oc process`` and applies it into
    *namespace*, returning ``{"status": "created"}``.
    Raises 502 if the template is not found or ``oc apply`` fails.
    """
    if not deployer.DNS_RE.match(namespace):
        raise HTTPException(422, "Virheellinen namespace.")
    deployer._env()  # noqa: SLF001

    # Already exists — idempotent fast-path.
    chk = deployer._oc(  # noqa: SLF001
        ["get", "servingruntime", _OVMS_RUNTIME_NAME, "-n", namespace, "-o", "name"],
        timeout=20,
    )
    if chk.returncode == 0:
        return {"status": "exists", "name": _OVMS_RUNTIME_NAME, "namespace": namespace}

    # Instantiate the official OSAI template into JSON objects.
    proc = deployer._oc(  # noqa: SLF001
        ["process", f"template/{_OVMS_TEMPLATE_NAME}",
         "-n", _OVMS_TEMPLATE_NS, "--local=false", "-o", "json"],
        timeout=30,
    )
    if proc.returncode != 0:
        log.error("oc process kserve-ovms failed: %s", deployer._redact(proc.stderr))  # noqa: SLF001
        raise HTTPException(
            502,
            f"OSAI-malli '{_OVMS_TEMPLATE_NAME}' ei löydy namespacesta '{_OVMS_TEMPLATE_NS}'. "
            f"Klusterivirhe: {deployer._redact(proc.stderr[:300])}",  # noqa: SLF001
        )

    # Patch namespace and name on every object returned by the template.
    try:
        template_list = json.loads(proc.stdout)
        items = template_list.get("items") or [template_list]
    except (ValueError, KeyError) as exc:
        raise HTTPException(502, f"oc process tuotti virheellisen JSONin: {exc}") from exc

    # Annotations required by the OSAI dashboard to resolve runtime name and API protocol.
    # Without these the dashboard shows "Unknown Serving Runtime" even when the ISVC
    # correctly references the runtime by name.
    _OVMS_DASHBOARD_ANNOTATIONS = {
        "opendatahub.io/template-name": _OVMS_TEMPLATE_NAME,     # "kserve-ovms"
        "opendatahub.io/template-display-name": "OpenVINO Model Server",
        "opendatahub.io/apiProtocol": "REST",
    }

    patched: list[dict] = []
    for obj in items:
        meta = obj.setdefault("metadata", {})
        for key in ("resourceVersion", "uid", "creationTimestamp", "generation",
                    "managedFields", "selfLink", "ownerReferences"):
            meta.pop(key, None)
        meta["name"] = _OVMS_RUNTIME_NAME
        meta["namespace"] = namespace
        meta.setdefault("annotations", {}).update(_OVMS_DASHBOARD_ANNOTATIONS)
        patched.append(obj)

    # Apply all objects at once; pipe a List manifest so we make one oc call.
    manifest = {"apiVersion": "v1", "kind": "List", "items": patched}
    res = deployer._oc(  # noqa: SLF001
        ["apply", "-n", namespace, "-f", "-"],
        input_text=json.dumps(manifest),
        timeout=30,
    )
    if res.returncode != 0:
        log.error("ensure_ovms_runtime apply failed: %s", deployer._redact(res.stderr))  # noqa: SLF001
        raise HTTPException(
            502,
            f"ServingRuntimen luonti epäonnistui: {deployer._redact(res.stderr[:300])}",  # noqa: SLF001
        )

    return {"status": "created", "name": _OVMS_RUNTIME_NAME, "namespace": namespace}


_VLLM_CPU_RUNTIME_NAME = "vllm-cpu-x86"
_VLLM_CUDA_TEMPLATE_NAME = "vllm-cuda-runtime-template"


@router.post("/ensure-vllm-runtime")
def ensure_vllm_runtime(namespace: str, gpu_count: int = 0) -> dict:
    """Ensure a vLLM ServingRuntime exists in *namespace*.

    If a vLLM runtime is already present → returns ``{"status": "exists"}``.
    Otherwise instantiates the appropriate vLLM ServingRuntime:
    - For GPU (> 0): instantiates ``vllm-cuda-runtime-template``
    - For CPU (0 GPU): applies the x86 CPU ServingRuntime manifest
    """
    if not deployer.DNS_RE.match(namespace):
        raise HTTPException(422, "Virheellinen namespace.")
    deployer._env()  # noqa: SLF001

    r = deployer._oc(  # noqa: SLF001
        ["get", "servingruntime", "-n", namespace,
         "-o", "jsonpath={.items[?(@.spec.supportedModelFormats[*].name==\"vLLM\")].metadata.name}"],
        timeout=20,
    )
    if r.returncode == 0 and r.stdout.strip():
        existing = r.stdout.strip().split()
        return {"status": "exists", "name": existing[0], "namespace": namespace}

    if gpu_count > 0:
        proc = deployer._oc(  # noqa: SLF001
            ["process", f"template/{_VLLM_CUDA_TEMPLATE_NAME}",
             "-n", _OVMS_TEMPLATE_NS, "--local=false", "-o", "json"],
            timeout=30,
        )
        if proc.returncode == 0:
            try:
                template_list = json.loads(proc.stdout)
                items = template_list.get("items") or [template_list]
                for obj in items:
                    meta = obj.setdefault("metadata", {})
                    for key in ("resourceVersion", "uid", "creationTimestamp", "generation",
                                "managedFields", "selfLink", "ownerReferences"):
                        meta.pop(key, None)
                    meta["namespace"] = namespace
                    meta.setdefault("annotations", {}).update({
                        "opendatahub.io/template-name": "vllm-runtime-2.x",
                        "opendatahub.io/template-display-name": "vLLM",
                        "opendatahub.io/apiProtocol": "REST",
                    })
                manifest = {"apiVersion": "v1", "kind": "List", "items": items}
                res = deployer._oc(["apply", "-n", namespace, "-f", "-"], input_text=json.dumps(manifest), timeout=30)  # noqa: SLF001
                if res.returncode == 0:
                    return {"status": "created", "name": "vllm-cuda-runtime", "namespace": namespace}
            except Exception:  # noqa: BLE001
                pass

    static_yaml = Path(__file__).parent / "static" / "vllm-cpu-x86-servingruntime.yaml"
    if static_yaml.exists():
        manifest_text = static_yaml.read_text(encoding="utf-8")
        res = deployer._oc(["apply", "-n", namespace, "-f", "-"], input_text=manifest_text, timeout=30)  # noqa: SLF001
        if res.returncode == 0:
            return {"status": "created", "name": _VLLM_CPU_RUNTIME_NAME, "namespace": namespace}

    raise HTTPException(502, "vLLM ServingRuntimen luonti epäonnistui.")


@router.get("/serving-runtimes")
def list_serving_runtimes(namespace: str) -> dict:
    """List ServingRuntimes available for InferenceService deployments.

    Strategy — adds from each source without replacing earlier results:
    1. Namespace-scoped ServingRuntimes in the target project (oc get servingruntimes -n <ns>).
    2. ClusterServingRuntimes (cluster-scoped; may require cluster-admin).
    3. ServingRuntimes in known OSAI system namespaces (redhat-ods-applications, opendatahub).
    4. OSAI built-in runtimes hardcoded from product docs — always available as fallback.
       These are what OSAI shows in its own UI when no Kubernetes resources exist.
    """
    if not deployer.DNS_RE.match(namespace):
        raise HTTPException(422, "Virheellinen namespace.")
    deployer._env()  # noqa: SLF001

    def _parse(stdout: str) -> list[dict]:
        """Parse a JSON list of ServingRuntime items into display dicts.

        Each item includes ``supported_model_formats``: a list of
        ``{name, version, autoSelect}`` dicts from the ServingRuntime spec.
        """
        out = []
        try:
            for it in json.loads(stdout).get("items", []):
                meta = it.get("metadata", {})
                ann  = meta.get("annotations", {})
                name = meta.get("name")
                if not name:
                    continue
                raw_fmts = it.get("spec", {}).get("supportedModelFormats", [])
                fmts = [
                    {
                        "name": f.get("name", ""),
                        "version": f.get("version", ""),
                        "autoSelect": bool(f.get("autoSelect", False)),
                    }
                    for f in raw_fmts if f.get("name")
                ]
                out.append({
                    "name": name,
                    "display_name": (ann.get("openshift.io/display-name")
                                     or ann.get("opendatahub.io/template-display-name")
                                     or name),
                    "kind": it.get("kind", "ServingRuntime"),
                    "type": ann.get("opendatahub.io/kserve-runtime") or "",
                    "protocol": ann.get("opendatahub.io/apiProtocol") or "",
                    "supported_model_formats": fmts,
                })
        except (ValueError, KeyError):
            pass
        return out

    items: list[dict] = []
    seen:  set[str]   = set()
    debug: dict       = {}

    def _merge(source: list[dict]) -> None:
        """Add items not yet in `seen`."""
        for i in source:
            if i.get("name") and i["name"] not in seen:
                items.append(i)
                seen.add(i["name"])

    # Step 1 — namespace-scoped (always readable by project members).
    r1 = deployer._oc(["get", "servingruntimes", "-n", namespace, "-o", "json"], timeout=60)  # noqa: SLF001
    debug["ns_rc"] = r1.returncode
    debug["ns_err"] = r1.stderr[:200]
    if r1.returncode == 0:
        _merge(_parse(r1.stdout))

    # Step 2 — cluster-scoped (may fail without elevated RBAC).
    r2 = deployer._oc(["get", "clusterservingruntimes", "-o", "json"], timeout=60)  # noqa: SLF001
    debug["cluster_rc"] = r2.returncode
    if r2.returncode == 0:
        _merge(_parse(r2.stdout))

    # Step 3 — OSAI system namespaces.
    for sys_ns in ("redhat-ods-applications", "opendatahub"):
        r3 = deployer._oc(["get", "servingruntimes", "-n", sys_ns, "-o", "json"], timeout=30)  # noqa: SLF001
        debug[f"sysns_{sys_ns}_rc"] = r3.returncode
        if r3.returncode == 0:
            _merge(_parse(r3.stdout))

    # Step 4 — OSAI built-in runtimes (always present; OSAI manages them internally).
    # These match what OSAI UI shows in the "Serving runtime" dropdown.
    _merge([{"name": r["name"], "display_name": r["display_name"],
             "kind": "BuiltIn", "type": r["type"], "protocol": "",
             "supported_model_formats": []}
            for r in _OSAI_BUILTIN_RUNTIMES])

    return {"namespace": namespace, "items": items, "debug": debug}


@router.get("/hardware-profiles")
def list_hardware_profiles(namespace: str) -> dict:
    """List HardwareProfiles (OSAI 2.x) and AcceleratorProfiles (OSAI 1.x)."""
    if not deployer.DNS_RE.match(namespace):
        raise HTTPException(422, "Virheellinen namespace.")
    deployer._env()  # noqa: SLF001
    items: list[dict] = []

    # OSAI 2.x: HardwareProfile (cluster-scoped)
    for kind in ("hardwareprofiles", "acceleratorprofiles"):
        r = deployer._oc(["get", kind, "-n", namespace, "-o", "json"], timeout=30)  # noqa: SLF001
        if r.returncode != 0:
            r = deployer._oc(["get", kind, "-o", "json"], timeout=30)  # noqa: SLF001 cluster-scoped
        if r.returncode == 0:
            try:
                for it in json.loads(r.stdout).get("items", []):
                    meta = it.get("metadata", {})
                    spec = it.get("spec", {})
                    items.append({
                        "name": meta.get("name"),
                        "display_name": spec.get("displayName") or meta.get("name"),
                        "kind": it.get("kind"),
                    })
            except (ValueError, KeyError):
                pass

    return {"namespace": namespace, "items": items}


@router.get("/ns-isvcs")
def list_ns_isvcs(namespace: str) -> dict:
    """List InferenceServices with name, ready-status, runtime, and URL."""
    if not deployer.DNS_RE.match(namespace):
        raise HTTPException(422, "Virheellinen namespace.")
    deployer._env()  # noqa: SLF001
    res = deployer._oc(["get", "inferenceservice", "-n", namespace, "-o", "json"], timeout=60)  # noqa: SLF001
    if res.returncode != 0:
        return {"namespace": namespace, "items": []}
    items = []
    try:
        for it in json.loads(res.stdout).get("items", []):
            meta = it.get("metadata", {})
            conds = it.get("status", {}).get("conditions", [])
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds)
            url = it.get("status", {}).get("url", "")
            runtime = it.get("spec", {}).get("predictor", {}).get("model", {}).get("runtime", "")
            items.append({
                "name": meta.get("name"),
                "display_name": meta.get("annotations", {}).get("openshift.io/display-name") or meta.get("name"),
                "ready": ready,
                "url": url,
                "runtime": runtime,
            })
    except (ValueError, KeyError):
        pass
    return {"namespace": namespace, "items": items}


@router.get("/isvc-diag")
def isvc_diag(namespace: str, name: str) -> dict:
    """Return full InferenceService status + pod events for diagnostics."""
    if not deployer.DNS_RE.match(namespace) or not deployer.DNS_RE.match(name):
        raise HTTPException(422, "Virheellinen namespace tai nimi.")
    deployer._env()  # noqa: SLF001

    # Full InferenceService JSON
    isvc = deployer._oc(["get", "inferenceservice", name, "-n", namespace,  # noqa: SLF001
                          "-o", "json"], timeout=30)
    # Kubernetes events for the ISVC
    evts = deployer._oc(["get", "events", "-n", namespace,  # noqa: SLF001
                          "--field-selector", f"involvedObject.name={name}",
                          "--sort-by=.lastTimestamp",
                          "-o", "custom-columns=TIME:.lastTimestamp,REASON:.reason,MSG:.message",
                          "--no-headers"], timeout=30)
    # Pod events (predictor pod name starts with <name>-predictor)
    pods = deployer._oc(["get", "pods", "-n", namespace,  # noqa: SLF001
                          "-l", f"serving.kserve.io/inferenceservice={name}",
                          "-o", "wide", "--no-headers"], timeout=30)
    pod_evts = ""
    if pods.returncode == 0 and pods.stdout.strip():
        pod_name = pods.stdout.strip().split()[0]
        pe = deployer._oc(["describe", "pod", pod_name, "-n", namespace], timeout=30)  # noqa: SLF001
        pod_evts = pe.stdout[-4000:] if pe.returncode == 0 else pe.stderr

    # Parse conditions from ISVC for quick summary
    conditions: list[dict] = []
    try:
        isvc_json = json.loads(isvc.stdout)
        conditions = isvc_json.get("status", {}).get("conditions", [])
    except (ValueError, KeyError):
        pass

    return {
        "isvc_stdout": isvc.stdout[-6000:],
        "isvc_stderr": isvc.stderr[-1000:],
        "conditions": conditions,
        "events": evts.stdout[-3000:],
        "pods": pods.stdout,
        "pod_describe": pod_evts,
    }


@router.get("/launches/{job_id}", response_model=LaunchStatus)
def get_launch(job_id: str) -> LaunchStatus:
    """Return the status of a launch job."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Tehtävää ei löydy.")
    return _status(job)


def _validate_launch(req: LaunchRequest) -> None:
    """Shared validation for single and dual launch."""
    deployer._env()  # noqa: SLF001
    if not deployer.DNS_RE.match(req.namespace):
        raise HTTPException(422, "Virheellinen namespace.")
    if not deployer.DNS_RE.match(req.deployment_name):
        raise HTTPException(422, "Deployment-nimessä saa olla vain pieniä kirjaimia, numeroita ja viivoja.")
    if not deployer.DNS_RE.match(req.pvc_name):
        raise HTTPException(422, "Virheellinen PVC-nimi.")


def _make_job(job_id: str) -> dict:
    """Create a fresh job dict."""
    return {
        "id": job_id, "state": "running", "log": [], "message": None, "result": None,
        "started_at": time.time(), "ended_at": None,
        "steps": [{"name": n, "state": "pending"} for n in LAUNCH_STEPS],
    }


# Max wall-clock seconds a launch job is allowed to be "running" before it is
# considered stale (15-min Ready-wait + 2-min overhead).
_MAX_JOB_AGE_S = 17 * 60


@router.post("/launches/reset")
def reset_launches() -> dict:
    """Force-cancel all stuck 'running' launch jobs so a new deploy can start."""
    now = time.time()
    cleared = 0
    with _lock:
        for j in _jobs.values():
            if j["state"] == "running":
                j["state"] = "error"
                j["ended_at"] = now
                j["message"] = "Manuaalisesti keskeytetty (reset)."
                for step in j["steps"]:
                    if step["state"] == "running":
                        step["state"] = "error"
                cleared += 1
    return {"cleared": cleared}


@router.post("/launches", response_model=LaunchStatus, status_code=202)
def start_launch(req: LaunchRequest) -> LaunchStatus:
    """Validate and start an async InferenceService deploy job."""
    _validate_launch(req)
    now = time.time()
    with _lock:
        # Mark stale "running" jobs as errored so they don't block new deploys.
        for j in _jobs.values():
            if j["state"] == "running" and (now - j.get("started_at", now)) > _MAX_JOB_AGE_S:
                j["state"] = "error"
                j["ended_at"] = now
                j["message"] = "Tehtävä keskeytettiin automaattisesti (aikakatkaisu)."
                for step in j["steps"]:
                    if step["state"] == "running":
                        step["state"] = "error"
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(409, "Toinen deploy on jo käynnissä.")
        job_id = uuid.uuid4().hex[:12]
        job = _make_job(job_id)
        _jobs[job_id] = job
        for old in list(_jobs)[:-MAX_JOBS]:
            _jobs.pop(old, None)
    threading.Thread(target=_run_launch, args=(job, req), daemon=True).start()
    return _status(job)


class DualLaunchStatus(BaseModel):
    """Status of a dual (CPU + GPU) deploy."""

    cpu_job_id: str
    gpu_job_id: str
    cpu: LaunchStatus
    gpu: LaunchStatus


class DualLaunchRequest(BaseModel):
    """Request body for CPU+GPU dual deploy."""

    model_config = ConfigDict(extra="forbid")

    # shared fields
    model_dir: str = Field(..., max_length=128)
    namespace: str = Field(..., max_length=63)
    pvc_name: str = Field(..., max_length=63)
    model_path: str = Field(..., max_length=256)
    serving_runtime: str = Field(..., max_length=128)
    token_auth: bool = True
    external_route: bool = True
    run_smoke_test: bool = True
    # base name — suffixes -cpu / -gpu are appended automatically
    base_name: str = Field(..., max_length=60)
    # CPU params
    cpu_memory_gi: int = Field(default=8, ge=1, le=2000)
    cpu_cores: int = Field(default=4, ge=1, le=512)
    cpu_args: list[str] = Field(default_factory=list)
    cpu_env: dict[str, str] = Field(default_factory=dict)
    # GPU params
    gpu_memory_gi: int = Field(default=24, ge=1, le=2000)
    gpu_count: int = Field(default=1, ge=1, le=16)
    gpu_args: list[str] = Field(default_factory=list)
    gpu_env: dict[str, str] = Field(default_factory=dict)


def _dual_worker(cpu_job: dict, gpu_job: dict,
                 cpu_req: LaunchRequest, gpu_req: LaunchRequest) -> None:
    """Run CPU deploy then GPU deploy sequentially in a single daemon thread."""
    _run_launch(cpu_job, cpu_req)
    # Transition GPU job from pending → running before starting it.
    gpu_job["state"] = "running"
    _run_launch(gpu_job, gpu_req)


@router.post("/launches/dual", status_code=202)
def start_dual_launch(req: DualLaunchRequest) -> DualLaunchStatus:
    """Start CPU and GPU InferenceService deploys sequentially."""
    if not deployer.DNS_RE.match(req.namespace):
        raise HTTPException(422, "Virheellinen namespace.")
    if not deployer.DNS_RE.match(req.base_name):
        raise HTTPException(422, "base_name: vain pieniä kirjaimia, numeroita ja viivoja.")
    if not deployer.DNS_RE.match(req.pvc_name):
        raise HTTPException(422, "Virheellinen PVC-nimi.")
    deployer._env()  # noqa: SLF001

    cpu_name = (req.base_name + "-cpu")[:63]
    gpu_name = (req.base_name + "-gpu")[:63]

    cpu_req = LaunchRequest(
        model_dir=req.model_dir, namespace=req.namespace,
        deployment_name=cpu_name, pvc_name=req.pvc_name,
        model_path=req.model_path, serving_runtime=req.serving_runtime,
        memory_gi=req.cpu_memory_gi, cpu=req.cpu_cores, gpu_count=0,
        runtime_args=req.cpu_args, runtime_env=req.cpu_env,
        token_auth=req.token_auth, external_route=req.external_route,
        run_smoke_test=req.run_smoke_test,
    )
    gpu_req = LaunchRequest(
        model_dir=req.model_dir, namespace=req.namespace,
        deployment_name=gpu_name, pvc_name=req.pvc_name,
        model_path=req.model_path, serving_runtime=req.serving_runtime,
        memory_gi=req.gpu_memory_gi, cpu=req.cpu_cores, gpu_count=req.gpu_count,
        gpu_memory_gi=req.gpu_memory_gi,
        runtime_args=req.gpu_args, runtime_env=req.gpu_env,
        token_auth=req.token_auth, external_route=req.external_route,
        run_smoke_test=req.run_smoke_test,
    )

    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(409, "Toinen deploy on jo käynnissä.")
        cpu_id = uuid.uuid4().hex[:12]
        gpu_id = uuid.uuid4().hex[:12]
        cpu_job = _make_job(cpu_id)
        gpu_job = _make_job(gpu_id)
        # GPU job starts as pending until CPU finishes
        gpu_job["state"] = "pending"
        for step in gpu_job["steps"]:
            step["state"] = "pending"
        _jobs[cpu_id] = cpu_job
        _jobs[gpu_id] = gpu_job
        for old in list(_jobs)[:-MAX_JOBS]:
            _jobs.pop(old, None)

    threading.Thread(target=_dual_worker,
                     args=(cpu_job, gpu_job, cpu_req, gpu_req), daemon=True).start()
    return DualLaunchStatus(
        cpu_job_id=cpu_id, gpu_job_id=gpu_id,
        cpu=_status(cpu_job), gpu=_status(gpu_job),
    )


@router.get("/launches/dual/{cpu_id}/{gpu_id}")
def get_dual_launch(cpu_id: str, gpu_id: str) -> DualLaunchStatus:
    """Poll status of a dual deploy."""
    cpu_job = _jobs.get(cpu_id)
    gpu_job = _jobs.get(gpu_id)
    if cpu_job is None or gpu_job is None:
        raise HTTPException(404, "Tehtävää ei löydy.")
    # Promote gpu_job from pending to running when cpu is done
    if cpu_job["state"] in ("done", "error") and gpu_job["state"] == "pending":
        gpu_job["state"] = "running"
    return DualLaunchStatus(
        cpu_job_id=cpu_id, gpu_job_id=gpu_id,
        cpu=_status(cpu_job), gpu=_status(gpu_job),
    )
