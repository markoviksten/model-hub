"""Hugging Face download feature (off when huggingface_hub is missing or MODEL_HUB_OFFLINE=1)."""

import asyncio
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

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
    token: Optional[str] = Field(default=None, max_length=200, repr=False)


class JobStatus(BaseModel):
    """Public status of a download job."""

    id: str
    repo_id: str
    state: Literal["running", "done", "error"]
    downloaded_bytes: int
    total_bytes: int
    path: Optional[str] = None
    message: Optional[str] = None
    elapsed_s: Optional[float] = None


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


def _run_job(job: dict, req: DownloadRequest, target: Path) -> None:
    """Worker thread: download the repo, then set the final state."""
    try:
        snapshot_download(repo_id=req.repo_id, local_dir=str(target), token=req.token or None)
        job["path"] = str(target)
        job["message"] = None
        job["ended_at"] = time.time()
        job["state"] = "done"
    except Exception as exc:  # noqa: BLE001 - mapped to a safe message
        log.exception("Download failed for %s", req.repo_id)
        job["message"] = _safe_error(exc)
        job["ended_at"] = time.time()
        job["state"] = "error"


def _to_status(job: dict) -> JobStatus:
    """Build a JobStatus, refreshing downloaded bytes from disk while running."""
    if job["state"] == "running":
        job["downloaded_bytes"] = _dir_size(Path(job["target"]))
    elif job["state"] == "done":
        job["downloaded_bytes"] = max(job["downloaded_bytes"], job["total_bytes"])
    public = {k: v for k, v in job.items() if k not in ("target", "started_at", "ended_at")}
    return JobStatus(**public, elapsed_s=settings.elapsed(job["started_at"], job["ended_at"]))


class SearchHit(BaseModel):
    """One Hugging Face search result."""

    repo_id: str
    downloads: int = 0
    likes: int = 0
    task: Optional[str] = None
    created: Optional[str] = None
    modified: Optional[str] = None


class SearchPage(BaseModel):
    """One page of search results."""

    items: list[SearchHit]
    page: int
    page_size: int
    has_next: bool


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
    items = [
        SearchHit(repo_id=m.id, downloads=m.downloads or 0, likes=m.likes or 0, task=m.pipeline_tag,
                  created=_date(m.created_at), modified=_date(m.last_modified))
        for m in models[start:start + SEARCH_PAGE_SIZE]
        if REPO_ID_RE.match(m.id)
    ]
    has_next = len(models) > start + SEARCH_PAGE_SIZE and page + 1 < SEARCH_MAX_PAGES
    return SearchPage(items=items, page=page, page_size=SEARCH_PAGE_SIZE, has_next=has_next)


@router.post("/downloads", response_model=JobStatus, status_code=202)
async def start_download(req: DownloadRequest) -> JobStatus:
    """Start a background download of a Hugging Face repo."""
    if not ENABLED:
        raise HTTPException(503, "Lataus on pois käytöstä (offline-tila tai huggingface_hub puuttuu).")
    if not REPO_ID_RE.match(req.repo_id):
        raise HTTPException(422, "repo_id muodossa 'organisaatio/malli'.")
    try:
        info = await asyncio.to_thread(
            HfApi().model_info, req.repo_id, files_metadata=True, token=req.token or None
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("model_info failed for %s", req.repo_id)
        raise HTTPException(404, _safe_error(exc)) from None
    total = sum((s.size or 0) for s in (info.siblings or []))
    target = (settings.MODELS_DIR / req.repo_id.split("/")[1]).resolve()
    if settings.MODELS_DIR not in target.parents:
        raise HTTPException(422, "Virheellinen repo_id.")
    need = total - _dir_size(target)
    free = settings.disk_free()
    if need > free:
        raise HTTPException(
            507, f"Levytila ei riitä: tarvitaan noin {need / 1e9:.1f} GB, vapaana {free / 1e9:.1f} GB. "
                 "Poista malleja tai kasvata levytilaa."
        )
    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(409, "Toinen lataus on jo käynnissä.")
        target.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {
            "id": job_id, "repo_id": req.repo_id, "state": "running",
            "downloaded_bytes": 0, "total_bytes": total, "path": None,
            "message": None, "target": str(target),
            "started_at": time.time(), "ended_at": None,
        }
        for old in list(_jobs)[:-MAX_JOBS_KEPT]:
            _jobs.pop(old, None)
        threading.Thread(target=_run_job, args=(_jobs[job_id], req, target), daemon=True).start()
        return _to_status(_jobs[job_id])


@router.get("/downloads/{job_id}", response_model=JobStatus)
async def get_download(job_id: str) -> JobStatus:
    """Return the status of a download job."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Latausta ei löytynyt.")
    return _to_status(job)
