"""Disk usage overview and deletion of downloaded/imported models and packages."""

import logging
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

import deployer
import downloader
import packages
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
    """True while a download, import or deploy job is running."""
    return any(
        j["state"] == "running"
        for jobs in (downloader._jobs, packages._jobs, deployer._jobs)  # noqa: SLF001
        for j in jobs.values()
    )


def _checked(name: str) -> None:
    """Validate a folder/package base name or raise 422."""
    if not settings.FOLDER_RE.match(name):
        raise HTTPException(422, "Virheellinen nimi.")


@router.get("")
def overview() -> dict:
    """Return disk usage and the models/packages stored in the models folder."""
    items = []
    if settings.MODELS_DIR.is_dir():
        for entry in sorted(settings.MODELS_DIR.iterdir()):
            if not settings.FOLDER_RE.match(entry.name.removesuffix(".tar")):
                continue
            if entry.is_dir():
                items.append({"kind": "model", "name": entry.name, "size_bytes": _tree_size(entry)})
            elif entry.is_file() and entry.name.endswith(".tar"):
                items.append({"kind": "package", "name": entry.name[:-4], "size_bytes": entry.stat().st_size})
    disk = settings.disk_usage()
    return {
        "dir": str(settings.MODELS_DIR),
        **disk,
        "models_bytes": sum(i["size_bytes"] for i in items),
        "items": items,
    }


@router.delete("/models/{name}")
def delete_model(name: str) -> dict:
    """Delete an extracted model folder (the `.tar` package, if any, is kept)."""
    _checked(name)
    target = (settings.MODELS_DIR / name).resolve()
    if target.parent != settings.MODELS_DIR or not target.is_dir() or target.is_symlink():
        raise HTTPException(404, "Mallia ei löydy.")
    if _busy():
        raise HTTPException(409, "Lataus, tuonti tai kopiointi on käynnissä. Odota sen valmistumista.")
    freed = _tree_size(target)
    shutil.rmtree(target)
    log.info("Deleted model folder %s (%d bytes)", name, freed)
    return {"deleted": name, "freed_bytes": freed}


@router.delete("/packages/{name}")
def delete_package(name: str) -> dict:
    """Delete a `<name>.tar` package and its `.sha256` file."""
    _checked(name)
    tar = (settings.MODELS_DIR / f"{name}.tar").resolve()
    if tar.parent != settings.MODELS_DIR or not tar.is_file():
        raise HTTPException(404, "Pakettia ei löydy.")
    if _busy():
        raise HTTPException(409, "Lataus, tuonti tai kopiointi on käynnissä. Odota sen valmistumista.")
    freed = tar.stat().st_size
    tar.unlink()
    Path(str(tar) + ".sha256").unlink(missing_ok=True)
    log.info("Deleted package %s (%d bytes)", name, freed)
    return {"deleted": f"{name}.tar", "freed_bytes": freed}
