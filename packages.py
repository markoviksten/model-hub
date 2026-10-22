"""Import transfer packages (`<model>.tar` + `.sha256`) into the models folder."""

import hashlib
import logging
import re
import shutil
import tarfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import settings

log = logging.getLogger("model_hub.packages")

router = APIRouter(prefix="/api/packages")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_JOBS_KEPT = 20


class ImportRequest(BaseModel):
    """Request body for importing a package."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., max_length=140)
    skip_verify: bool = False


class ImportStatus(BaseModel):
    """Public status of an import job."""

    id: str
    state: Literal["running", "done", "error"]
    stage: str
    progress: int
    message: Optional[str] = None
    elapsed_s: Optional[float] = None
    stage_elapsed_s: Optional[float] = None


def _status(job: dict) -> ImportStatus:
    """Build the public status (with elapsed times) from a job dict."""
    return ImportStatus(
        id=job["id"], state=job["state"], stage=job["stage"], progress=job["progress"],
        message=job["message"], elapsed_s=settings.elapsed(job["started_at"], job["ended_at"]),
        stage_elapsed_s=settings.elapsed(job["stage_started_at"], job["ended_at"]),
    )


def _tar_path(name: str) -> Path:
    """Resolve a `.tar` file name inside the models dir or raise 4xx."""
    if not name.endswith(".tar") or not settings.FOLDER_RE.match(name[:-4]):
        raise HTTPException(422, "Virheellinen paketin nimi.")
    path = (settings.MODELS_DIR / name).resolve()
    if path.parent != settings.MODELS_DIR or not path.is_file():
        raise HTTPException(404, "Pakettia ei löydy.")
    return path


def _sha256(path: Path, job: dict) -> str:
    """Hash a file, updating `job['progress']` (0-100) while reading."""
    digest, done, total = hashlib.sha256(), 0, max(path.stat().st_size, 1)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
            done += len(chunk)
            job["progress"] = int(100 * done / total)
    return digest.hexdigest()


def _expected_sha(tar_path: Path) -> str:
    """Read the expected SHA-256 from `<tar>.sha256`."""
    sha_file = Path(str(tar_path) + ".sha256")
    if not sha_file.is_file():
        raise RuntimeError("Tiedostoa .sha256 ei löydy. Lisää se tai valitse 'ohita tarkistus'.")
    parts = sha_file.read_text(encoding="utf-8").split()
    if not parts or not SHA_RE.match(parts[0].lower()):
        raise RuntimeError("Tiedoston .sha256 sisältö on virheellinen.")
    return parts[0].lower()


def _run_import(job: dict, tar_path: Path, skip_verify: bool) -> None:
    """Worker thread: verify the checksum, then extract safely into the models dir."""
    folder = tar_path.name[:-4]
    dest = settings.MODELS_DIR / folder
    extracting = False
    try:
        if not skip_verify:
            job["stage"], job["stage_started_at"] = "Tarkistetaan SHA-256", time.time()
            expected = _expected_sha(tar_path)
            if _sha256(tar_path, job) != expected:
                raise RuntimeError("SHA-256 ei täsmää: siirto on voinut vioittua. Pakettia ei purettu.")
        job["stage"], job["progress"], job["stage_started_at"] = "Puretaan", 0, time.time()
        with tarfile.open(tar_path) as tar:
            members = tar.getmembers()
            prefix = folder + "/"
            if any(m.name != folder and not m.name.startswith(prefix) for m in members):
                raise RuntimeError(f"Paketin sisällön pitää olla kansiossa '{folder}/'.")
            extracting = True
            for i, member in enumerate(members, 1):
                tar.extract(member, settings.MODELS_DIR, filter="data")
                job["progress"] = int(100 * i / len(members))
        job["ended_at"] = time.time()
        job["state"] = "done"
    except Exception as exc:  # noqa: BLE001 - message is client-safe or generic
        log.exception("Import failed for %s", tar_path.name)
        if extracting:
            shutil.rmtree(dest, ignore_errors=True)
        job["message"] = str(exc) if isinstance(exc, RuntimeError) else "Purku epäonnistui. Katso palvelimen loki."
        job["ended_at"] = time.time()
        job["state"] = "error"


@router.get("")
def list_packages() -> dict:
    """List `.tar` packages in the models folder."""
    items = []
    if settings.MODELS_DIR.is_dir():
        for f in sorted(settings.MODELS_DIR.glob("*.tar")):
            stem = f.name[:-4]
            if f.is_file() and settings.FOLDER_RE.match(stem):
                items.append({
                    "name": f.name, "size_bytes": f.stat().st_size,
                    "has_sha256": Path(str(f) + ".sha256").is_file(),
                    "imported": (settings.MODELS_DIR / stem).is_dir(),
                })
    return {"dir": str(settings.MODELS_DIR), "packages": items}


@router.post("/import", response_model=ImportStatus, status_code=202)
def start_import(req: ImportRequest) -> ImportStatus:
    """Start importing a package (verifies SHA-256 unless `skip_verify`)."""
    tar_path = _tar_path(req.name)
    if (settings.MODELS_DIR / tar_path.name[:-4]).exists():
        raise HTTPException(409, "Kansio on jo olemassa. Poista tai nimeä se ensin.")
    with _lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(409, "Toinen tuonti on jo käynnissä.")
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "state": "running", "stage": "Aloitetaan", "progress": 0, "message": None,
               "started_at": time.time(), "stage_started_at": time.time(), "ended_at": None}
        _jobs[job_id] = job
        for old in list(_jobs)[:-MAX_JOBS_KEPT]:
            _jobs.pop(old, None)
    threading.Thread(target=_run_import, args=(job, tar_path, req.skip_verify), daemon=True).start()
    return _status(job)


@router.get("/import/{job_id}", response_model=ImportStatus)
def get_import(job_id: str) -> ImportStatus:
    """Return the status of an import job."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Tehtävää ei löytynyt.")
    return _status(job)
