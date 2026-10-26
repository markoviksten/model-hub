"""Deployment advice for a downloaded model, derived from its local config.json and file sizes.

All numbers are rough estimates meant as a starting point, not guarantees.
"""
import json
import math
import re
from pathlib import Path

import runtime as rt

GI = 1024 ** 3
NODE_GI = 56            # usable memory of a typical worker node (60 GB nodes)
_MAX_CONFIG = 5 * 1024 * 1024
_SAFE = re.compile(r"[^\w.\-+/ ]")


def _s(value, limit: int = 60) -> str:
    """Sanitize an untrusted config string: drop odd characters and cap the length."""
    return _SAFE.sub("", str(value))[:limit]


def _num(value, default: float = 0) -> float:
    """Return `value` if it is a plain positive number, else `default`."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default


def _load_config(folder: Path) -> dict:
    """Read config.json (size-capped); return {} when missing or invalid."""
    path = folder / "config.json"
    try:
        if path.stat().st_size > _MAX_CONFIG:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _round_up(value: float, step: int = 2, minimum: int = 1) -> int:
    """Round up to a multiple of `step`, at least `minimum`."""
    return max(minimum, int(math.ceil(value / step) * step))


def _quant(cfg: dict) -> dict | None:
    """Quantization info {method, format} from config, or None for unquantized models."""
    q = cfg.get("quantization_config")
    if not isinstance(q, dict):
        return None
    method = _s(q.get("quant_method", "tuntematon")).lower()
    fmt = ""
    groups = q.get("config_groups")
    if isinstance(groups, dict):
        for g in groups.values():
            if isinstance(g, dict) and g.get("format"):
                fmt = _s(g["format"]).lower()
                break
    fmt = fmt or _s(q.get("format", "")).lower()
    return {"method": method, "format": fmt}


def _kv_bytes_per_token(text: dict) -> int:
    """Estimated KV-cache bytes per token (bf16), or 0 if the config lacks the fields."""
    layers = _num(text.get("num_hidden_layers"))
    heads = _num(text.get("num_attention_heads"))
    kv_heads = _num(text.get("num_key_value_heads"), heads)
    hidden = _num(text.get("hidden_size"))
    head_dim = _num(text.get("head_dim"), hidden / heads if heads else 0)
    return int(2 * layers * kv_heads * head_dim * 2) if layers and kv_heads and head_dim else 0


def _timeout(size_gi: float) -> int:
    """Suggested route timeout (seconds) from the model size."""
    return 300 if size_gi < 2 else 600 if size_gi < 10 else 1800


def _warnings(quant, moe, multimodal, size_gi, mem_gi, runtime, embedding) -> list[dict]:
    """Build the list of {level, text} cautions for this model."""
    out: list[dict] = []
    add = lambda level, text: out.append({"level": level, "text": text})  # noqa: E731
    if runtime == "ovms":
        add("info", "OpenVINO Model Server toimii ilman GPU:ta; vLLM-parametrit eivät koske tätä mallia.")
        if mem_gi > NODE_GI:
            add("warn", f"Arvioitu muisti (~{mem_gi} Gi) ylittää tyypillisen noodin ({NODE_GI} Gi).")
        return out
    if quant:
        q = f"{quant['method']} {quant['format']}".lower()
        if "fp4" in q:
            add("warn", "NVFP4/FP4 on ensisijaisesti GPU-formaatti (Blackwell). CPU-vLLM ei välttämättä "
                        "tue sitä, ja käynnistys voi epäonnistua. Jos painot puretaan bf16:ksi, muistitarve voi olla "
                        "moninkertainen (tässä ~" + str(_round_up(size_gi * 3.5)) + " Gi). Varmista tuki tai valitse bf16/int8-malli.")
        elif "fp8" in q or "float-quantized" in q:
            add("warn", "FP8-painot: CPU-tuki on rajallinen ja riippuu vLLM-versiosta. GPU:lla tarvitaan Ada/Hopper tai uudempi.")
        elif any(k in q for k in ("awq", "gptq", "compressed-tensors", "int4", "int8")):
            add("info", "Kvantisoitu malli: varmista, että käyttämäsi vLLM-versio tukee formaattia CPU:lla/GPU:lla.")
        elif "bitsandbytes" in q:
            add("warn", "bitsandbytes-kvantisointi ei toimi CPU:lla.")
        add("info", "Kvantisoidulla mallilla --dtype jätetään yleensä automaattiseksi (ei --dtype-lippua).")
    if mem_gi > NODE_GI:
        add("warn", f"Arvioitu CPU-muisti (~{mem_gi} Gi) ylittää tyypillisen noodin ({NODE_GI} Gi). "
                    "Malli ei mahdu CPU:lle; valitse pienempi malli tai käytä GPU:ta.")
    if moe:
        add("info", "MoE-malli: kaikki asiantuntijat ladataan muistiin, vaikka aktiivisia parametreja on vähän. "
                    "Muistitarve määräytyy koko painotiedoston mukaan.")
    if multimodal:
        add("info", "Multimodaalinen (kuva+teksti). Kuvasyöte käyttää lisämuistia; jos et tarvitse kuvia, "
                    "rajaa kuvien määrä (vLLM: --limit-mm-per-prompt, tarkista syntaksi versiosta).")
    if embedding:
        add("info", "Embedding-malli: vLLM:ssä tarvitaan embedding-tila (--task=embed tai --runner=pooling versiosta riippuen).")
    if size_gi >= 10:
        add("info", "Suuri malli: käynnistys ja ensimmäinen pyyntö voivat kestää useita minuutteja. "
                    "Nosta readiness/liveness-aikarajoja ja reitin timeout.")
    return out


def advise(folder: Path, size_bytes: int) -> dict:
    """Return summary, CPU/GPU recommendations and warnings for a local model folder."""
    manifest = rt.read_manifest(folder)
    runtime = manifest.get("runtime", "vllm")
    cfg = _load_config(folder)
    text = cfg.get("text_config") if isinstance(cfg.get("text_config"), dict) else cfg
    archs = [_s(a) for a in cfg.get("architectures", []) if isinstance(a, str)][:3]
    quant = _quant(cfg)
    experts = _num(text.get("num_experts") or text.get("num_local_experts") or text.get("n_routed_experts"))
    moe = experts > 1
    multimodal = isinstance(cfg.get("vision_config"), dict) or any("ConditionalGeneration" in a for a in archs)
    native_ctx = int(_num(text.get("max_position_embeddings")))
    size_gi = size_bytes / GI
    summary = {
        "architecture": ", ".join(archs) or None,
        "model_type": _s(cfg.get("model_type", "")) or None,
        "size_bytes": size_bytes,
        "dtype": _s(cfg.get("dtype") or cfg.get("torch_dtype") or "") or None,
        "quantization": quant,
        "moe": moe,
        "experts": int(experts) if moe else None,
        "experts_per_token": int(_num(text.get("num_experts_per_tok") or text.get("top_k_experts") or text.get("moe_top_k"))) or None,
        "multimodal": multimodal,
        "native_context": native_ctx or None,
        "runtime": runtime,
        "embedding": bool(manifest.get("embedding")),
    }
    pvc_gi = _round_up(size_gi * 1.2 + 1, 1, 2)
    result = {"name": folder.name, "estimate": True, "summary": summary, "pvc_gi": pvc_gi, "cpu": None, "gpu": None}

    if runtime == "ovms":
        mem = _round_up(size_gi * 2 + 1, 1, 2)
        result["cpu"] = {"memory_gi": mem, "cpu": 2 if size_gi < 2 else 4, "args": [], "env": {}}
        result["route_timeout_s"] = 300
        result["warnings"] = _warnings(quant, moe, multimodal, size_gi, mem, runtime, summary["embedding"])
        return result

    per_tok = _kv_bytes_per_token(text)
    # CPU: modest context, 2 concurrent sequences
    cpu_ctx = min(native_ctx or 2048, 4096 if size_gi < 2 else 2048)
    kv_gi = max(1, math.ceil(per_tok * cpu_ctx * 2 / GI)) if per_tok else (1 if size_gi < 2 else 4)
    mem = max(8, _round_up(size_gi * 1.15 + kv_gi + 3))
    cpu_args = [f"--max-model-len={cpu_ctx}"]
    if not quant:
        cpu_args.insert(0, "--dtype=bfloat16")
    result["cpu"] = {
        "memory_gi": mem,
        "cpu": 4 if size_gi < 2 else 8 if size_gi < 10 else 16,
        "kv_cache_gi": kv_gi,
        "max_model_len": cpu_ctx,
        "args": cpu_args,
        "env": {"VLLM_CPU_KVCACHE_SPACE": str(kv_gi), "HF_HUB_OFFLINE": "1"},
    }
    # GPU: weights + KV + headroom, longer context
    gpu_ctx = min(native_ctx or 8192, 8192)
    gpu_kv = per_tok * gpu_ctx * 4 / GI if per_tok else 2
    gpu_gi = _round_up(size_gi * 1.2 + gpu_kv, 2, 4)
    gpu_args = [f"--max-model-len={gpu_ctx}", "--gpu-memory-utilization=0.9"]
    if summary["embedding"]:
        cpu_args.append("--task=embed")
        gpu_args.append("--task=embed")
    result["gpu"] = {"gpu_memory_gi": gpu_gi, "max_model_len": gpu_ctx, "args": gpu_args, "env": {}}
    result["route_timeout_s"] = _timeout(size_gi)
    result["warnings"] = _warnings(quant, moe, multimodal, size_gi, mem, runtime, summary["embedding"])
    return result
