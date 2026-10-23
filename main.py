"""Model Hub: download, import and deploy models to OpenShift AI (connected or air-gapped)."""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import deployer
import downloader
import settings
import storage

app = FastAPI(title="Model Hub", docs_url=None, redoc_url=None)
app.include_router(downloader.router)
app.include_router(deployer.router)
app.include_router(storage.router)
app.mount("/static", StaticFiles(directory=settings.BASE_DIR / "static"), name="static")


@app.get("/api/info")
def info() -> dict:
    """Report available features and the external tools in use."""
    tools = settings.tool_info()
    return {
        "download_enabled": downloader.ENABLED,
        "offline": settings.OFFLINE,
        "hf_installed": downloader.HF_INSTALLED,
        "models_dir": str(settings.MODELS_DIR),
        "oc": tools["oc"],
        "oc_version": tools["oc_version"],
        "oc_bundled": tools["oc_bundled"],
        "rsync": tools["rsync"],
        "loader_image": settings.DEFAULT_LOADER_IMAGE,
    }


@app.get("/")
def index() -> FileResponse:
    """Serve the single-page UI."""
    return FileResponse(settings.BASE_DIR / "static" / "index.html")
