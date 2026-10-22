"""Shared paths, flags and external-tool lookup for Model Hub."""

import os
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
MODELS_DIR = Path(os.environ.get("MODEL_HUB_MODELS_DIR", BASE_DIR / "models")).resolve()
TOOLS_DIR = BASE_DIR / "tools"
OFFLINE = os.environ.get("MODEL_HUB_OFFLINE", "").lower() in ("1", "true", "yes")
DEFAULT_LOADER_IMAGE = os.environ.get(
    "MODEL_HUB_LOADER_IMAGE", "registry.access.redhat.com/ubi9/ubi:latest"
)
FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXE = ".exe" if os.name == "nt" else ""


def elapsed(started: Optional[float], ended: Optional[float] = None) -> Optional[float]:
    """Seconds between `started` and `ended` (or now); None if not started."""
    if started is None:
        return None
    return round((ended or time.time()) - started, 1)


def find_tool(name: str, env_var: str = "") -> Optional[str]:
    """Locate an external tool: env override, then bundled `tools/`, then PATH."""
    override = os.environ.get(env_var) if env_var else None
    if override:
        return override
    bundled = TOOLS_DIR / f"{name}{_EXE}"
    if bundled.is_file():
        return str(bundled)
    return shutil.which(name)


def tool_env(extra: Optional[dict] = None) -> dict:
    """Process environment with the bundled `tools/` dir first on PATH."""
    env = {**os.environ, **(extra or {})}
    env["PATH"] = str(TOOLS_DIR) + os.pathsep + env.get("PATH", "")
    return env


@lru_cache(maxsize=1)
def tool_info() -> dict:
    """Return which `oc` / `rsync` binaries are used (cached after first call)."""
    oc = find_tool("oc", "OC_BIN")
    version = None
    if oc:
        try:
            res = subprocess.run([oc, "version", "--client"], capture_output=True, text=True,
                                 timeout=15, env=tool_env(), check=False)
            match = re.search(r"Client Version:\s*(\S+)", res.stdout)
            version = match.group(1) if match else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "oc": oc,
        "oc_version": version,
        "oc_bundled": bool(oc and Path(oc).parent == TOOLS_DIR),
        "rsync": find_tool("rsync"),
    }
