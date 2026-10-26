"""Hugging Face download feature (off when huggingface_hub is missing or MODEL_HUB_OFFLINE=1)."""

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

import runtime as rt
import settings

try:
    from huggingface_hub import HfApi, snapshot_download
    from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError

    HF_INSTALLED = True
except ImportError:  # air-gapped bundle ships without huggingface_hub
    HF_INSTALLED = False

    class GatedRepoError(Exception):
        """Placeholder when huggingface_hub is not installed."""

    class RepositoryNotFoundError(Exception):
        """Placeholder when huggingface_hub is not installed."""

log = logging.getLogger("model_hub.downloader")

ENABLED = HF_INSTALLED and not settings.OFFLINE
REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
MAX_JOBS_KEPT = 50
SEARCH_PAGE_SIZE = 10
SEARCH_MAX_PAGES = 10

router = APIRouter(prefix="/api")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class DownloadRequest(BaseModel):
    """Request body for starting a download."""

    model_config = ConfigDict(extra="forbid")

    repo_id: str = Field(..., max_length=200)
    token: Optional[str] = Field(default=None, max_length=200, repr=False)  # falls back to env HF_TOKEN
    runtime: Literal["auto", "vllm", "ovms"] = "auto"
    variant: str = Field(default=rt.DEFAULT_VARIANT, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    overwrite: bool = False


class JobStatus(BaseModel):
    """Public status of a download job."""

    id: str
    repo_id: str
    state: Literal["running", "done", "error", "cancelled"]
    downloaded_bytes: int
    total_bytes: int
    path: Optional[str] = None
    message: Optional[str] = None
    elapsed_s: Optional[float] = None
    runtime: Optional[str] = None
    reason: Optional[str] = None
    files: list[str] = []


def _dir_size(path: Path) -> int:
    """Return total size in bytes of all files under `path` (incl. partial files)."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _safe_error(exc: Exception) -> str:
    """Map an exception to a message that is safe to show to the client."""
    if isinstance(exc, GatedRepoError):
        return "Malli on gated: hyväksy lisenssi Hugging Facessa ja anna token."
    if isinstance(exc, RepositoryNotFoundError):
        return "Mallia ei löytynyt (tai se vaatii tokenin)."
    return "Lataus epäonnistui. Katso palvelimen loki."


_CHILD_CODE = (
    "import sys, json\n"
    "from huggingface_hub import snapshot_download\n"
    "from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError\n"
    "try:\n"
    "    snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2], allow_patterns=json.load(sys.stdin))\n"
    "except GatedRepoError:\n"
    "    sys.exit(3)\n"
    "except RepositoryNotFoundError:\n"
    "    sys.exit(4)\n"
)
_EXIT_MESSAGES = {
    3: "Malli on gated: hyväksy lisenssi Hugging Facessa ja anna token.",
    4: "Mallia ei löytynyt (tai se vaatii tokenin).",
}


def _finish(job: dict, req: DownloadRequest, plan: rt.Plan, tmp: Path, final: Path) -> None:
    """Move the downloaded files into the runtime layout and swap it in place of the old folder."""
    missing = [f for f in plan.files if not (tmp / f).is_file()]
    if missing:
        raise RuntimeError("Lataus jäi vajaaksi (tiedostoja puuttuu).")
    out = tmp.with_name(tmp.name + "-out")
    shutil.rmtree(out, ignore_errors=True)
    rt.build_layout(tmp, out, plan)
    rt.write_manifest(out, {"runtime": plan.runtime, "repo_id": req.repo_id, "format": plan.fmt,
                            "variant": plan.variant})
    if final.exists():  # overwrite was confirmed in start_download
        shutil.rmtree(final)
    out.rename(final)
    shutil.rmtree(tmp, ignore_errors=True)


def _run_job(job: dict, req: DownloadRequest, plan: rt.Plan, tmp: Path, final: Path) -> None:
    """Worker: download only the planned files into a temp folder in a child process (cancellable),
    then build the final layout. A failed or cancelled download leaves nothing in the final path."""
    env = {**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
    if req.token:
        env["HF_TOKEN"] = req.token  # via env, never argv
    try:
        proc = subprocess.Popen([sys.executable, "-c", _CHILD_CODE, req.repo_id, str(tmp)],
                                stdin=subprocess.PIPE, env=env)
        job["proc"] = proc
        if job["cancel"]:  # cancelled before the process handle was stored
            proc.terminate()
        proc.communicate(json.dumps(plan.files).encode())
        rc = proc.returncode
        if job["cancel"]:
            job["message"] = "Lataus keskeytetty, keskeneräiset tiedostot poistettu."
            job["state"] = "cancelled"
        elif rc == 0:
            _finish(job, req, plan, tmp, final)
            job["path"] = str(final)
            job["message"] = None
            job["state"] = "done"
        else:
            log.error("Download of %s exited with code %s", req.repo_id, rc)
            job["message"] = _EXIT_MESSAGES.get(rc, "Lataus epäonnistui. Katso palvelimen loki.")
            job["state"] = "error"
    except Exception:  # noqa: BLE001 - mapped to a safe message
        log.exception("Download failed for %s", req.repo_id)
        job["message"] = "Lataus epäonnistui. Katso palvelimen loki."
        job["state"] = "error"
    finally:
        job["proc"] = None
        job["ended_at"] = time.time()
        if job["state"] != "done":
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(tmp.with_name(tmp.name + "-out"), ignore_errors=True)


def _to_status(job: dict) -> JobStatus:
    """Build a JobStatus, refreshing downloaded bytes from disk while running."""
    if job["state"] == "running":
        job["downloaded_bytes"] = _dir_size(Path(job["target"]))
    elif job["state"] == "done":
        job["downloaded_bytes"] = max(job["downloaded_bytes"], job["total_bytes"])
    public = {k: v for k, v in job.items() if k not in ("target", "started_at", "ended_at", "proc", "cancel")}
    return JobStatus(**public, elapsed_s=settings.elapsed(job["started_at"], job["ended_at"]))


class SearchHit(BaseModel):
    """One Hugging Face search result."""

    repo_id: str
    downloads: int = 0
    likes: int = 0
    task: Optional[str] = None
    created: Optional[str] = None
    modified: Optional[str] = None
    size_bytes: Optional[int] = None


class SearchPage(BaseModel):
    """One page of search results."""

    items: list[SearchHit]
    page: int
    page_size: int
    has_next: bool


def _repo_size(repo_id: str) -> Optional[int]:
    """Return the total size of all files in a repo (what a download fetches), or None on failure."""
    try:
        info = HfApi().model_info(repo_id, files_metadata=True)
    except Exception:  # noqa: BLE001 - size is optional
        return None
    return sum((s.size or 0) for s in (info.siblings or []))


def _date(value) -> Optional[str]:
    """Return a datetime as YYYY-MM-DD (None if missing)."""
    return value.date().isoformat() if value else None


@router.get("/search", response_model=SearchPage)
async def search_models(
    q: str = Query(..., min_length=2, max_length=100),
    sort: Literal["downloads", "last_modified", "created_at", "likes"] = "downloads",
    page: int = Query(0, ge=0, lt=SEARCH_MAX_PAGES),
) -> SearchPage:
    """Search public Hugging Face models; 10 per page, at most 10 pages (100 hits)."""
    if not ENABLED:
        raise HTTPException(503, "Haku on pois käytöstä (offline-tila tai huggingface_hub puuttuu).")
    limit = (page + 1) * SEARCH_PAGE_SIZE + 1  # one extra hit tells whether a next page exists
    try:
        models = await asyncio.to_thread(
            lambda: list(HfApi().list_models(
                search=q, sort=sort, limit=limit,
                expand=["downloads", "likes", "pipeline_tag", "createdAt", "lastModified"],
            ))
        )
    except Exception:  # noqa: BLE001
        log.exception("Hugging Face search failed")
        raise HTTPException(502, "Haku epäonnistui. Katso palvelimen loki.") from None
    start = page * SEARCH_PAGE_SIZE
    page_models = [m for m in models[start:start + SEARCH_PAGE_SIZE] if REPO_ID_RE.match(m.id)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        sizes = await asyncio.to_thread(lambda: list(pool.map(_repo_size, [m.id for m in page_models])))
    items = [
        SearchHit(repo_id=m.id, downloads=m.downloads or 0, likes=m.likes or 0, task=m.pipeline_tag,
                  created=_date(m.created_at), modified=_date(m.last_modified), size_bytes=size)
        for m, size in zip(page_models, sizes)
    ]
    has_next = len(models) > start + SEARCH_PAGE_SIZE and page + 1 < SEARCH_MAX_PAGES
    return SearchPage(items=items, page=page, page_size=SEARCH_PAGE_SIZE, has_next=has_next)


def _plan_for(req: DownloadRequest) -> tuple[rt.Plan, int]:
    """Read the repo file list (+ config.json architectures), build the plan and return it with its size."""
    info = HfApi().model_info(req.repo_id, files_metadata=True, token=req.token or None)
    sizes = {s.rfilename: (s.size or 0) for s in (info.siblings or [])}
    archs = list((getattr(info, "config", None) or {}).get("architectures") or [])
    if not archs and "config.json" in sizes and req.runtime == "auto":
        from huggingface_hub import hf_hub_download  # config.json is tiny; read it only for detection
        with tempfile.TemporaryDirectory() as d:
            cfg = hf_hub_download(req.repo_id, "config.json", local_dir=d, token=req.token or None)
            archs = list(json.loads(Path(cfg).read_text(encoding="utf-8")).get("architectures") or [])
    plan = rt.make_plan(list(sizes), archs, req.runtime, req.variant)
    return plan, sum(sizes[f] for f in plan.files)


@router.post("/downloads", response_model=JobStatus, status_code=202)
async def start_download(req: DownloadRequest) -> JobStatus:
    """Choose the runtime layout, check repo size and free space, then start a background download."""
    if not ENABLED:
        raise HTTPException(503, "Lataus on pois käytöstä (offline-tila tai huggingface_hub puuttuu).")
    if not REPO_ID_RE.match(req.repo_id):
        raise HTTPException(422, "repo_id muodossa 'organisaatio/malli'.")
    try:
        plan, total = await asyncio.to_thread(_plan_for, req)
    except rt.PlanError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        log.exception("model_info failed for %s", req.repo_id)
        raise HTTPException(404, _safe_error(exc)) from None
    final = (settings.MODELS_DIR / req.repo_id.split("/")[1]).resolve()
    if settings.MODELS_DIR not in final.parents:
        raise HTTPException(422, "Virheellinen repo_id.")
    if final.exists() and not req.overwrite:
        raise HTTPException(409, f"Kansio {final.name} on jo olemassa. Vahvista ylikirjoitus (overwrite).")
    free = settings.disk_free()
    if total > free:
        raise HTTPException(
            507, f"Levytila ei riitä: tarvitaan noin {total / 1e9:.1f} GB, vapaana {free / 1e9:.1f} GB. "
                 "Poista malleja tai kasvata levytilaa."
        )
    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(409, "Toinen lataus on jo käynnissä.")
        for stale in settings.MODELS_DIR.glob(".tmp-*"):  # leftovers of a crashed download
            shutil.rmtree(stale, ignore_errors=True)
        job_id = uuid.uuid4().hex[:12]
        tmp = settings.MODELS_DIR / f".tmp-{job_id}"
        tmp.mkdir(parents=True)
        _jobs[job_id] = {
            "id": job_id, "repo_id": req.repo_id, "state": "running",
            "downloaded_bytes": 0, "total_bytes": total, "path": None,
            "message": None, "target": str(tmp), "proc": None, "cancel": False,
            "runtime": plan.runtime, "reason": plan.reason, "files": plan.files[:50],
            "started_at": time.time(), "ended_at": None,
        }
        for old in list(_jobs)[:-MAX_JOBS_KEPT]:
            _jobs.pop(old, None)
        threading.Thread(target=_run_job, args=(_jobs[job_id], req, plan, tmp, final), daemon=True).start()
        return _to_status(_jobs[job_id])


@router.get("/downloads/{job_id}", response_model=JobStatus)
async def get_download(job_id: str) -> JobStatus:
    """Return the status of a download job."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Latausta ei löytynyt.")
    return _to_status(job)

@router.post("/downloads/{job_id}/cancel", response_model=JobStatus)
async def cancel_download(job_id: str) -> JobStatus:
    """Stop a running download; the worker then deletes the partial files of that model."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Latausta ei löytynyt.")
    if job["state"] != "running":
        raise HTTPException(409, "Lataus ei ole käynnissä.")
    job["cancel"] = True
    proc = job.get("proc")
    if proc is not None:
        proc.terminate()
    return _to_status(job)
