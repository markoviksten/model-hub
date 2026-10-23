"""Disk usage overview and deletion of downloaded models."""

import logging
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

import deployer
import downloader
import settings

log = logging.getLogger("model_hub.storage")

router = APIRouter(prefix="/api/storage")


def _tree_size(path: Path) -> int:
    """Total size in bytes of all files under `path` (incl. `.cache`)."""
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _busy() -> bool:
    """True while a download or deploy job is running."""
    return any(
        j["state"] == "running"
        for jobs in (downloader._jobs, deployer._jobs)  # noqa: SLF001
        for j in jobs.values()
    )


def _checked(name: str) -> None:
    """Validate a model folder name or raise 422."""
    if not settings.FOLDER_RE.match(name):
        raise HTTPException(422, "Virheellinen nimi.")


@router.get("")
def overview() -> dict:
    """Return disk usage and the models stored in the models folder."""
    items = []
    if settings.MODELS_DIR.is_dir():
        for entry in sorted(settings.MODELS_DIR.iterdir()):
            if entry.is_dir() and settings.FOLDER_RE.match(entry.name):
                items.append({"name": entry.name, "size_bytes": _tree_size(entry)})
    disk = settings.disk_usage()
    return {
        "dir": str(settings.MODELS_DIR),
        **disk,
        "models_bytes": sum(i["size_bytes"] for i in items),
        "items": items,
    }


@router.delete("/models/{name}")
def delete_model(name: str) -> dict:
    """Delete a model folder."""
    _checked(name)
    target = (settings.MODELS_DIR / name).resolve()
    if target.parent != settings.MODELS_DIR or not target.is_dir() or target.is_symlink():
        raise HTTPException(404, "Mallia ei löydy.")
    if _busy():
        raise HTTPException(409, "Lataus tai kopiointi on käynnissä. Odota sen valmistumista.")
    freed = _tree_size(target)
    shutil.rmtree(target)
    log.info("Deleted model folder %s (%d bytes)", name, freed)
    return {"deleted": name, "freed_bytes": freed}
