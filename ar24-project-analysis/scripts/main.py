import os
import re
import subprocess
import tempfile
import requests
import shutil
import json

# ==========================================
# 🌐 网络与代理基础配置（本地化：默认直连，可经 env 配置）
# ==========================================
# 仅当用户显式配置了代理时才启用；本地通常可直连公网。
PROXY_URL = os.environ.get(
    "REPRO_HTTP_PROXY",
    os.environ.get("http_proxy", os.environ.get("HTTP_PROXY", "")),
)

if PROXY_URL:
    os.environ["http_proxy"] = PROXY_URL
    os.environ["https_proxy"] = PROXY_URL
    REQ_PROXIES = {"http": PROXY_URL, "https": PROXY_URL}
else:
    REQ_PROXIES = None  # 直连
REQ_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"}

# Gated Model 本地配置库路径
GATED_MODEL_MIRRORS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "gated_model_mirrors.yaml"
)


def _writable(path: str) -> bool:
    """Return whether path, or its nearest existing parent, is writable."""
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return bool(p) and os.access(p, os.W_OK)


def resolve_workspace_root() -> str:
    """
    Keep audit artifacts in the explicit project root selected by the user.
    """
    env = (os.environ.get("WORKSPACE_ROOT") or "").strip()
    if env:
        return env
    raise RuntimeError(
        "WORKSPACE_ROOT is required before project analysis. Complete the "
        "path-confirmation step and export WORKSPACE_ROOT=<repro_root>."
    )


def resolve_run_id(value: str | None = None) -> str:
    run_id = str(value or os.environ.get("RUN_ID") or "").strip()
    if not re.fullmatch(r"run-\d{3,}", run_id):
        raise RuntimeError("RUN_ID is required and must use the form run-NNN")
    return run_id




# ==========================================
# 📐 确定性多维评分契约 (machine-owned scorecard)
# ==========================================
SCIML_PHYSICS_DIMENSIONS = {
    "documentation_entry": {"label": "文档与入口闭环", "max": 10},
    "environment": {"label": "多运行时环境可构建性", "max": 15},
    "artifacts": {"label": "数据、网格与参数资产", "max": 15},
    "migration_cost": {"label": "运行迁移成本", "max": 10},
    "experiment_reproduction": {"label": "计算链复现实验闭环", "max": 20},
    "physics_fidelity": {"label": "物理工况与验证可信度", "max": 20},
    "risk_gate": {"label": "安全/合规红线", "max": 10},
}

MIGRATION_SUBSIGNALS = {
    "path_migration": "路径迁移",
    "hardware_gpu_migration": "硬件/GPU 迁移",
    "runtime_param_migration": "运行参数迁移",
    "system_dependency_migration": "系统依赖迁移",
}

GPU_PROFILES = {
    "4090": {"name": "RTX 4090", "vram_gb": 24, "arch": "Ada", "sm": "8.9", "class": "consumer"},
    "rtx 4090": {"name": "RTX 4090", "vram_gb": 24, "arch": "Ada", "sm": "8.9", "class": "consumer"},
    "l40s": {"name": "NVIDIA L40S", "vram_gb": 48, "arch": "Ada", "sm": "8.9", "class": "datacenter"},
    "h100": {"name": "NVIDIA H100", "vram_gb": 80, "arch": "Hopper", "sm": "9.0", "class": "datacenter"},
    "a100": {"name": "NVIDIA A100", "vram_gb": 80, "arch": "Ampere", "sm": "8.0", "class": "datacenter"},
    "3090": {"name": "RTX 3090", "vram_gb": 24, "arch": "Ampere", "sm": "8.6", "class": "consumer"},
    "rtx 3090": {"name": "RTX 3090", "vram_gb": 24, "arch": "Ampere", "sm": "8.6", "class": "consumer"},
    "v100": {"name": "NVIDIA V100", "vram_gb": 32, "arch": "Volta", "sm": "7.0", "class": "datacenter"},
}

EVIDENCE_LEVELS = {
    "E0": "未检查，不参与扣分",
    "E1": "弱证据，关键词或启发式推断",
    "E2": "中证据，文件存在但信息不完整",
    "E3": "强证据，明确文件、行号、HTTP状态或确定性规则命中",
    "E4": "红线证据，触发 BLOCKED",
}

LAST_AUDIT_SCORECARD = None


def new_scorecard(repo_url: str) -> dict:
    return {
        "version": "4.0",
        "scoring_profile": "sciml_physics",
        "scoring_owner": "deterministic_static_rules",
        "model_role": "explain_only",
        "repo_url": repo_url,
        "project_mode": "SCIML_PHYSICS",
        "detected_domain": None,
        "overall_score": 0,
        "verdict": "UNSCORABLE",
        "risk_gate_status": "PASS",
        "hardware_migration": {},
        "resource_gate": {
            "fit_status": "unknown",
            "action": "confirm",
            "recommended_resource": "unknown",
            "message": "Resource Gate 尚未运行或证据不足。",
        },
        "physics_gate": {
            "fit_status": "pending",
            "action": "inspect",
            "execution_blockers": [],
            "claim_blockers": [],
            "message": "SciML/计算物理项目必须完成物理门禁检查。",
        },
        "dimensions": {
            key: {"label": spec["label"], "score": spec["max"], "max": spec["max"], "deductions": []}
            for key, spec in SCIML_PHYSICS_DIMENSIONS.items()
        },
        "blockers": [],
        "deductions": [],
        "migration_subsignals": MIGRATION_SUBSIGNALS,
        "evidence_levels": EVIDENCE_LEVELS,
    }


def configure_scorecard_profile(scorecard: dict, profile: str = "sciml_physics") -> dict:
    """Apply the sole SciML/physics scorecard, regardless of classifier output.

    ``profile`` remains accepted for callers using the former API, but it can no
    longer select a conventional ML or software-engineering scorecard.
    """
    scorecard["scoring_profile"] = "sciml_physics"
    dimensions = SCIML_PHYSICS_DIMENSIONS
    scorecard["dimensions"] = {
        key: {"label": spec["label"], "score": spec["max"], "max": spec["max"], "deductions": []}
        for key, spec in dimensions.items()
    }
    scorecard["deductions"] = []
    scorecard["blockers"] = []
    return scorecard


def add_deduction(scorecard: dict, dimension: str, points: int, evidence_level: str,
                  reason: str, file: str = None, line: int = None,
                  code: str = None, blocker: bool = False):
    if not scorecard or dimension not in scorecard["dimensions"]:
        return
    dim = scorecard["dimensions"][dimension]
    points = max(0, int(points))
    actual = min(points, dim["score"])
    dim["score"] -= actual
    item = {
        "dimension": dimension,
        "points": -actual,
        "requested_points": -points,
        "evidence_level": evidence_level,
        "reason": reason,
    }
    if code:
        item["code"] = code
    if file:
        item["file"] = file
    if line is not None:
        item["line"] = line
    dim["deductions"].append(item)
    scorecard["deductions"].append(item)
    if blocker:
        scorecard["blockers"].append(item)
        if dimension == "risk_gate":
            scorecard["risk_gate_status"] = "BLOCKED"


def finalize_scorecard(scorecard: dict) -> dict:
    overall = sum(dim["score"] for dim in scorecard["dimensions"].values())
    scorecard["overall_score"] = max(0, min(100, overall))
    if scorecard.get("blockers"):
        scorecard["verdict"] = "BLOCKED"
    elif scorecard["overall_score"] >= 80:
        scorecard["verdict"] = "PASS"
    elif scorecard["overall_score"] >= 60:
        scorecard["verdict"] = "CONDITIONAL"
    else:
        scorecard["verdict"] = "HIGH_RISK"
    return scorecard


def append_scorecard_summary(report: list, scorecard: dict):
    report.append(f"\n📐 **SciML/计算物理确定性多维评分（v{scorecard.get('version', '4.0')}，模型仅解释证据，不直接改分）**")
    report.append(f"- scoring_profile: `{scorecard.get('scoring_profile', 'sciml_physics')}`")
    report.append("| 维度 | 得分 | 扣分摘要 |")
    report.append("| :-- | :-- | :-- |")
    for key, dim in scorecard["dimensions"].items():
        reasons = "；".join(d["reason"] for d in dim["deductions"][:2]) or "无扣分"
        if len(dim["deductions"]) > 2:
            reasons += f"；另 {len(dim['deductions']) - 2} 项"
        report.append(f"| {dim['label']} | {dim['score']} / {dim['max']} | {reasons} |")
    if scorecard.get("blockers"):
        report.append("\n🚫 **红线阻断项**")
        for item in scorecard["blockers"]:
            report.append(f"- [{item['evidence_level']}] {item['reason']}")
    hw = scorecard.get("hardware_migration") or {}
    if hw:
        report.append("\n🖥️ **硬件/GPU 迁移判断**")
        report.append(f"- 作者/论文 GPU: {hw.get('author_gpu', '未识别')}")
        report.append(f"- 本机 GPU: {hw.get('local_gpu', '未识别')}")
        report.append(f"- 结论: {hw.get('conclusion', '未形成结论')}")
    report.append(f"\n🚦 **安全/合规 Gate：{scorecard.get('risk_gate_status', 'PASS')}**")
    report.append(f"📊 **最终确定性复现可行性分：{scorecard['overall_score']} / 100**")
    report.append(f"🏷️ **Verdict: `{scorecard['verdict']}`**")



def _normalize_gpu_name(text: str):
    if not text:
        return None
    lower = text.lower()
    for key, profile in GPU_PROFILES.items():
        if key in lower:
            return profile.copy()
    return None


def _probe_local_gpu_profile() -> dict:
    env_spec = os.environ.get("LOCAL_GPU_SPEC") or os.environ.get("REPRO_LOCAL_GPU")
    if env_spec:
        prof = _normalize_gpu_name(env_spec) or {"name": env_spec, "vram_gb": None, "arch": "unknown", "sm": None, "class": "unknown"}
        prof["source"] = "env"
        return prof
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=8
        )
        if r.returncode == 0 and r.stdout.strip():
            first = r.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first.split(",")]
            prof = _normalize_gpu_name(parts[0]) or {"name": parts[0], "vram_gb": None, "arch": "unknown", "sm": None, "class": "unknown"}
            if len(parts) > 1:
                try:
                    prof["vram_gb"] = round(float(parts[1]) / 1024, 1)
                except Exception:
                    pass
            prof["source"] = "nvidia-smi"
            return prof
    except Exception:
        pass
    return {"name": "未检测到 GPU", "vram_gb": 0, "arch": "none", "sm": None, "class": "cpu", "source": "nvidia-smi"}


def _infer_author_gpu_profile(project_dir: str) -> dict:
    env_spec = os.environ.get("AUTHOR_GPU_SPEC") or os.environ.get("PAPER_GPU_SPEC")
    if env_spec:
        prof = _normalize_gpu_name(env_spec) or {"name": env_spec, "vram_gb": None, "arch": "unknown", "sm": None, "class": "unknown"}
        prof["source"] = "env"
        return prof
    candidate_files = []
    for root, _, files in os.walk(project_dir):
        if ".git" in root:
            continue
        for fname in files:
            if fname.lower() in {"readme.md", "requirements.txt", "environment.yml", "environment.yaml"} or fname.lower().endswith((".md", ".yaml", ".yml", ".json", ".sh")):
                path = os.path.join(root, fname)
                if os.path.getsize(path) <= 512 * 1024:
                    candidate_files.append(path)
    for path in candidate_files[:80]:
        try:
            content = open(path, "r", encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        prof = _normalize_gpu_name(content)
        if prof:
            prof["source"] = os.path.relpath(path, project_dir)
            return prof
    return {"name": "未识别", "vram_gb": None, "arch": "unknown", "sm": None, "class": "unknown", "source": "not_found"}


def evaluate_hardware_migration(project_dir: str, report: list, scorecard: dict):
    """Describe author/README GPU -> selected backend differences without scoring them."""
    local = _probe_local_gpu_profile()
    author = _infer_author_gpu_profile(project_dir)
    local_name = local.get("name", "未识别")
    author_name = author.get("name", "未识别")
    conclusion = "未识别作者/论文 GPU，按本机 GPU 做后续实测验证"
    resource_note = None

    if local.get("class") == "cpu" or (local.get("vram_gb") == 0):
        resource_note = "本机无可用 GPU，但项目属于 GPU/深度学习复现场景"
        conclusion = "本机无 GPU，GPU 论文结果只能做 CPU 降级 smoke test"
    elif author.get("name") and author.get("name") != "未识别":
        lv = local.get("vram_gb")
        av = author.get("vram_gb")
        same_sm = local.get("sm") and author.get("sm") and local.get("sm") == author.get("sm")
        same_arch = local.get("arch") != "unknown" and local.get("arch") == author.get("arch")
        if av and lv and lv < av * 0.8:
            resource_note = f"本机 GPU 显存约 {lv}GB，低于作者/论文 GPU {author_name} 约 {av}GB 的 80%"
            conclusion = "显存低于论文环境，需降低 batch/grid/分辨率"
        elif same_sm or same_arch:
            conclusion = f"{author_name} -> {local_name} 架构/compute capability 兼容，显存不构成迁移阻塞"
        elif av and lv and lv >= av:
            resource_note = "本机显存不低于作者/论文 GPU，但 GPU 架构不同，需要验证 CUDA 扩展/wheel 兼容"
            conclusion = "显存充足，架构差异需在 step_4 编译/导入验证"
        else:
            resource_note = "作者/论文 GPU 与本机 GPU 架构信息不完全一致，需要验证 CUDA 扩展和驱动兼容"
            conclusion = "硬件迁移风险中低，需要运行时验证"

    scorecard["hardware_migration"] = {
        "author_gpu": author_name,
        "author_gpu_source": author.get("source"),
        "local_gpu": f"{local_name} ({local.get('vram_gb')}GB)",
        "local_gpu_source": local.get("source"),
        "deduction_points": 0,
        "resource_note": resource_note,
        "conclusion": conclusion,
    }
    report.append("\n🖥️ [3b/6] 检查【运行迁移成本：硬件/GPU 适配】...")
    report.append(f"  作者/论文 GPU: {author_name} (来源: {author.get('source')})")
    report.append(f"  本机 GPU: {local_name}, 显存约 {local.get('vram_gb')}GB (来源: {local.get('source')})")
    report.append(f"  结论: {conclusion}")
    if resource_note:
        report.append(f"  ⚠️ 资源提示（不参与可复现性评分）：{resource_note}")


def _extract_model_params_b(model_id: str):
    """Infer parameter count in billions from common model id patterns like 7B/72b/1.5B."""
    if not model_id:
        return None
    text = model_id.lower()
    patterns = [
        r"(?<!\d)(\d+(?:\.\d+)?)\s*b(?:\b|[-_/])",
        r"(?<!\d)(\d+(?:\.\d+)?)\s*billion\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def _resource_from_param_count(params_b: float) -> dict:
    """Return conservative default inference resources for FP16-ish reproduction."""
    if params_b >= 65:
        return {
            "min_vram_gb": 140,
            "recommended_vram_gb": 160,
            "requires_multi_gpu": True,
            "recommended_resource": ">= 2x A100/H100 80GB 或等效多卡显存",
        }
    if params_b >= 30:
        return {
            "min_vram_gb": 80,
            "recommended_vram_gb": 80,
            "requires_multi_gpu": False,
            "recommended_resource": "A100/H100 80GB",
        }
    if params_b >= 13:
        return {
            "min_vram_gb": 32,
            "recommended_vram_gb": 48,
            "requires_multi_gpu": False,
            "recommended_resource": "L40S/A6000 48GB 或 A100/H100",
        }
    if params_b >= 7:
        return {
            "min_vram_gb": 16,
            "recommended_vram_gb": 24,
            "requires_multi_gpu": False,
            "recommended_resource": "RTX 4090/3090 24GB 或更高",
        }
    if params_b >= 3:
        return {
            "min_vram_gb": 12,
            "recommended_vram_gb": 16,
            "requires_multi_gpu": False,
            "recommended_resource": "16GB GPU 或更高",
        }
    return {
        "min_vram_gb": 8,
        "recommended_vram_gb": 16,
        "requires_multi_gpu": False,
        "recommended_resource": "16GB GPU 或更高",
    }


def _recommended_resource_for_vram(min_vram_gb: float, recommended_vram_gb: float, requires_multi_gpu: bool) -> str:
    if requires_multi_gpu or min_vram_gb > 80:
        return ">= 2x A100/H100 80GB 或等效多卡显存"
    target = recommended_vram_gb or min_vram_gb
    if target >= 80:
        return "A100/H100 80GB"
    if target >= 48:
        return "L40S/A6000 48GB 或 A100/H100"
    if target >= 24:
        return "RTX 4090/3090 24GB 或更高"
    if target >= 16:
        return "16GB GPU 或更高"
    return "8GB GPU 或更高"


def infer_resource_requirements(analyzed_configs=None, model_findings=None, detected_domain: str = "") -> dict:
    """
    Build a structured resource requirement contract from static audit evidence.

    fit is evaluated later against the selected backend. Unknown evidence should not
    auto-stop, but it must be surfaced before environment setup.
    """
    analyzed_configs = analyzed_configs or []
    model_findings = model_findings or []
    evidence = []
    min_vram = 0
    recommended_vram = 0
    requires_multi_gpu = False

    for item in analyzed_configs:
        item_min = float(item.get("min_vram_gb") or 0)
        item_rec = float(item.get("recommended_vram_gb") or item_min or 0)
        if item_min:
            min_vram = max(min_vram, item_min)
            recommended_vram = max(recommended_vram, item_rec)
            if item.get("requires_multi_gpu"):
                requires_multi_gpu = True
            evidence.append({
                "source": "config",
                "path": item.get("path"),
                "load_level": item.get("load_level"),
                "min_vram_gb": item_min,
                "recommended_vram_gb": item_rec,
            })

    for item in model_findings:
        model_id = item.get("model_id", "")
        params_b = _extract_model_params_b(model_id)
        if params_b is None:
            continue
        profile = _resource_from_param_count(params_b)
        min_vram = max(min_vram, profile["min_vram_gb"])
        recommended_vram = max(recommended_vram, profile["recommended_vram_gb"])
        requires_multi_gpu = requires_multi_gpu or profile["requires_multi_gpu"]
        evidence.append({
            "source": "model_id",
            "model_id": model_id,
            "params_b": params_b,
            "min_vram_gb": profile["min_vram_gb"],
            "recommended_vram_gb": profile["recommended_vram_gb"],
            "recommended_resource": profile["recommended_resource"],
        })

    requires_gpu = bool(evidence) or any(
        token in (detected_domain or "")
        for token in ("大语言", "深度学习", "计算机视觉", "科学计算", "物理 AI")
    )

    confidence = "estimated" if evidence else "unknown"
    recommended_resource = (
        _recommended_resource_for_vram(min_vram, recommended_vram, requires_multi_gpu)
        if evidence
        else "无法静态确认；需人工确认模型参数量、batch/resolution/seq_len"
    )

    return {
        "requires_gpu": requires_gpu,
        "requires_multi_gpu": requires_multi_gpu,
        "min_vram_gb": min_vram,
        "recommended_vram_gb": recommended_vram,
        "recommended_resource": recommended_resource,
        "confidence": confidence,
        "evidence": evidence,
    }


def _backend_from_env_or_local() -> dict:
    """Prefer step_0 exported selected-backend fields, then fall back to local probe."""
    raw_gpus = os.environ.get("REPRO_SELECTED_GPUS_JSON")
    if raw_gpus:
        try:
            gpus = json.loads(raw_gpus)
            if isinstance(gpus, list):
                gpu_count = int(os.environ.get("REPRO_GPU_COUNT") or len(gpus))
                mode = os.environ.get("REPRO_MODE") or ("multi_gpu" if gpu_count > 1 else "single_gpu" if gpu_count == 1 else "cpu")
                return {"mode": mode, "gpu_count": gpu_count, "gpus": gpus}
        except Exception:
            pass

    name = os.environ.get("REPRO_SELECTED_GPU_NAME")
    vram = os.environ.get("REPRO_SELECTED_GPU_VRAM_GB")
    count = os.environ.get("REPRO_GPU_COUNT")
    if name or vram or count:
        try:
            vram_gb = float(vram) if vram else None
        except ValueError:
            vram_gb = None
        gpu_count = int(count or (1 if name or vram_gb else 0))
        gpus = [{"name": name or "selected backend GPU", "vram_gb": vram_gb}] if gpu_count else []
        mode = os.environ.get("REPRO_MODE") or ("multi_gpu" if gpu_count > 1 else "single_gpu" if gpu_count == 1 else "cpu")
        return {"mode": mode, "gpu_count": gpu_count, "gpus": gpus}

    local = _probe_local_gpu_profile()
    if local.get("class") == "cpu" or not local.get("vram_gb"):
        return {"mode": "cpu", "gpu_count": 0, "gpus": []}
    return {"mode": "single_gpu", "gpu_count": 1, "gpus": [{"name": local.get("name"), "vram_gb": local.get("vram_gb")}]}


def evaluate_resource_gate(requirements: dict, backend: dict) -> dict:
    """Compare static requirements with the selected backend and decide proceed/confirm/stop."""
    requirements = requirements or {}
    backend = backend or {}
    gpus = backend.get("gpus") or []
    gpu_count = int(backend.get("gpu_count") or len(gpus) or 0)
    mode = backend.get("mode") or ("multi_gpu" if gpu_count > 1 else "single_gpu" if gpu_count == 1 else "cpu")
    vram_values = []
    for gpu in gpus:
        try:
            if gpu.get("vram_gb") is not None:
                vram_values.append(float(gpu.get("vram_gb")))
        except (TypeError, ValueError):
            continue
    max_single_vram = max(vram_values) if vram_values else 0
    total_vram = sum(vram_values)
    min_vram = float(requirements.get("min_vram_gb") or 0)
    recommended_vram = float(requirements.get("recommended_vram_gb") or min_vram or 0)
    recommended_resource = requirements.get("recommended_resource") or _recommended_resource_for_vram(min_vram, recommended_vram, requirements.get("requires_multi_gpu"))

    selected_resource = {
        "mode": mode,
        "gpu_count": gpu_count,
        "max_single_vram_gb": max_single_vram,
        "total_vram_gb": total_vram,
        "gpus": gpus,
    }

    if requirements.get("confidence") == "unknown":
        return {
            "fit_status": "unknown",
            "action": "confirm",
            "message": "无法静态确认资源需求；请人工确认模型参数量、batch/resolution/seq_len 后再继续。",
            "selected_resource": selected_resource,
            "requirements": requirements,
        }

    if requirements.get("requires_gpu") and (mode == "cpu" or gpu_count == 0):
        return {
            "fit_status": "insufficient",
            "action": "stop",
            "message": f"当前后端为 CPU，但该项目需要 GPU。推荐资源: {recommended_resource}。",
            "selected_resource": selected_resource,
            "requirements": requirements,
        }

    if requirements.get("requires_multi_gpu") and gpu_count < 2:
        return {
            "fit_status": "insufficient",
            "action": "stop",
            "message": f"该项目静态判断需要多卡或总显存约 {min_vram:g}GB；当前仅 {gpu_count} 张 GPU。推荐资源: {recommended_resource}。",
            "selected_resource": selected_resource,
            "requirements": requirements,
        }

    capacity = total_vram if requirements.get("requires_multi_gpu") else max_single_vram
    if min_vram and capacity < min_vram:
        return {
            "fit_status": "insufficient",
            "action": "stop",
            "message": f"当前可用显存约 {capacity:g}GB，低于最低需求 {min_vram:g}GB。推荐资源: {recommended_resource}。",
            "selected_resource": selected_resource,
            "requirements": requirements,
        }

    if recommended_vram and capacity < recommended_vram:
        return {
            "fit_status": "risky",
            "action": "confirm",
            "message": f"当前可用显存约 {capacity:g}GB，达到最低需求但低于推荐显存 {recommended_vram:g}GB；继续可能需要降低 batch/resolution/seq_len。",
            "selected_resource": selected_resource,
            "requirements": requirements,
        }

    return {
        "fit_status": "fit",
        "action": "proceed",
        "message": f"当前后端资源满足静态最低需求；推荐资源: {recommended_resource}。",
        "selected_resource": selected_resource,
        "requirements": requirements,
    }


def append_resource_gate_summary(report: list, scorecard: dict, gate: dict):
    scorecard["resource_gate"] = gate
    requirements = gate.get("requirements") or {}
    report.append("\n🧯 [5b/6] 执行【Resource Gate：资源需求 vs 选定后端】...")
    report.append(f"  fit_status: `{gate['fit_status']}`")
    report.append(f"  action: `{gate['action']}`")
    report.append(f"  recommended_resource: {requirements.get('recommended_resource', 'unknown')}")
    report.append(f"  结论: {gate['message']}")

    report.append("  注：资源条件只限制候选计划，不修改论文/项目可复现性评分。")

def _load_gated_mirrors() -> dict:
    """加载 gated_model_mirrors.yaml，返回 {model_id: {mirrors: [...], status: ...}}"""
    if not os.path.exists(GATED_MODEL_MIRRORS_PATH):
        return {}
    try:
        import yaml
        with open(GATED_MODEL_MIRRORS_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        # 无 yaml 库时用简单正则解析
        result = {}
        current_key = None
        with open(GATED_MODEL_MIRRORS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                if not line or line.startswith("#"):
                    continue
                if not line.startswith(" ") and line.endswith(":"):
                    current_key = line[:-1].strip()
                    result[current_key] = {"mirrors": [], "status": "unknown"}
                elif current_key and "- " in line and "mirrors" not in line:
                    mirror = line.split("- ")[1].split("#")[0].strip()
                    if mirror:
                        result[current_key]["mirrors"].append(mirror)
                elif current_key and "status:" in line:
                    result[current_key]["status"] = line.split("status:")[1].strip()
        return result


def _scan_gated_models(project_dir: str) -> list:
    """
    扫描项目中所有 from_pretrained("xxx") 调用，
    检查 model_id 是否为 gated model，返回发现列表。
    """
    mirrors_db = _load_gated_mirrors()
    pattern = re.compile(r'from_pretrained\s*\(\s*["\']([^"\']+)["\']')
    default_pattern = re.compile(r'default\s*=\s*["\']([a-zA-Z0-9_-]+/[a-zA-Z0-9._-]+)["\']')
    LOCAL_PATH_SUFFIXES = ('.pt', '.pth', '.bin', '.safetensors', '.ckpt', '.index', '.json', '.onnx')

    def _is_hf_model_id(mid: str) -> bool:
        if mid.startswith(('./', '../', '/')):
            return False
        if any(mid.endswith(s) for s in LOCAL_PATH_SUFFIXES):
            return False
        parts = mid.split('/')
        if len(parts) != 2:
            return False
        return all(len(p) > 0 for p in parts)

    findings = []
    seen_ids = set()

    for root, _, files in os.walk(project_dir):
        if ".git" in root:
            continue
        for fname in files:
            if not fname.endswith((".py", ".yaml", ".yml", ".json")):
                continue
            filepath = os.path.join(root, fname)
            if os.path.getsize(filepath) > 2 * 1024 * 1024:
                continue
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
                    for line_num, line in enumerate(fh, 1):
                        for m in list(pattern.finditer(line)) + list(default_pattern.finditer(line)):
                            model_id = m.group(1).strip()
                            if "/" in model_id and model_id not in seen_ids and _is_hf_model_id(model_id):
                                seen_ids.add(model_id)
                                entry = {
                                    "model_id": model_id,
                                    "file": os.path.relpath(filepath, project_dir),
                                    "line": line_num,
                                    "in_mirror_db": model_id in mirrors_db,
                                }
                                if model_id in mirrors_db:
                                    entry["mirrors"] = mirrors_db[model_id].get("mirrors", [])
                                    entry["status"] = mirrors_db[model_id].get("status", "unknown")
                                findings.append(entry)
            except Exception:
                pass

    for item in findings:
        if item["in_mirror_db"]:
            item["gated"] = True
            continue
        try:
            resp = requests.get(
                f"https://huggingface.co/api/models/{item['model_id']}",
                timeout=5, proxies=REQ_PROXIES, headers=REQ_HEADERS
            )
            if resp.status_code == 200:
                data = resp.json()
                gated_val = data.get("gated", False)
                item["gated"] = gated_val not in (False, "false", None)
                item["gated_type"] = str(gated_val)
            elif resp.status_code == 401:
                item["gated"] = True
                item["gated_type"] = "requires_auth"
            elif resp.status_code == 404:
                item["gated"] = None
                item["gated_type"] = "not_found"
            else:
                item["gated"] = None
                item["gated_type"] = f"http_{resp.status_code}"
        except Exception:
            item["gated"] = None
            item["gated_type"] = "timeout"

    return findings


# ==========================================
# ⚙️ 智能项目类别前置断言 (Pre-Assertion)
# ==========================================
def detect_project_type(project_dir: str) -> str:
    """
    前置断言：扫描项目语言分布与核心依赖文件，判断其为 AI/DL 还是通用软件工程项目 (SE)。
    """
    ai_libs = ["torch", "tensorflow", "jax", "scikit-learn", "keras", "transformers", "diffusers", "pinn", "sciml", "deepxde", "modulus", "openfold", "flux", "openfoam", "rheotool", "navier-stokes", "turbulence"]
    ai_score = 0
    
    for root, _, files in os.walk(project_dir):
        if ".git" in root: continue
        for fname in files:
            if re.search(r'(requirements.*\.txt|environment.*\.yml|setup\.py|pyproject\.toml|conda.*\.yml|project\.toml|manifest\.toml|package\.json|pom\.xml|go\.mod|controldict|fvschemes|fvsolution)', fname, re.IGNORECASE):
                filepath = os.path.join(root, fname)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read().lower()
                        for lib in ai_libs:
                            if lib in content:
                                ai_score += 5
                except Exception:
                    pass

    py_files, se_files = 0, 0
    for root, _, files in os.walk(project_dir):
        if ".git" in root: continue
        for fname in files:
            if fname.endswith((".py", ".jl")):
                py_files += 1
            elif fname.endswith((".js", ".ts", ".java", ".go", ".rs", ".cpp", ".html", ".css")):
                se_files += 1

    if py_files > 0:
        ai_score += 10
    if py_files > se_files:
        ai_score += 15

    return "AI" if ai_score >= 15 else "SE"


# ==========================================
# 🧬 物理/流体/多领域 AI 自动分类（带文件权重升级版）
# ==========================================
def detect_project_domain(project_dir: str) -> str:
    """
    智能领域识别器（深度优化版）：
    1. 引入 README (5x)、配置文件 (3x) 与文件路径 (10x) 权重判定机制。
    2. 解决 diffusion, unet 等既是物理流体词、又是 CV 扩散模型骨架的“领域歧义词”分类重映射。
    """
    DOMAIN_SIGNATURES = {
        "🧬 生物计算 (Bio-computing)": [
            "biopython", "alphafold", "esmfold", "openfold", "biotite", "pdb", "fasta", 
            "protein", "rna", "dna", "amino-acid", "structural-biology"
        ],
        "💬 大语言模型 (LLM/NLP)": [
            "transformers", "deepspeed", "megatron", "peft", "tokenizers", "llama", "gpt", 
            "pretrain", "lora", "sft", "autoregressive", "attention_mask"
        ],
        "🌀 科学计算与物理 AI (SciML/CFD/流体力学)": [
            "pinn", "sciml", "cfd", "navier-stokes", "deepxde", "modulus", "fenics", 
            "lattice-boltzmann", "finite-element", "boundary-condition", "grid_size",
            "fluid", "flow", "turbulence", "velocity", "openfoam", "rheotool", "giesekus",
            "weissenberg", "reynolds-stress", "dns", "les", "constitutive", "流体力学", "湍流", "流场"
        ],
        "🖼️ 计算机视觉 (CV)": [
            "diffusers", "torchvision", "timm", "segment-anything", "stable-diffusion",
            "coco-dataset", "image-segmentation", "resnet", "yolov8", "super-resolution",
            "text-to-image", "object-detection"
        ]
    }
    
    # 时空降维与扩散架构特异性歧义词
    ambiguous_keywords = ["unet", "diffusion"]
    ambiguous_scores = 0

    scores = {domain: 0 for domain in DOMAIN_SIGNATURES}
    
    for root, _, files in os.walk(project_dir):
        if ".git" in root: 
            continue
        for file in files:
            filepath = os.path.join(root, file)
            rel_path = os.path.relpath(filepath, project_dir).lower()
            
            # 1. 路径/文件名命中判定 (极高优先级，加权 10x)
            for domain, signatures in DOMAIN_SIGNATURES.items():
                for sig in signatures:
                    if sig in rel_path:
                        scores[domain] += 10
            for amb in ambiguous_keywords:
                if amb in rel_path:
                    ambiguous_scores += 10

            if file.endswith((".py", ".jl", ".cpp", ".c", ".h", ".C", ".H", ".md", ".yaml", ".yml", ".json", ".ipynb")) or file in {"controlDict", "fvSchemes", "fvSolution"}:
                if os.path.getsize(filepath) > 1 * 1024 * 1024: 
                    continue
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read().lower()
                        
                        # 2. 文件级全局比重匹配 (README 5x 权重，YAML 3x 权重，py/ipynb 代码 1x 权重)
                        if file.lower() == "readme.md":
                            weight_multiplier = 5
                        elif file.lower().endswith((".yaml", ".yml")):
                            weight_multiplier = 3
                        else:
                            weight_multiplier = 1
                            
                        # 统计标准特征词
                        for domain, signatures in DOMAIN_SIGNATURES.items():
                            for sig in signatures:
                                count = content.count(sig)
                                if count > 0:
                                    scores[domain] += count * weight_multiplier
                                    
                        # 统计歧义架构词
                        for amb in ambiguous_keywords:
                            count = content.count(amb)
                            if count > 0:
                                ambiguous_scores += count * weight_multiplier
                except Exception:
                    pass
                    
    # 🧩 歧义架构词动态重映射逻辑 (De-ambiguity Resolver)
    sciml_key = "🌀 科学计算与物理 AI (SciML/CFD/流体力学)"
    cv_key = "🖼️ 计算机视觉 (CV)"
    
    if scores[sciml_key] > 0:
        scores[sciml_key] += ambiguous_scores
    else:
        scores[cv_key] += ambiguous_scores

    max_domain = max(scores, key=scores.get)
    if scores[max_domain] > 0:
        return max_domain
    return "⚪ 通用深度学习 (General Deep Learning)"


def evaluate_physics_gate(project_dir: str, scorecard: dict, report: list | None = None) -> dict:
    """Assess whether plan-critical physical configuration has repository evidence.

    Missing fields block only execution candidates that need them. Validation
    evidence remains a separate claim blocker and is never inferred from a
    successful program exit.
    """
    groups = {
        "governing_equations": ("navier-stokes", "giesekus", "constitutive", "momentum equation", "pde"),
        "state_variables": ("velocity", "pressure", "stress", "vorticity", "temperature"),
        "geometry_mesh": ("mesh", "grid", "blockmesh", "geometry", "channel", "contraction"),
        "boundary_conditions": ("boundary condition", "boundaryfield", "inlet", "outlet", "no-slip", "periodic"),
        "material_parameters": ("viscosity", "density", "relaxation time", "weissenberg", "reynolds"),
        "solver_numerics": ("openfoam", "rheotool", "solver", "discret", "timestep", "time step", "cfl"),
        "validation_metrics": ("rmse", "relative error", "profile", "reynolds stress", "tke", "spectrum", "pdf", "correlation"),
    }
    corpus_parts = []
    evidence_files = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [name for name in dirs if name not in {".git", "__pycache__", "node_modules"}]
        for name in files:
            lowered = name.casefold()
            if not (lowered.endswith((".md", ".txt", ".py", ".jl", ".yaml", ".yml", ".json", ".toml", ".c", ".h", ".cpp")) or lowered in {"controldict", "fvschemes", "fvsolution", "transportproperties"}):
                continue
            path = os.path.join(root, name)
            try:
                if os.path.getsize(path) > 2 * 1024 * 1024:
                    continue
                content = open(path, "r", encoding="utf-8", errors="ignore").read().casefold()
            except OSError:
                continue
            corpus_parts.append(content)
            evidence_files.append(os.path.relpath(path, project_dir))
    corpus = "\n".join(corpus_parts)
    fields = {}
    for field, markers in groups.items():
        hits = [marker for marker in markers if marker in corpus]
        fields[field] = {"status": "found" if hits else "missing", "markers": hits[:8]}

    critical = [name for name in ("governing_equations", "state_variables", "geometry_mesh", "boundary_conditions") if fields[name]["status"] == "missing"]
    claim_blockers = [name for name, item in fields.items() if item["status"] == "missing"]
    gate = {
        "fit_status": "incomplete" if critical else "ready_for_plan_selection",
        "action": "block_affected_execution" if critical else "proceed",
        "execution_blockers": critical,
        "claim_blockers": claim_blockers,
        "fields": fields,
        "evidence_files": evidence_files[:30],
        "message": "关键物理配置缺失，阻断依赖这些配置的执行候选。" if critical else "物理配置具备仓库证据；成功结论仍取决于 step_7 验证。",
    }
    scorecard["physics_gate"] = gate
    if "physics_fidelity" in scorecard.get("dimensions", {}):
        if critical:
            add_deduction(scorecard, "physics_fidelity", min(12, len(critical) * 3), "E3", f"缺少关键物理配置证据：{', '.join(critical)}", code="PHYSICS_CONFIG_MISSING")
        if fields["validation_metrics"]["status"] == "missing":
            add_deduction(scorecard, "physics_fidelity", 5, "E2", "未找到数值、统计或物理验证指标定义", code="PHYSICS_VALIDATION_MISSING")
    if report is not None:
        report.append("\n🧭 **Physics Gate**")
        report.append(f"- fit_status: `{gate['fit_status']}`; action: `{gate['action']}`")
        report.append(f"- execution_blockers: {critical or '无'}")
        report.append(f"- claim_blockers: {claim_blockers or '无'}")
    return gate


# ==========================================
# 🔮 本地静态同义词超参/运行耗时估算器 (百分之百离线安全)
# ==========================================
def estimate_general_dl_resources(project_dir: str, detected_domain: str) -> list:
    """
    通用深度学习项目算力与时间静态估算器。
    通过同义词字典扫描所有配置，计算复杂度并给出通用资源与时间预测。
    """
    import yaml
    import json
    
    BATCH_SYNONYMS = ["batch_size", "batch", "train_batch_size", "test_batch_size", "micro_batch_size"]
    SPATIAL_SYNONYMS = ["image_size", "resolution", "grid_size", "nx", "ny", "nz", "latent_length"]
    SEQUENCE_SYNONYMS = ["seq_len", "sequence_length", "max_len", "time_length", "nt"]
    MODEL_SYNONYMS = ["hidden_size", "hidden_dim", "d_model", "num_layers", "num_res_blocks", "depth"]
    ITER_SYNONYMS = ["epochs", "num_epochs", "train_steps", "max_steps", "steps", "n_epochs"]

    def _deep_search_synonyms(d, synonyms):
        for s in synonyms:
            if s in d: return d[s]
        for k, v in d.items():
            if isinstance(v, dict):
                res = _deep_search_synonyms(v, synonyms)
                if res is not None: return res
        return None

    analyzed_configs = []
    
    for root, _, files in os.walk(project_dir):
        if ".git" in root: continue
        for fname in files:
            if fname.lower().endswith((".yaml", ".yml", ".json")):
                filepath = os.path.join(root, fname)
                try:
                    data = {}
                    if fname.lower().endswith((".yaml", ".yml")):
                        with open(filepath, "r", encoding="utf-8") as f:
                            data = yaml.safe_load(f) or {}
                    elif fname.lower().endswith(".json"):
                        with open(filepath, "r", encoding="utf-8") as f:
                            data = json.load(f) or {}
                    
                    b_size = _deep_search_synonyms(data, BATCH_SYNONYMS) or 2
                    s_size = _deep_search_synonyms(data, SPATIAL_SYNONYMS) or 1
                    t_size = _deep_search_synonyms(data, SEQUENCE_SYNONYMS) or 1
                    m_size = _deep_search_synonyms(data, MODEL_SYNONYMS) or 128
                    steps = _deep_search_synonyms(data, ITER_SYNONYMS) or 1
                    
                    if b_size and (s_size > 1 or t_size > 1 or m_size > 128):
                        ci = b_size * t_size * (s_size ** 2) * m_size
                        
                        domain_tips = ""
                        if "生物" in detected_domain:
                            domain_tips = " **生物计算提示**：通常需要大存储挂载（~1TB+）装载 PDB 等大库。"
                        elif "大语言" in detected_domain:
                            domain_tips = " **大模型提示**：高度依赖 NVLink 带宽，强推 H100 双卡并配置 DeepSpeed ZeRO 阶段 3。"
                        elif "科学计算" in detected_domain:
                            domain_tips = " **流体力学/物理AI提示**：网格分辨率敏感 [2]，高维张量求导易爆显存，注意控制 batch_size。"
                        
                        config_info = {
                            "file_name": fname,
                            "path": os.path.relpath(filepath, project_dir),
                            "batch_size": b_size,
                            "spatial_size": s_size,
                            "seq_len": t_size,
                            "model_dim": m_size,
                            "steps": steps,
                            "domain_tips": domain_tips
                        }
                        
                        if ci >= 100000000 or (s_size >= 384 and t_size >= 384):
                            config_info["load_level"] = "extreme"
                            config_info["min_vram_gb"] = 48
                            config_info["recommended_vram_gb"] = 80
                            config_info["requires_multi_gpu"] = False
                            config_info["est_resource"] = f"🔴 **极高负载 (大规模场景)**: 强推 NVIDIA H100/A100 (80GB 显存)。{domain_tips}"
                            config_info["est_runtime_inf"] = "⏱️ **推理耗时 (4 样本)**: 约 **8 ~ 12 分钟** (1x H100) / 约 **15 ~ 25 分钟** (1x RTX 4090) [3]"
                            config_info["est_runtime_train"] = "⏳ **全量训练耗时 (100k Steps)**: 约 **18 ~ 30 小时** (单卡 H100)。建议采用多卡并行分布式训练。"
                        elif ci >= 10000000 or s_size >= 256 or t_size >= 256:
                            config_info["load_level"] = "medium"
                            config_info["min_vram_gb"] = 24
                            config_info["recommended_vram_gb"] = 48
                            config_info["requires_multi_gpu"] = False
                            config_info["est_resource"] = f"🟡 **中等负载 (常规 3D 模拟/大模型微调)**: 建议 RTX 3090/4090 或 A100 (显存 >= 24GB)。{domain_tips}"
                            config_info["est_runtime_inf"] = "⏱️ **推理耗时 (4 样本)**: 约 **3 ~ 5 分钟** (1x H100) / 约 **6 ~ 10 分钟** (1x RTX 4090)"
                            config_info["est_runtime_train"] = "⏳ **全量训练耗时 (100k Steps)**: 约 **8 ~ 14 小时** (单卡 H100) / 约 **16 ~ 26 小时** (单卡 RTX 4090)。"
                        else:
                            config_info["load_level"] = "light"
                            config_info["min_vram_gb"] = 16
                            config_info["recommended_vram_gb"] = 24
                            config_info["requires_multi_gpu"] = False
                            config_info["est_resource"] = f"🟢 **轻量负载 (小模型/2D 场景)**: 单卡消费级显力（RTX 3090/4090，显存 >= 16GB）即可流畅跑通。{domain_tips}"
                            config_info["est_runtime_inf"] = "⏱️ **推理耗时 (4 样本)**: 约 **1 ~ 1.5 分钟** (1x H100) / 约 **2 ~ 3 分钟** (1x RTX 4090)"
                            config_info["est_runtime_train"] = "⏳ **全量训练耗时 (100k Steps)**: 约 **2 ~ 4 小时** (单卡 H100) / 约 **5 ~ 8 小时** (单卡 RTX 4090)。"
                            
                        analyzed_configs.append(config_info)
                except Exception:
                    pass
                    
    return analyzed_configs


# ==========================================
# 🔐 高危密钥泄露扫描正则集 (用于通用 SE 审计)
# ==========================================
SECRET_PATTERNS = {
    "AWS Access Key": re.compile(r'(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}'),
    "OpenAI API Key": re.compile(r'sk-[a-zA-Z0-9]{20,48}'),
    "Generic Private Key": re.compile(r'-----BEGIN [A-Z ]+ PRIVATE KEY-----'),
    "Hardcoded Password": re.compile(r'(?:api_key|client_secret|db_password|database_pass|aws_secret_access_key)\s*=\s*[\'"][a-zA-Z0-9_+=/-]{16,64}[\'"]')
}


# ==========================================
# 🌀 SciML / 计算物理 / 流变学审计
# ==========================================
def evaluate_security_gate(project_dir: str, scorecard: dict, report: list) -> None:
    """Keep credential leakage as a universal red line without an SE scorecard."""
    leaked_secrets = []
    for root, _, files in os.walk(project_dir):
        if ".git" in root:
            continue
        for name in files:
            if not name.endswith((".py", ".jl", ".js", ".ts", ".cpp", ".json", ".yaml", ".yml", ".toml", ".env", ".conf")):
                continue
            path = os.path.join(root, name)
            if os.path.getsize(path) > 1 * 1024 * 1024:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    for line_num, line in enumerate(handle, 1):
                        for secret_name, pattern in SECRET_PATTERNS.items():
                            if pattern.search(line):
                                leaked_secrets.append(f"{os.path.relpath(path, project_dir)}:{line_num} ({secret_name})")
            except OSError:
                continue
    if leaked_secrets:
        add_deduction(
            scorecard,
            "risk_gate",
            10,
            "E4",
            "疑似硬编码密钥、私钥或密码，安全红线",
            code="SECRET_LEAK",
            file=", ".join(leaked_secrets[:3]),
            blocker=True,
        )
        report.append(f"\n🔴 安全红线：发现疑似凭据泄漏证据：{', '.join(leaked_secrets[:3])}")
    else:
        report.append("\n✅ 安全门禁：未检测到硬编码密钥、私钥或密码。")


def audit_sciml_physics_mode(temp_dir: str, report: list, detected_domain: str, scorecard: dict = None) -> None:
    """唯一审计模式：检查物理计算链、环境、资产和实验闭环。"""
    if scorecard:
        scorecard["project_mode"] = "SCIML_PHYSICS"
        scorecard["detected_domain"] = detected_domain
    report.append("\n🌀 **进入 [SciML / 计算物理 / 流变学审计模式]**")
    
    # 👈 优化点：完全转为百分之百稳定、免 API 的离线超参与运行时评估
    report.append("\n🕵️ [1/6] 静态核算物理复现点及显存/耗时画像...")
    configs_evaluated = estimate_general_dl_resources(temp_dir, detected_domain)
    physics_case_files = []
    for root, _, files in os.walk(temp_dir):
        for name in files:
            if name.casefold() in {"controldict", "fvschemes", "fvsolution", "project.toml", "manifest.toml"}:
                physics_case_files.append(os.path.join(root, name))
    if not configs_evaluated and not physics_case_files:
        add_deduction(scorecard, "experiment_reproduction", 5, "E2", "未检测到 YAML/JSON/TOML 实验配置或 OpenFOAM case 配置，复现实验规格不完整", code="NO_CONFIG_CASES")
        report.append("  ⚠️ 未检测到结构化实验配置或 OpenFOAM case 配置。")
    elif physics_case_files:
        report.append(f"  ✅ 检测到 {len(physics_case_files)} 个 Julia/OpenFOAM 物理执行配置文件。")
    else:
        report.append("  📊 模型复现点（Cases）及硬件画像估算结果：")
        for item in configs_evaluated:
            report.append(f"    - **复现点配置文件: `{item['path']}`**")
            report.append(f"      * 时空特征: `batch_size: {item['batch_size']}`, `spatial_size: {item['spatial_size']}`, `seq_len: {item['seq_len']}`, `model_dim: {item['model_dim']}`")
            report.append(f"      * 硬件要求: {item['est_resource']}")
            report.append(f"      * 推理时效: {item['est_runtime_inf']}")
            report.append(f"      * 训练时效: {item['est_runtime_train']}")

    report.append("\n📦 [2/6] 检查【依赖锚定】与 H100 硬件级兼容排雷...")
    has_old_torch = False
    has_old_python = False
    dep_files = []
    python_dep_files = []
    for root, _, files in os.walk(temp_dir):
        if ".git" in root:
            continue
        for fname in files:
            if re.search(r'(requirements.*\.txt|environment.*\.yml|setup\.py|pyproject\.toml|conda.*\.yml)', fname, re.IGNORECASE):
                path = os.path.join(root, fname)
                dep_files.append(path)
                python_dep_files.append(path)
            elif fname.casefold() in {"project.toml", "manifest.toml", "dockerfile", "cmakelists.txt", "spack.yaml", "controldict", "fvschemes", "fvsolution"}:
                dep_files.append(os.path.join(root, fname))

    if not dep_files:
        add_deduction(scorecard, "environment", 14, "E3", "未找到 Python、Julia、OpenFOAM、CMake 或容器环境锚点", code="NO_DEP_FILE")
        report.append("  ❌ 未找到任何受支持运行时的环境或求解器配置文件。")
    elif python_dep_files:
        best_dep = python_dep_files[0]
        dep_filepath = best_dep
        try:
            with open(dep_filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                lines = [l.strip() for l in content.split('\n') if l.strip() and not l.startswith('#')]
                locked = sum(1 for l in lines if "==" in l or ">=" in l or "~=" in l)
                
                if any(re.search(r"torch(==|<=|<)1\.", l) for l in lines):
                    has_old_torch = True
                    
                py_match = re.search(r'python\s*(?:==|<=|=|\s*:)\s*3\.[0-8]\b', content)
                if py_match:
                    has_old_python = True

                if lines and locked == 0:
                    add_deduction(scorecard, "environment", 4, "E2", "依赖未锁版本，存在版本漂移风险", code="UNPINNED_DEPS", file=os.path.relpath(best_dep, temp_dir))
                    report.append("  ⚠️ Python依赖未锁定版本，存在版本漂移风险。")
                else:
                    report.append(f"  ✅ 依赖锁死度良好 ({locked}/{len(lines)})。")
        except Exception:
            pass
                
    if has_old_torch:
        add_deduction(scorecard, "environment", 8, "E3", "检测到 PyTorch 1.x 约束，现代 CUDA/GPU 环境兼容风险高", code="OLD_TORCH")
        report.append("  ❌ 项目约束PyTorch 1.x，现代CUDA/GPU环境兼容风险较高。")
        
    if has_old_python:
        add_deduction(scorecard, "environment", 5, "E3", "检测到 Python <= 3.8 约束，现代深度学习依赖 wheel 可得性较差", code="OLD_PYTHON")
        report.append("  ❌ 检测到Python 3.8或更早版本约束，现代依赖wheel可得性较差。")

    report.append("\n🖥️ [3/6] 检查【代码规范】与 Hopper 架构适配性...")
    hardcoded_paths = []
    has_seed_control = False
    has_stochastic_code = False
    has_apex = False
    has_bf16 = False
    has_flash_attn = False
    has_sm90 = False
    
    path_regex = re.compile(
        r'(?:["\']|\s|:|=|^)(/home/[a-zA-Z0-9_-]+/[^\s"\'#\n\r]+)'
        r'|(?:["\']|\s|:|=|^)([a-zA-Z]:\\Users\\[a-zA-Z0-9_-]+\\[^\s"\'#\n\r]+)'
    )
    seed_regex = re.compile(r'(random\.seed|np\.random\.seed|torch\.manual_seed|cudnn\.deterministic)')
    
    for root, _, files in os.walk(temp_dir):
        if ".git" in root: continue
        for file in files:
            if file.endswith((".py", ".sh", ".yaml", ".yml", ".cpp", ".cu", ".json")):
                filepath = os.path.join(root, file)
                if os.path.getsize(filepath) > 2 * 1024 * 1024: continue 
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, 1):
                            if path_regex.search(line): 
                                hardcoded_paths.append(f"`{file}` (行 {line_num})")
                            if seed_regex.search(line): 
                                has_seed_control = True
                            if re.search(r'\b(random|randn|dropout|diffusion|sample)\b', line, re.IGNORECASE):
                                has_stochastic_code = True
                            if "apex.amp" in line or "from apex " in line: 
                                has_apex = True
                            if "bfloat16" in line or "bf16" in line: 
                                has_bf16 = True
                            if "flash_attn" in line or "FlashAttention" in line: 
                                has_flash_attn = True
                            if "sm_90" in line or "compute_90" in line: 
                                has_sm90 = True
                except Exception:
                    pass
    
    if hardcoded_paths:
        add_deduction(scorecard, "migration_cost", 6, "E3", "检测到个人绝对路径硬编码", code="HARDCODED_PATH", file=", ".join(hardcoded_paths[:3]))
        report.append(f"  ❌ 写死了个人绝对路径：{', '.join(hardcoded_paths[:3])}...")
    else:
        report.append("  ✅ 代码整洁，未发现明显的个人电脑绝对路径。")
        
    if has_stochastic_code and not has_seed_control:
        add_deduction(scorecard, "experiment_reproduction", 3, "E2", "未扫描到固定随机种子操作，指标复现稳定性较弱", code="NO_SEED")
        report.append("  ⚠️ 检测到随机计算，但未扫描到固定随机种子操作。")
    elif has_seed_control:
        report.append("  ✅ 随机性控制：代码包含随机种子锁定，实验具备确定性基础。")
    else:
        report.append("  ℹ️ 未发现随机计算证据，随机种子项不适用且不扣分。")

    if has_apex:
        add_deduction(scorecard, "environment", 6, "E3", "检测到已弃用 NVIDIA Apex，现代 CUDA 环境编译风险高", code="APEX_RISK")
        report.append("  ❌ 检测到已弃用的NVIDIA Apex，现代CUDA环境编译风险较高。")
        
    if has_sm90: report.append("  🌟 架构前瞻：代码已针对 sm_90 (Hopper架构) 进行底层优化。")
    if has_bf16: report.append("  🌟 精度优化：支持 BF16 (bfloat16) 精度，完美契合 H100 第四代 Tensor Core。")
    if has_flash_attn: report.append("  🌟 算力解放：支持 FlashAttention，可充分榨干 H100 超高显存带宽！")

    evaluate_hardware_migration(temp_dir, report, scorecard)

    report.append("\n📖 [4/6] 检查 README 执行闭环...")
    readme_path = os.path.join(temp_dir, "README.md")
    if not os.path.exists(readme_path):
        add_deduction(scorecard, "documentation_entry", 15, "E3", "完全没有 README.md，训练/推理/评估入口不可确认", code="NO_README")
        report.append("  ❌ 完全没有README，物理计算链入口不可确认。")
    else:
        with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
            content_lower = f.read().lower()
            has_train = any(token in content_lower for token in ("train", "run ", "python main.py", "julia ", "foamrun", "blockmesh", "solver", "simulate"))
            has_eval = any(token in content_lower for token in ("eval", "test", "inference", "validation", "validate", "rmse", "profile", "spectrum", "error"))
            
            if not has_train:
                add_deduction(scorecard, "documentation_entry", 6, "E2", "README 缺少明确训练/运行入口", code="README_NO_TRAIN", file="README.md")
                report.append("  ⚠️ README缺少明确的训练、推理或数值模拟入口。")
            if not has_eval:
                add_deduction(scorecard, "experiment_reproduction", 6, "E2", "README 缺少 eval/test/inference 评估入口", code="README_NO_EVAL", file="README.md")
                report.append("  ⚠️ README缺少数值、统计或物理验证入口。")
            if has_train and has_eval:
                report.append("  ✅ 执行说明闭环：README 完整覆盖了训练与评估说明。")

    report.append("\n🔒 [5/6] 检查 Gated Model 风险...")
    gated_findings = _scan_gated_models(temp_dir)
    if not gated_findings:
        report.append("  ✅ 未发现 from_pretrained() 调用引用外部 HuggingFace 模型，无 Gated 风险。")
    else:
        gated_blocked = [item for item in gated_findings if item.get("gated") is True]
        gated_ok = [item for item in gated_findings if item.get("gated") is False]
        if gated_ok:
            report.append(f"  ✅ 公开可访问模型 ({len(gated_ok)}):")
            for item in gated_ok:
                report.append(f"    - `{item['model_id']}` ({item['file']}:{item['line']})")
        if gated_blocked:
            deduct = min(len(gated_blocked) * 10, 20)
            add_deduction(scorecard, "artifacts", min(deduct, 12), "E3", f"发现 {len(gated_blocked)} 个 Gated/受限模型引用", code="GATED_MODEL")
            report.append(f"  ❌ 发现 Gated/受限模型 ({len(gated_blocked)}):")
            for item in gated_blocked:
                mirrors = item.get("mirrors", [])
                if mirrors:
                    report.append(f"    - `{item['model_id']}` ({item['file']}:{item['line']}) → 🔄 可用镜像: {', '.join(mirrors)}")
                else:
                    report.append(f"    - `{item['model_id']}` ({item['file']}:{item['line']}) → ⚠️ 无已知镜像，需 HF Token")

    requirements = infer_resource_requirements(
        analyzed_configs=configs_evaluated,
        model_findings=gated_findings,
        detected_domain=detected_domain,
    )
    gate = evaluate_resource_gate(requirements, _backend_from_env_or_local())
    append_resource_gate_summary(report, scorecard, gate)

    return None


# ==========================================
# 📊 核心调度入口 (audit_repo)
# ==========================================
def audit_repo(repo_url: str) -> str:
    """使用唯一 SciML/计算物理量表审计仓库并输出确定性评分。"""
    global LAST_AUDIT_SCORECARD
    if PROXY_URL:
        os.environ["http_proxy"] = PROXY_URL
        os.environ["https_proxy"] = PROXY_URL
    report = [f"🔍 **开始对仓库 {repo_url} 进行 [多维智能静态审计]...**\n"]
    scorecard = new_scorecard(repo_url)
    LAST_AUDIT_SCORECARD = scorecard
    temp_dir = tempfile.mkdtemp()
    
    try:
        # 步骤 1: 拉取
        report.append("📥 [1/6] 正在拉取代码库及子模块...")
        try:
            subprocess.run(["git", "clone", "--depth", "1", "--recurse-submodules", repo_url, temp_dir],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60
            )
        except Exception as e:
            add_deduction(scorecard, "documentation_entry", 20, "E4", f"仓库无法 clone：{str(e)[:160]}", code="CLONE_FAILED", blocker=True)
            finalize_scorecard(scorecard)
            scorecard["verdict"] = "UNSCORABLE"
            append_scorecard_summary(report, scorecard)
            return "\n".join(report)

        # 步骤 2: 领域识别仅用于报告提示，不再选择评分量表。
        report.append("\n⚙️ [2/6] 识别物理子领域（不影响评分量表）...")
        project_mode = detect_project_type(temp_dir)
        detected_domain = detect_project_domain(temp_dir)
        configure_scorecard_profile(scorecard)
        report.append(f"  ℹ️ 原始代码形态识别：**{project_mode}**（仅作提示）")
        report.append(f"  🎯 物理子领域提示：**{detected_domain}**")
        scorecard["detected_domain"] = detected_domain
        audit_sciml_physics_mode(temp_dir, report, detected_domain, scorecard)
        evaluate_physics_gate(temp_dir, scorecard, report)
        evaluate_security_gate(temp_dir, scorecard, report)

        # 步骤 3: 连通性与死链检测 (通用模块)
        report.append("\n🔗 [6/6] 检查外部数据链接死链...")
        readme_path = os.path.join(temp_dir, "README.md")
        if os.path.exists(readme_path):
            with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                urls = list(set(re.findall(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[^\s"\'\])]*', content)))
                dead_links = []
                trusted_domains = ["zenodo.org", "huggingface.co", "drive.google.com", "github.com", "arxiv.org", "doi.org"]
                
                for url in urls[:8]: 
                    if any(domain in url for domain in ["github.com", "arxiv.org", "doi.org"]): continue
                    try:
                        resp = requests.head(url, timeout=3, allow_redirects=True, proxies=REQ_PROXIES, headers=REQ_HEADERS)
                        if resp.status_code in [403, 405]:
                            resp = requests.get(url, timeout=3, stream=True, proxies=REQ_PROXIES, headers=REQ_HEADERS)
                        
                        is_trusted_restricted = resp.status_code in [401, 403] and any(dom in url for dom in trusted_domains)
                        
                        if resp.status_code >= 400 and not is_trusted_restricted: 
                            dead_links.append(f"{url} (HTTP {resp.status_code})")
                        elif is_trusted_restricted:
                            report.append(f"  ⚠️ 提示：受托平台链接返回 {resp.status_code} ({url[:30]}...)，通常为防火墙反爬拦截，实际环境可访问（不扣分）。")
                    except Exception:
                        dead_links.append(f"{url} (连接失败)")
                
                if dead_links:
                    deduct = min(len(dead_links) * 10, 30)
                    add_deduction(scorecard, "artifacts", min(deduct, 15), "E3", f"README 外部数据/权重链接失效或连接失败：{', '.join(dead_links[:3])}", code="DEAD_LINKS", file="README.md")
                    report.append(f"  ❌ 严重：发现数据/权重外链失效 (-{deduct}分): {', '.join(dead_links)}")
                else:
                    report.append("  ✅ 数据外链完整：文档中的外部链接检测连通良好。")

        # ---------------------------------------------------------
        # 📊 输出综合打分评估结论
        # ---------------------------------------------------------
        finalize_scorecard(scorecard)
        append_scorecard_summary(report, scorecard)
        report.append(f"\n📊 **SciML/计算物理 v4 确定性总分：{scorecard['overall_score']} / 100**")
        if scorecard["verdict"] == "PASS":
            report.append("🟢 **结论：PASS。静态证据显示复现条件较完整，可进入环境准备。**")
        elif scorecard["verdict"] == "CONDITIONAL":
            report.append("🟡 **结论：CONDITIONAL。具备复现条件，但需先按多维扣分项处理风险。**")
        elif scorecard["verdict"] == "BLOCKED":
            report.append("🔴 **结论：命中红线阻断项，建议先解决 blocker 再投入复现。**")
        else:
            report.append("🟠 **结论：高风险项目，建议人工复核入口、环境、数据权重后再决定是否继续。**")

        return "\n".join(report)
        
    finally:
        # 清理内存现场
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AR24 step_1 repository audit")
    parser.add_argument("repo_url")
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID"))
    args = parser.parse_args()
    repo_url = args.repo_url

    repo_name = repo_url.rstrip("/").split("/")[-1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]

    workspace_root = resolve_workspace_root()
    run_id = resolve_run_id(args.run_id)
    output_dir = os.path.join(workspace_root, run_id, "step_1")
    os.makedirs(output_dir, exist_ok=True)

    # 执行多模审计
    report_text = audit_repo(repo_url)

    # 打印到控制台
    print(report_text)

    # 输出 Markdown 报告归档
    report_path = os.path.join(output_dir, f"{repo_name}_Audit_Report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"\n📁 报告已成功输出至: {report_path}")

    # 输出机器可复核的确定性多维评分。大模型报告只能解释该文件，不应自由改分。
    if LAST_AUDIT_SCORECARD:
        scorecard_path = os.path.join(output_dir, "audit_score.json")
        with open(scorecard_path, "w", encoding="utf-8") as f:
            json.dump(LAST_AUDIT_SCORECARD, f, ensure_ascii=False, indent=2)
        print(f"📊 多维评分 JSON 已成功输出至: {scorecard_path}")
