"""Copy a model folder to an OpenShift AI PVC with `oc` (works fully offline)."""

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import runtime as rt
import settings

log = logging.getLogger("model_hub.deployer")

DNS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,299}$")
DASHBOARD_LABEL = "opendatahub.io/dashboard"
LOADER_POD = "model-loader"
MOUNT = "/mnt/models"
TOKEN_RE = re.compile(r"sha256~[A-Za-z0-9_-]+")
MAX_JOBS_KEPT = 20
STEPS = [
    "Tarkista yhteys ja projekti",
    "Luo PVC",
    "Käynnistä apupod",
    "Kopioi malli PVC:lle",
    "Tarkista kopio ja siirrä paikalleen",
    "Poista apupod",
]

router = APIRouter(prefix="/api")
_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_session: dict = {}  # kubeconfig path, secrets (token/password), server, user


class LoginRequest(BaseModel):
    """Request body for `oc login`."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., max_length=2000, repr=False)


class DeployRequest(BaseModel):
    """Request body for starting a PVC copy."""

    model_config = ConfigDict(extra="forbid")

    model_dir: str = Field(..., max_length=128)
    namespace: str = Field(..., max_length=63)
    pvc_name: str = Field(default="model-storage", max_length=63)
    pvc_size_gi: int = Field(..., ge=1, le=2000)
    storage_class: Optional[str] = Field(default=None, max_length=63)
    loader_image: str = Field(default=settings.DEFAULT_LOADER_IMAGE, max_length=300)
    create_namespace: bool = False
    run_as_root: bool = False
    overwrite: bool = False  # replace an existing vLLM model folder on the PVC


class Step(BaseModel):
    """One step of a deploy job."""

    name: str
    state: Literal["pending", "running", "done", "error"]
    elapsed_s: Optional[float] = None


class DeployStatus(BaseModel):
    """Public status of a deploy job."""

    id: str
    state: Literal["running", "done", "error"]
    steps: list[Step]
    log: list[str]
    message: Optional[str] = None
    result: Optional[dict] = None
    elapsed_s: Optional[float] = None
    copy_done_bytes: int = 0
    copy_total_bytes: int = 0


def _status(job: dict) -> DeployStatus:
    """Build the public status (with step and total elapsed times) from a job dict."""
    steps = [Step(name=s["name"], state=s["state"], elapsed_s=settings.elapsed(s.get("started_at"), s.get("ended_at")))
             for s in job["steps"]]
    return DeployStatus(
        id=job["id"], state=job["state"], steps=steps, log=job["log"], message=job["message"],
        result=job["result"], elapsed_s=settings.elapsed(job["started_at"], job["ended_at"]),
        copy_done_bytes=job["copy_done"], copy_total_bytes=job["copy_total"],
    )


def _oc_bin() -> str:
    """Return the `oc` binary to use or raise a clear error."""
    oc = settings.tool_info()["oc"]
    if not oc:
        raise HTTPException(500, "oc-työkalua ei löydy. Aseta OC_BIN tai kopioi oc kansioon tools/.")
    return oc


def _redact(text: str) -> str:
    """Remove tokens and passwords from text before it is logged or returned."""
    for secret in _session.get("secrets", []):
        if secret:
            text = text.replace(secret, "***")
    return TOKEN_RE.sub("***", text)


def _env() -> dict:
    """Environment for `oc` calls, using the app's private kubeconfig."""
    if "kubeconfig" not in _session:
        raise HTTPException(401, "Kirjaudu ensin (oc login).")
    return settings.tool_env({"KUBECONFIG": _session["kubeconfig"]})


def _oc(args: list[str], *, input_text: Optional[str] = None, timeout: int = 120,
        env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Run `oc` without a shell and return the completed process (text mode)."""
    return subprocess.run(
        [_oc_bin(), *args], input=input_text, capture_output=True, text=True, timeout=timeout,
        env=env or _env(), check=False,
    )


def _parse_login(command: str) -> tuple[list[str], list[str]]:
    """Validate an `oc login ...` command; return (allowlisted args, secrets)."""
    try:
        tokens = shlex.split(command.strip())
    except ValueError:
        raise HTTPException(422, "Komentoa ei voi tulkita (lainausmerkit?).") from None
    if tokens[:2] != ["oc", "login"]:
        raise HTTPException(422, "Komennon pitää alkaa muodossa 'oc login ...'.")
    opts: dict[str, str] = {}
    i = 2
    while i < len(tokens):
        tok = tokens[i]
        name, eq, val = tok.partition("=")
        if tok.startswith("https://") and "server" not in opts:
            opts["server"] = tok
        elif name == "--insecure-skip-tls-verify":
            opts["insecure"] = val or "true"
        elif name in ("--token", "--server", "--username", "-u", "--password", "-p"):
            if not eq:
                i += 1
                if i >= len(tokens):
                    raise HTTPException(422, f"Parametrilta {name} puuttuu arvo.")
                val = tokens[i]
            opts[{"-u": "username", "-p": "password"}.get(name, name.lstrip("-"))] = val
        else:
            raise HTTPException(422, f"Parametri ei ole sallittu: {name}")
        i += 1
    server = opts.get("server", "")
    if not server.startswith("https://"):
        raise HTTPException(422, "Tarvitaan --server=https://...")
    if "token" not in opts and not ("username" in opts and "password" in opts):
        raise HTTPException(422, "Tarvitaan --token tai --username ja --password.")
    args = ["login", f"--server={server}"]
    for key in ("token", "username", "password"):
        if key in opts:
            args.append(f"--{key}={opts[key]}")
    if opts.get("insecure", "").lower() == "true":
        args.append("--insecure-skip-tls-verify=true")
    return args, [opts.get("token", ""), opts.get("password", "")]


def _clear_session() -> None:
    """Delete the private kubeconfig and forget the session."""
    path = _session.get("kubeconfig")
    if path:
        shutil.rmtree(Path(path).parent, ignore_errors=True)
    _session.clear()


def _local_size(path: Path) -> int:
    """Total size in bytes of files under `path`, ignoring the `.cache` folder."""
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".cache"]
        for name in files:
            if name != rt.MANIFEST:
                total += os.path.getsize(os.path.join(root, name))
    return total


def _to_bytes(qty: str) -> int:
    """Convert a Kubernetes quantity like '5Gi' or '500M' to bytes (0 if unknown)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTP]i?)?", qty.strip())
    if not m:
        return 0
    unit = m.group(2) or ""
    base = 1024 if unit.endswith("i") else 1000
    exp = "KMGTP".index(unit[0]) + 1 if unit else 0
    return int(float(m.group(1)) * base ** exp)


def _pods_using_pvc(ns: str, pvc: str) -> list[dict]:
    """Active pods (other than the loader pod) that mount the PVC, as {"name", "node"}."""
    res = _oc(["get", "pods", "-n", ns, "-o", "json"], timeout=60)
    if res.returncode != 0:
        return []  # can't tell; the pod wait step will surface real problems
    names = []
    for pod in json.loads(res.stdout).get("items", []):
        name = pod["metadata"]["name"]
        if name == LOADER_POD or pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for vol in pod["spec"].get("volumes", []):
            if vol.get("persistentVolumeClaim", {}).get("claimName") == pvc:
                names.append({"name": name, "node": pod["spec"].get("nodeName")})
    return names


def _model_path(name: str) -> Path:
    """Resolve a model folder name inside MODELS_DIR or raise 422."""
    if not settings.FOLDER_RE.match(name):
        raise HTTPException(422, "Virheellinen kansion nimi.")
    path = (settings.MODELS_DIR / name).resolve()
    if path.parent != settings.MODELS_DIR or not path.is_dir():
        raise HTTPException(422, "Mallikansiota ei löydy.")
    return path


@router.post("/login")
def login(req: LoginRequest) -> dict:
    """Run `oc login` against a private kubeconfig and store the session."""
    args, secrets = _parse_login(req.command)
    _oc_bin()
    with _lock:
        _clear_session()
        cfg_dir = tempfile.mkdtemp(prefix="model_hub_")
        env = settings.tool_env({"KUBECONFIG": str(Path(cfg_dir) / "kubeconfig")})
        _session.update(kubeconfig=env["KUBECONFIG"], secrets=secrets)
        try:
            res = _oc(args, env=env, timeout=60)
            if res.returncode != 0:
                log.error("oc login failed: %s", _redact(res.stderr))
                _clear_session()
                raise HTTPException(401, "Kirjautuminen epäonnistui (tarkista token ja palvelin).")
            who = _oc(["whoami"], env=env, timeout=30).stdout.strip()
        except subprocess.TimeoutExpired:
            _clear_session()
            raise HTTPException(504, "Kirjautuminen aikakatkaistiin.") from None
        _session.update(user=who, server=args[1].split("=", 1)[1])
        return {"user": who, "server": _session["server"]}


@router.get("/session")
def get_session() -> dict:
    """Return the current login state."""
    if "kubeconfig" not in _session:
        return {"logged_in": False}
    return {"logged_in": True, "user": _session.get("user"), "server": _session.get("server")}


@router.post("/logout")
def logout() -> dict:
    """Forget the session and delete the private kubeconfig."""
    with _lock:
        _clear_session()
    return {"logged_in": False}


@router.get("/models")
def list_models() -> dict:
    """List model folders (those that contain config.json)."""
    models = []
    if settings.MODELS_DIR.is_dir():
        for d in sorted(settings.MODELS_DIR.iterdir()):
            if d.is_dir() and settings.FOLDER_RE.match(d.name) and (
                    (d / "config.json").is_file() or (d / rt.MANIFEST).is_file()):
                models.append({"name": d.name, "size_bytes": _local_size(d),
                               "runtime": rt.read_manifest(d)["runtime"],
                               "framework": rt.framework(rt.read_manifest(d))})
    return {"dir": str(settings.MODELS_DIR), "models": models}


def _pvc_manifest(req: DeployRequest) -> dict:
    """Build the PVC manifest."""
    spec: dict = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": f"{req.pvc_size_gi}Gi"}},
    }
    if req.storage_class:
        spec["storageClassName"] = req.storage_class
    return {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": req.pvc_name, "namespace": req.namespace,
                     "labels": {DASHBOARD_LABEL: "true"}},
        "spec": spec,
    }


def _share_node(job: dict, pvc_name: str, modes: list[str], users: list[dict]) -> Optional[str]:
    """Decide how the loader pod can share a PVC that a running pod (e.g. a model server) already uses.

    RWX: no restriction. RWO: several pods may mount it only on the same node, so return that node.
    RWOP: sharing is impossible, so fail.
    """
    names = ", ".join(u["name"] for u in users)
    if "ReadWriteOncePod" in modes:
        _fail(f"PVC {pvc_name} on ReadWriteOncePod-tilassa ja pod {names} käyttää sitä. "
              "Sitä ei voi jakaa: käytä toista PVC:tä tai pysäytä pod hetkeksi.")
    if "ReadWriteMany" in modes:
        _log(job, f"PVC {pvc_name} on käytössä ({names}), mutta ReadWriteMany sallii jakamisen.")
        return None
    nodes = {u["node"] for u in users}
    if None in nodes or len(nodes) != 1:
        _fail(f"PVC {pvc_name} on käytössä ({names}), mutta podin nodea ei voitu päätellä. "
              "Odota, että pod on käynnissä, ja yritä uudelleen.")
    node = nodes.pop()
    _log(job, f"PVC {pvc_name} on käytössä ({names}) eikä deploymenttia tarvitse pysäyttää: "
              f"ReadWriteOnce sallii jakamisen samalla nodella, joten apupodi ajetaan nodella {node}.")
    return node


def _pod_manifest(req: DeployRequest, node: Optional[str] = None) -> dict:
    """Build the loader pod manifest (restricted-SCC compatible unless `run_as_root`); `node` pins it to a node."""
    if req.run_as_root:
        # Root keeps default capabilities (incl. DAC_OVERRIDE) so it can write to storage owned by others.
        sec = {"allowPrivilegeEscalation": False, "runAsUser": 0,
               "seccompProfile": {"type": "RuntimeDefault"}}
    else:
        sec = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
               "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": LOADER_POD, "namespace": req.namespace},
        "spec": {
            "restartPolicy": "Never",
            **({"nodeName": node} if node else {}),
            "containers": [{
                "name": "loader", "image": req.loader_image,
                "command": ["sleep", "infinity"],
                "volumeMounts": [{"name": "models", "mountPath": MOUNT}],
                "securityContext": sec,
            }],
            "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": req.pvc_name}}],
        },
    }


def _fail(msg: str) -> None:
    """Raise a RuntimeError with a client-safe message."""
    raise RuntimeError(msg)


def _log(job: dict, text: str) -> None:
    """Append redacted lines to the job log (kept short)."""
    for line in _redact(text).splitlines():
        if line.strip():
            job["log"].append(line.strip()[:300])
    del job["log"][:-60]


def _check(job: dict, res: subprocess.CompletedProcess, msg: str) -> None:
    """Log output and fail the step with `msg` if the command failed."""
    _log(job, res.stdout)
    _log(job, res.stderr)
    if res.returncode != 0:
        _fail(msg)


def _rsync(job: dict, req: DeployRequest, tmp: str) -> None:
    """Stream `oc rsync` output into the job log; fail on non-zero exit."""
    # Without a local rsync binary (e.g. Windows) oc falls back to tar, which needs the target dir to exist.
    res = _oc(["exec", LOADER_POD, "-n", req.namespace, "--", "mkdir", "-p", tmp])
    if res.returncode != 0:
        _log(job, res.stderr)
        diag = _oc(["exec", LOADER_POD, "-n", req.namespace, "--", "sh", "-c", f"id; ls -ldn {MOUNT}"])
        _log(job, diag.stdout)
        _fail("Ei kirjoitusoikeutta PVC:lle (storage ei sovella fsGroup-oikeuksia). "
              "Valitse 'Aja apupodi rootina' (vaatii anyuid-oikeuden, esim. cluster-admin) ja yritä uudelleen.")
    job["copy_total"] = _local_size(settings.MODELS_DIR / req.model_dir)
    stop = threading.Event()
    threading.Thread(target=_poll_copied, args=(job, req, tmp, stop), daemon=True).start()
    args = [_oc_bin(), "rsync", f"{req.model_dir}/", f"{LOADER_POD}:{tmp}/",
            "-n", req.namespace, "--exclude=.cache", f"--exclude={rt.MANIFEST}"]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env=_env(), cwd=str(settings.MODELS_DIR))
        for line in proc.stdout or []:
            _log(job, line)
        code = proc.wait()
    finally:
        stop.set()
    if code != 0:
        _fail("Kopiointi (oc rsync) epäonnistui. Katso loki.")
    job["copy_done"] = job["copy_total"]


def _poll_copied(job: dict, req: DeployRequest, target: str, stop: threading.Event) -> None:
    """Background thread: every few seconds measure how many bytes have reached the PVC."""
    while not stop.wait(4):
        try:
            res = _oc(["exec", LOADER_POD, "-n", req.namespace, "--", "du", "-sb", "--exclude=.cache", target],
                      timeout=30)
            if res.returncode == 0 and not stop.is_set():
                job["copy_done"] = min(int(res.stdout.split()[0]), job["copy_total"])
        except (subprocess.TimeoutExpired, ValueError, IndexError, HTTPException):
            continue


def _set(job: dict, idx: int, state: str) -> None:
    """Update one step's state and record its start/end time."""
    step = job["steps"][idx]
    if state == "running":
        step["started_at"] = time.time()
    elif state in ("done", "error"):
        step["ended_at"] = time.time()
    step["state"] = state


_FINALIZE = r'''set -e
tmp="$1"; final="$2"; rtm="$3"; ow="$4"
rm -f "$tmp/.model-hub.json"
if [ "$rtm" = ovms ]; then
  mkdir -p "$final"
  v=0
  for d in "$final"/*/; do
    n=$(basename "$d")
    case "$n" in ""|*[!0-9]*) ;; *) if [ "$n" -gt "$v" ]; then v=$n; fi ;; esac
  done
  v=$((v+1))
  mv "$tmp/1" "$final/$v"
  for f in config.json preprocessor_config.json; do
    if [ -f "$tmp/$f" ]; then cp -f "$tmp/$f" "$final/$f"; fi
  done
  rm -rf "$tmp"
  echo "version=$v"
else
  if [ -e "$final" ]; then
    if [ "$ow" != 1 ]; then exit 42; fi
    rm -rf "$final"
  fi
  mv "$tmp" "$final"
fi
'''


def _check_free(job: dict, ns: str, need: int) -> None:
    """Fail when the PVC has less free space than the model needs."""
    res = _oc(["exec", LOADER_POD, "-n", ns, "--", "df", "-P", "-B1", MOUNT])
    try:
        free = int(res.stdout.strip().splitlines()[-1].split()[3])
    except (IndexError, ValueError):
        _log(job, "Huom: PVC:n vapaata tilaa ei voitu lukea; tarkistus ohitetaan.")
        return
    if free < need:
        _fail(f"PVC:llä ei ole tarpeeksi tilaa: tarvitaan {need / 1073741824:.1f} GiB, "
              f"vapaana {free / 1073741824:.1f} GiB. Käytä isompaa PVC:tä.")


def _run_deploy(job: dict, req: DeployRequest) -> None:
    """Worker thread: run all deploy steps, always remove the loader pod, then set the final state."""
    ns, idx, failed = req.namespace, 0, True
    pin: Optional[str] = None  # node to run the loader pod on when the PVC is shared with a running pod
    try:
        _set(job, 0, "running")
        res = _oc(["get", "namespace", ns, "-o", "name"], timeout=60)
        if res.returncode != 0:
            if not req.create_namespace:
                _fail("Projektia ei löydy (tai ei oikeuksia). Valitse 'Luo projekti' tai tarkista nimi.")
            manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {
                "name": ns, "labels": {DASHBOARD_LABEL: "true"}}}
            _check(job, _oc(["apply", "-f", "-"], input_text=json.dumps(manifest)), "Projektin luonti epäonnistui.")
        else:
            # Existing project: make sure the OSAI dashboard shows it; not fatal if we lack permission.
            lab = _oc(["label", "namespace", ns, f"{DASHBOARD_LABEL}=true"])
            if lab.returncode != 0 and "already has a value" not in lab.stderr:
                _log(job, "Huom: projektin dashboard-labelia ei voitu lisätä (oikeudet?). Ohitetaan.")
        _set(job, 0, "done")

        idx = 1
        _set(job, 1, "running")
        existing = _oc(["get", "pvc", req.pvc_name, "-n", ns, "-o", "json"])
        if existing.returncode == 0:
            # Reuse as is: PVC specs are mostly immutable and the size can't shrink.
            pvc = json.loads(existing.stdout)
            cap = pvc.get("status", {}).get("capacity", {}).get("storage") \
                or pvc["spec"]["resources"]["requests"]["storage"]
            _log(job, f"PVC {req.pvc_name} on jo olemassa ({cap}); käytetään sitä sellaisenaan.")
            _oc(["label", "pvc", req.pvc_name, "-n", ns, f"{DASHBOARD_LABEL}=true", "--overwrite"])
            users = _pods_using_pvc(ns, req.pvc_name)
            if users:
                pin = _share_node(job, req.pvc_name, pvc["spec"].get("accessModes") or [], users)
            have, need = _to_bytes(cap), _local_size(settings.MODELS_DIR / req.model_dir)
            if have and have < need * 1.05:
                _fail(f"Olemassa oleva PVC on liian pieni ({cap}) mallille ({need / 1073741824:.1f} GiB). "
                      "Käytä toista PVC:n nimeä tai laajenna PVC.")
        else:
            _check(job, _oc(["apply", "-f", "-"], input_text=json.dumps(_pvc_manifest(req))),
                   "PVC:n luonti epäonnistui.")
        _set(job, 1, "done")

        idx = 2
        _set(job, 2, "running")
        _oc(["delete", "pod", LOADER_POD, "-n", ns, "--ignore-not-found", "--wait=true"], timeout=120)
        _check(job, _oc(["apply", "-f", "-"], input_text=json.dumps(_pod_manifest(req, pin))), "Apupodin luonti epäonnistui.")
        res = _oc(["wait", "--for=condition=Ready", f"pod/{LOADER_POD}", "-n", ns, "--timeout=600s"], timeout=660)
        if res.returncode != 0:
            _log(job, _oc(["get", "events", "-n", ns, "--field-selector", f"involvedObject.name={LOADER_POD}",
                           "-o", "custom-columns=REASON:.reason,MSG:.message", "--no-headers"]).stdout[-1500:])
            _fail("Apupod ei käynnistynyt. Tyypillisiä syitä: image ei haettavissa (air-gapped: peilattu image), "
                  "PVC ei sido tai ei kiintiötä."
              + (f" Apupodi yritettiin ajaa nodella {pin} (jaettu PVC); tarkista noden resurssit." if pin else ""))
        _set(job, 2, "done")

        idx = 3
        _set(job, 3, "running")
        local_dir = settings.MODELS_DIR / req.model_dir
        runtime = rt.read_manifest(local_dir)["runtime"]
        final, tmp = f"{MOUNT}/{req.model_dir}", f"{MOUNT}/.tmp-{job['id']}"
        _oc(["exec", LOADER_POD, "-n", ns, "--", "sh", "-c", f"rm -rf {MOUNT}/.tmp-*"])  # stale temp folders
        if runtime == "vllm" and not req.overwrite and _oc(
                ["exec", LOADER_POD, "-n", ns, "--", "test", "-e", final]).returncode == 0:
            _fail(f"Kansio {req.model_dir} on jo PVC:llä (vLLM ei tue versiokansioita). "
                  "Valitse 'Korvaa olemassa oleva malli' vahvistaaksesi ylikirjoituksen.")
        _check_free(job, ns, _local_size(local_dir))
        _rsync(job, req, tmp)
        _set(job, 3, "done")

        idx = 4
        _set(job, 4, "running")
        marker = "config.json" if runtime == "vllm" else rt.LOCAL_VERSION
        # oc's tar fallback (no local rsync) nests files under <tmp>/<model_dir>/; flatten if so.
        flat = _oc(["exec", LOADER_POD, "-n", ns, "--", "sh", "-c",
                    'cd "$1" && if [ ! -e "$3" ] && [ -e "$2/$3" ]; then '
                    'cp -a "$2"/. . && rm -rf "$2" && echo flattened; fi',
                    "_", tmp, req.model_dir, marker])
        _log(job, flat.stdout + flat.stderr)
        if flat.returncode != 0:
            _fail("Kansiorakenteen korjaus PVC:llä epäonnistui.")
        _check(job, _oc(["exec", LOADER_POD, "-n", ns, "--", "test", "-e", f"{tmp}/{marker}"]),
               "Mallin tiedostot puuttuvat PVC:ltä.")
        res = _oc(["exec", LOADER_POD, "-n", ns, "--", "du", "-sb", "--exclude=.cache", tmp])
        _check(job, res, "Koon tarkistus epäonnistui.")
        remote, local = int(res.stdout.split()[0]), _local_size(local_dir)
        if remote < local:
            _fail(f"PVC:llä on vähemmän dataa kuin paikallisesti ({remote} < {local} tavua).")
        # Only now, with every file in place, touch the deployment path (single mv per model/version).
        done = _oc(["exec", LOADER_POD, "-n", ns, "--", "sh", "-c", _FINALIZE, "_",
                    tmp, final, runtime, "1" if req.overwrite else "0"])
        _log(job, done.stderr)
        if done.returncode != 0:
            _fail("Mallin siirto paikalleen epäonnistui." if done.returncode != 42 else
                  f"Kansio {req.model_dir} on jo PVC:llä. Valitse 'Korvaa olemassa oleva malli'.")
        version = next((ln.split("=", 1)[1] for ln in done.stdout.splitlines() if ln.startswith("version=")), "")
        # Make files readable for the (random-UID) model server pod; failure is not fatal.
        _log(job, _oc(["exec", LOADER_POD, "-n", ns, "--", "chmod", "-R", "a+rX", final]).stderr)
        _set(job, 4, "done")
        job["result"] = {
            "namespace": ns, "pvc": req.pvc_name, "model_path": req.model_dir, "runtime": runtime,
            "version": version or None, "pvc_uri": f"pvc://{req.pvc_name}/{req.model_dir}",
            "embedding": bool(rt.read_manifest(local_dir).get("embedding")),
            "framework": rt.framework(rt.read_manifest(local_dir)),
            "size_bytes": local, "serving_runtime": rt.SERVING_NAMES[runtime],
        }
        failed = False
    except subprocess.TimeoutExpired:
        job["message"] = "Komento aikakatkaistiin."
        _set(job, idx, "error")
    except Exception as exc:  # noqa: BLE001 - message is client-safe or generic
        log.exception("Deploy failed")
        job["message"] = _redact(str(exc)) if isinstance(exc, RuntimeError) else "Odottamaton virhe. Katso palvelimen loki."
        _set(job, idx, "error")
    # Cleanup always runs; the final state is set only after it so the UI never sees a half-finished job.
    try:
        _set(job, 5, "running")
        if failed:  # an interrupted copy must not leave a half model behind
            _oc(["exec", LOADER_POD, "-n", ns, "--", "sh", "-c", f"rm -rf {MOUNT}/.tmp-*"], timeout=120)
        _oc(["delete", "pod", LOADER_POD, "-n", ns, "--ignore-not-found", "--wait=false"], timeout=60)
        _set(job, 5, "done")
    except Exception:  # noqa: BLE001
        log.exception("Cleanup failed")
        _set(job, 5, "error")
    job["ended_at"] = time.time()
    job["state"] = "error" if failed else "done"


@router.post("/deploys", response_model=DeployStatus, status_code=202)
def start_deploy(req: DeployRequest) -> DeployStatus:
    """Validate input and start copying a model folder to a PVC."""
    _env()
    _model_path(req.model_dir)
    if not DNS_RE.match(req.namespace) or not DNS_RE.match(req.pvc_name):
        raise HTTPException(422, "Projektin ja PVC:n nimen pitää olla DNS-yhteensopiva (a-z, 0-9, -).")
    if req.storage_class and not DNS_RE.match(req.storage_class):
        raise HTTPException(422, "Virheellinen storage class.")
    if not IMAGE_RE.match(req.loader_image):
        raise HTTPException(422, "Virheellinen image-osoite.")
    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(409, "Toinen kopiointi on jo käynnissä.")
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "state": "running", "log": [], "message": None, "result": None,
               "started_at": time.time(), "ended_at": None, "copy_done": 0, "copy_total": 0,
               "steps": [{"name": n, "state": "pending"} for n in STEPS]}
        _jobs[job_id] = job
        for old in list(_jobs)[:-MAX_JOBS_KEPT]:
            _jobs.pop(old, None)
    threading.Thread(target=_run_deploy, args=(job, req), daemon=True).start()
    return _status(job)


@router.get("/deploys/{job_id}", response_model=DeployStatus)
def get_deploy(job_id: str) -> DeployStatus:
    """Return the status of a deploy job."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Tehtävää ei löytynyt.")
    return _status(job)
