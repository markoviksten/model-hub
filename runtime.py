"""Choose the serving runtime (vLLM / OVMS) and the files + folder layout it expects on the PVC."""

import fnmatch
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_VARIANT = "onnx/model.onnx"
MANIFEST = ".model-hub.json"  # local-only metadata, never copied to the PVC
LOCAL_VERSION = "1"  # placeholder version folder in the local OVMS layout; renumbered on the PVC
CAUSAL_SUFFIXES = ("ForCausalLM", "ForConditionalGeneration")
VLLM_PATTERNS = (
    "config.json", "generation_config.json", "tokenizer*", "special_tokens_map.json",
    "*.safetensors", "*.safetensors.index.json", "chat_template*",
    "preprocessor_config.json", "processor_config.json",
)
OVMS_ROOT_FILES = ("config.json", "preprocessor_config.json")
SERVING_NAMES = {
    "vllm": "vLLM ... ServingRuntime for KServe (esim. vLLM NVIDIA GPU ServingRuntime for KServe)",
    "ovms": "OpenVINO Model Server",
}


class PlanError(ValueError):
    """The runtime or files cannot be chosen; the message is safe to show to the user."""


@dataclass(frozen=True)
class Plan:
    """What to download and where each file goes inside the model folder."""

    runtime: str
    reason: str
    layout: dict = field(default_factory=dict)  # repo file -> path inside the model folder
    variant: Optional[str] = None
    fmt: str = "safetensors"

    @property
    def files(self) -> list[str]:
        """Repo files to download."""
        return list(self.layout)


def detect_runtime(files: list[str], architectures: list[str]) -> tuple[str, str]:
    """Pick 'vllm' or 'ovms' from the repo file list and config.json architectures, with the reasoning."""
    arch = next((a for a in architectures if a.endswith(CAUSAL_SUFFIXES)), None)
    if arch and any(f.endswith(".safetensors") for f in files):
        return "vllm", f"arkkitehtuuri {arch} ja *.safetensors-tiedostot -> vLLM"
    has_ir = "openvino_model.xml" in files or (
        any(f.endswith(".xml") for f in files) and any(f.endswith(".bin") for f in files))
    if any(f.endswith(".onnx") for f in files):
        return "ovms", "repossa on *.onnx-tiedostoja -> OpenVINO Model Server"
    if has_ir:
        return "ovms", "repossa on OpenVINO IR (.xml + .bin) -> OpenVINO Model Server"
    raise PlanError("Runtimea ei voitu päätellä (ei LLM-safetensorsia, ONNX- tai OpenVINO-tiedostoja). "
                    "Valitse runtime itse: vllm tai ovms.")


def _vllm_files(files: list[str]) -> list[str]:
    """Root-level files matching VLLM_PATTERNS (skips original/, onnx/, *.gguf, *.pth, pytorch_model*.bin)."""
    return [f for f in files if "/" not in f and any(fnmatch.fnmatch(f, p) for p in VLLM_PATTERNS)]


def _ovms_layout(files: list[str], variant: str) -> tuple[dict, str, str]:
    """Return (layout, format, variant) for OVMS: <version>/model.onnx|model.xml+model.bin, configs in root."""
    onnx = [f for f in files if f.endswith(".onnx")]
    v = LOCAL_VERSION
    layout: dict = {}
    if variant in files and variant.endswith(".onnx"):
        base = variant.rsplit("/", 1)[-1]
        folder = variant.rsplit("/", 1)[0] + "/" if "/" in variant else ""
        layout[variant] = f"{v}/model.onnx"
        for f in files:  # external weights, e.g. model.onnx_data
            name = f[len(folder):]
            if f.startswith(folder) and "/" not in name and (name.startswith(base + "_data") or name == base + ".data"):
                layout[f] = f"{v}/{name}"
        fmt = "onnx"
    elif variant.endswith(".xml") and variant in files:
        xml, fmt = variant, "openvino"
    elif onnx:
        raise PlanError(f"Varianttia '{variant}' ei löydy. ONNX-vaihtoehdot: " + ", ".join(sorted(onnx)[:20]))
    else:
        xmls = [f for f in files if f.endswith(".xml")]
        xml = "openvino_model.xml" if "openvino_model.xml" in files else (xmls[0] if len(xmls) == 1 else "")
        if not xml:
            raise PlanError("OpenVINO-malleja on useita: anna --variant (esim. polku/malli.xml). Vaihtoehdot: "
                            + ", ".join(sorted(xmls)[:20]))
        fmt = "openvino"
    if fmt == "openvino":
        binf = xml[:-4] + ".bin"
        if binf not in files:
            raise PlanError(f"Tiedosto {binf} puuttuu.")
        layout[xml], layout[binf] = f"{v}/model.xml", f"{v}/model.bin"
        variant = xml
    for name in OVMS_ROOT_FILES:
        if name in files:
            layout[name] = name
    return layout, fmt, variant


def make_plan(files: list[str], architectures: list[str], runtime: str = "auto",
              variant: str = DEFAULT_VARIANT) -> Plan:
    """Decide runtime (when 'auto') and build the download plan; raises PlanError with a clear message."""
    if ".." in variant.split("/") or variant.startswith("/"):
        raise PlanError("Virheellinen variant.")
    if runtime == "auto":
        runtime, reason = detect_runtime(files, architectures)
    else:
        reason = f"runtime annettu käyttäjän toimesta ({runtime})"
    if runtime == "vllm":
        sel = _vllm_files(files)
        if "config.json" not in sel or not any(f.endswith(".safetensors") for f in sel):
            raise PlanError("vLLM tarvitsee repon juureen config.json:n ja *.safetensors-tiedostot.")
        return Plan("vllm", reason, {f: f for f in sel})
    layout, fmt, used = _ovms_layout(files, variant)
    return Plan("ovms", reason, layout, used, fmt)


def build_layout(src: Path, out: Path, plan: Plan) -> None:
    """Move downloaded files from `src` into the final folder layout under `out` (same filesystem)."""
    for rel, dest in plan.layout.items():
        target = out / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src / rel), str(target))


def write_manifest(folder: Path, data: dict) -> None:
    """Write the local metadata file (runtime, repo, format) into a model folder."""
    (folder / MANIFEST).write_text(json.dumps(data), encoding="utf-8")


def read_manifest(folder: Path) -> dict:
    """Read the metadata file; legacy folders without one count as vLLM-style flat folders."""
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        if data.get("runtime") in ("vllm", "ovms"):
            return data
    except (OSError, ValueError):
        pass
    return {"runtime": "vllm"}
