#!/usr/bin/env python3
"""
analyze_paper.py — 论文解析重器 v2.0 (结构化提取版)
===================================================
职责边界: 脚本做结构化提取 + Agent 做语义推理 = 全自动分析

v2.0 新增:
- PDF 元数据自动提取 (标题/作者/DOI)
- 智能章节分段 (Abstract/Introduction/Methods/Results/Conclusion)
- 公式/表格页面图像导出 (供 vision model 分析)
- 超参模式匹配 (lr/batch_size/epochs/optimizer 等)
- 评估指标模式匹配 (PSNR/SSIM/LPIPS/FID/BLEU/雷诺应力等)
- 领域关键词自动检测与分类建议
- 结构化 JSON 元数据输出 (paper_meta.json)
- 实验结果图候选检测、裁剪与严格绘图规范骨架
"""

import argparse
import json
import os
import re
import sys
import subprocess
import urllib.request
from pathlib import Path
from collections import OrderedDict

PHYSICS_VALIDATION_SCRIPTS = Path(__file__).resolve().parents[2] / "ar24-physics-validation" / "scripts"
if str(PHYSICS_VALIDATION_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PHYSICS_VALIDATION_SCRIPTS))

from scientific_contract import build_contract

def _writable(path: str) -> bool:
    """目录可写判定：存在且可写，或其最近的已存在父级可写。"""
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return bool(p) and os.access(p, os.W_OK)


def _resolve_output_root() -> str:
    """
    Paper analysis output root must be explicitly selected by the user.
    """
    env = (os.environ.get("REPRO_OUTPUT_ROOT") or os.environ.get("WORKSPACE_ROOT") or "").strip()
    if env:
        return env
    raise RuntimeError(
        "REPRO_OUTPUT_ROOT or WORKSPACE_ROOT is required before paper analysis. "
        "Complete the path-confirmation step and export REPRO_OUTPUT_ROOT=<repro_root>."
    )


# 产物输出根：由路径确认步骤显式设置，不做自动兜底。
FIXED_OUTPUT_ROOT = _resolve_output_root()


def _resolve_step_1_output() -> Path:
    run_root = str(os.environ.get("RUN_ROOT") or "").strip()
    run_id = str(os.environ.get("RUN_ID") or "").strip()
    if run_root:
        return Path(run_root) / "step_1"
    if not re.fullmatch(r"run-\d{3,}", run_id):
        raise RuntimeError("RUN_ID or RUN_ROOT is required for paper analysis")
    return Path(FIXED_OUTPUT_ROOT) / run_id / "step_1"

# ── 代理配置（本地化：默认直连，仅当配置了 REPRO_HTTP_PROXY 时启用）──
_proxy = os.environ.get("REPRO_HTTP_PROXY", "")
if _proxy:
    os.environ.setdefault("http_proxy", _proxy)
    os.environ.setdefault("https_proxy", _proxy)

# ── 领域关键词库 ──────────────────────────────────────────
DOMAIN_KEYWORDS = {
    "🧬 生物计算 (Bio)": {
        "keywords": [
            "protein", "genomic", "DNA", "RNA", "amino acid", "antibody",
            "molecule", "ligand", "docking", "PDB", "AlphaFold", "ESM",
            "sequence", "peptide", "enzyme", "CRISPR", "bacteria", "immunity",
            "metagenomic", "genome", "proteome", "homology", "structural biology",
            "TM-score", "lDDT", "RMSD", "binding affinity", "drug discovery"
        ],
        "threshold": 4
    },
    "💬 大语言模型 (LLM/NLP)": {
        "keywords": [
            "transformer", "attention", "LLM", "language model", "GPT",
            "pre-train", "fine-tune", "RLHF", "DPO", "SFT", "tokenizer",
            "PPL", "perplexity", "BLEU", "ROUGE", "MMLU", "GSM8K",
            "instruction tuning", "alignment", "chatbot", "prompt",
            "decoder", "encoder", "self-attention", "causal LM"
        ],
        "threshold": 4
    },
    "🌀 科学计算与物理AI (SciML/CFD)": {
        "keywords": [
            "turbulence", "fluid", "CFD", "Navier-Stokes", "Reynolds",
            "vortex", "flow field", "velocity", "vorticity", "channel flow",
            "pipe flow", "boundary layer", "DNS", "LES", "RANS",
            "finite element", "spectral method", "PDE", "computational fluid",
            "aerodynamic", "hydrodynamic", "convection", "diffusion",
            "energy spectrum", "enstrophy", "pressure", "stream function",
            "neural operator", "physics-informed", "PINN", "FNO",
            "spatiotemporal", "latent diffusion", "neural field", "CoNFiLD"
        ],
        "threshold": 4
    },
    "🖼️ 计算机视觉 (CV)": {
        "keywords": [
            "image", "video", "object detection", "segmentation", "classification",
            "CNN", "ResNet", "ViT", "diffusion model", "GAN", "VAE",
            "PSNR", "SSIM", "LPIPS", "FID", "mAP", "IoU", "3D reconstruction",
            "NeRF", "point cloud", "depth estimation", "pose estimation",
            "optical flow", "super-resolution", "inpainting", "style transfer"
        ],
        "threshold": 4
    }
}

# ── 超参模式库 ────────────────────────────────────────────
HP_PATTERNS = [
    # 学习率 (多种表述)
    (r"(?:learning[\s_-]?rate|LR|lr)\s*[:=＝]\s*([\d.eE\-\+×x]+)", "学习率 (Learning Rate)"),
    (r"(?:learning[\s_-]?rate|LR|lr)\s*(?:of|is|was|set\s*(?:to|as))\s*([\d.eE\-\+×x]+)", "学习率 (Learning Rate)"),
    # 批次大小
    (r"(?:batch[\s_-]?size|mini[\s_-]?batch|B)\s*[:=＝]\s*(\d+)", "批次大小 (Batch Size)"),
    (r"(?:batch[\s_-]?size|mini[\s_-]?batch)\s*(?:of|is|was|set\s*(?:to|as))\s*(\d+)", "批次大小 (Batch Size)"),
    # 训练轮数
    (r"(?:epochs?|training[\s_-]?epochs?)\s*[:=＝]\s*(\d+)", "训练轮数 (Epochs)"),
    (r"(?:epochs?)\s*(?:of|is|was)\s*(\d+)", "训练轮数 (Epochs)"),
    (r"(?:train(?:ed|ing)?\s*(?:for)?)\s*(\d+)\s*epochs?", "训练轮数 (Epochs)"),
    # 迭代步数
    (r"(?:iterations?|steps?|training[\s_-]?steps?)\s*[:=＝]\s*([\d,]+)", "迭代步数 (Steps/Iterations)"),
    # 优化器
    (r"(?:optimizer|optim)\s*[:=＝]\s*(AdamW?|SGD|RMSprop|Ada(?:grad|delta)|LAMB|Lion)", "优化器 (Optimizer)"),
    (r"(?:use[sd]?\s*(?:the\s+)?)(AdamW?|SGD|RMSprop)\s*(?:optimizer|optim)", "优化器 (Optimizer)"),
    # 权重衰减
    (r"(?:weight[\s_-]?decay|L2[\s_-]?reg(?:ularization)?)\s*[:=＝]\s*([\d.eE\-\+×x]+)", "权重衰减 (Weight Decay)"),
    # Dropout
    (r"(?:dropout[\s_-]?rate|dropout)\s*[:=＝]\s*([\d.]+)", "Dropout"),
    # 学习率调度
    (r"(?:lr[\s_-]?schedul(?:e|er)|learning[\s_-]?rate[\s_-]?schedul(?:e|er))\s*[:=＝]\s*(CosineAnnealing|StepLR|ReduceLROnPlateau|cosine|linear|exponential)", "学习率调度 (LR Scheduler)"),
    (r"(?:warm(?:up|ing)[\s_-]?steps?)\s*[:=＝]\s*(\d+)", "预热步数 (Warmup Steps)"),
    # 梯度裁剪
    (r"(?:gradient[\s_-]?clip(?:ping)?|grad[\s_-]?clip)\s*[:=＝]\s*([\d.]+)", "梯度裁剪 (Gradient Clipping)"),
    # 混合精度
    (r"(?:mixed[\s_-]?precision|AMP|fp16|bfloat16|bf16)", "混合精度 (Mixed Precision)"),
    # 分布式训练
    (r"(\d+)\s*(?:GPU|gpu)s?", "GPU 数量"),
    # 潜在维度 (latent dimension, 对 diffusion/VAE 很重要)
    (r"(?:latent[\s_-]?dim(?:ension)?|latent[\s_-]?size)\s*[:=＝]\s*(\d+)", "潜在维度 (Latent Dim)"),
    # 扩散步数
    (r"(?:diffusion[\s_-]?steps?|denoising[\s_-]?steps?|T)\s*[:=＝]\s*(\d+)", "扩散/去噪步数 (Diffusion Steps)"),
    # β 调度
    (r"(?:beta[\s_-]?schedul(?:e|er)|noise[\s_-]?schedul(?:e|er))\s*[:=＝]\s*(\w+)", "噪声调度 (Noise/Beta Schedule)"),
    # 分辨率
    (r"(?:resolution|image[\s_-]?size|spatial[\s_-]?res(?:olution)?)\s*[:=＝]\s*(\d+×?\d*×?\d*)", "分辨率 (Resolution)"),
    # 随机种子
    (r"(?:random[\s_-]?seed|seed)\s*[:=＝]\s*(\d+)", "随机种子 (Random Seed)"),
]

# ── 指标模式库 ────────────────────────────────────────────
METRIC_PATTERNS = [
    # CV 指标
    (r"PSNR\s*[:=＝]?\s*([\d.]+)\s*(?:dB)?", "PSNR (dB)", "🖼️ CV"),
    (r"SSIM\s*[:=＝]?\s*([\d.]+)", "SSIM", "🖼️ CV"),
    (r"LPIPS\s*[:=＝]?\s*([\d.]+)", "LPIPS", "🖼️ CV"),
    (r"FID\s*[:=＝]?\s*([\d.]+)", "FID", "🖼️ CV"),
    # NLP 指标
    (r"BLEU\s*[:=＝]?\s*([\d.]+)", "BLEU", "💬 LLM/NLP"),
    (r"ROUGE[-_]?L\s*[:=＝]?\s*([\d.]+)", "ROUGE-L", "💬 LLM/NLP"),
    (r"PPL|perplexity\s*[:=＝]?\s*([\d.]+)", "Perplexity", "💬 LLM/NLP"),
    # CFD/物理指标
    (r"(?:Reynolds[\s_-]?stress|Reynolds[\s_-]?shear)\s*[:=＝]?\s*([\d.]+)", "雷诺应力 (Reynolds Stress)", "🌀 CFD"),
    (r"(?:turbulence[\s_-]?intensity|turbulent[\s_-]?kinetic[\s_-]?energy|TKE)\s*[:=＝]?\s*([\d.]+)", "湍动能 (TKE)", "🌀 CFD"),
    (r"(?:energy[\s_-]?spectrum|power[\s_-]?spectrum)", "能量谱 (Energy Spectrum)", "🌀 CFD"),
    (r"(?:friction[\s_-]?velocity|u_tau|uτ)\s*[:=＝]?\s*([\d.]+)", "摩擦速度 (u_τ)", "🌀 CFD"),
    (r"Reτ\s*[:=＝]?\s*([\d.]+)", "摩擦雷诺数 (Re_τ)", "🌀 CFD"),
    (r"(?:enstrophy|vorticity[\s_-]?magnitude)\s*[:=＝]?\s*([\d.]+)", "涡度拟能 (Enstrophy)", "🌀 CFD"),
    # 通用
    (r"(?:accuracy|acc)\s*[:=＝]?\s*([\d.]+)%?", "准确率 (Accuracy)", "⚪ 通用"),
    (r"(?:MSE|mean[\s_-]?squared[\s_-]?error)\s*[:=＝]?\s*([\d.eE\-\+]+)", "MSE", "⚪ 通用"),
    (r"(?:MAE|mean[\s_-]?absolute[\s_-]?error)\s*[:=＝]?\s*([\d.eE\-\+]+)", "MAE", "⚪ 通用"),
    (r"(?:RMSE|root[\s_-]?mean[\s_-]?squared[\s_-]?error)\s*[:=＝]?\s*([\d.eE\-\+]+)", "RMSE", "⚪ 通用"),
    (r"(?:inference[\s_-]?time|latency)\s*[:=＝]?\s*([\d.]+)\s*(?:s|ms|sec)", "推理时间 (Inference Time)", "⚪ 通用"),
    (r"(?:FLOPs|flops)\s*[:=＝]?\s*([\d.eE\-\+]+[GMk]?)", "FLOPs", "⚪ 通用"),
]

# ── 章节检测模式 ──────────────────────────────────────────
SECTION_PATTERNS = OrderedDict({
    "Abstract": [r"^\s*abstract\s*$", r"^\s*Abstract\s*$", r"^\s*ABSTRACT\s*$"],
    "Introduction": [r"^\s*introduction\s*$", r"^\s*Introduction\s*$", r"^\s*1\.?\s*Introduction"],
    "Related Work": [r"^\s*related\s*work", r"^\s*Related\s*Work", r"^\s*background", r"^\s*Background"],
    "Methods": [r"^\s*method", r"^\s*Method", r"^\s*proposed\s*method", r"^\s*approach", r"^\s*Methodology"],
    "Problem Formulation": [r"^\s*problem\s*formulation", r"^\s*Problem\s*Formulation"],
    "Experiments": [r"^\s*experiment", r"^\s*Experiment", r"^\s*Experimental\s*Setup"],
    "Results": [r"^\s*result", r"^\s*Result", r"^\s*Results\s*and\s*Discussion"],
    "Discussion": [r"^\s*discussion", r"^\s*Discussion"],
    "Conclusion": [r"^\s*conclusion", r"^\s*Conclusion", r"^\s*summary", r"^\s*Summary"],
    "Appendix": [r"^\s*appendix", r"^\s*Appendix", r"^\s*supplementary", r"^\s*Supplementary"],
    "Data Availability": [r"^\s*data\s*availability", r"^\s*Data\s*Availability"],
    "Code Availability": [r"^\s*code\s*availability", r"^\s*Code\s*Availability"],
    "Acknowledgments": [r"^\s*acknowledgment", r"^\s*Acknowledgment"],
    "References": [r"^\s*reference", r"^\s*Reference", r"^\s*bibliography", r"^\s*Bibliography"],
})


def ensure_deps():
    """确保 PyMuPDF + Pillow 已安装"""
    try:
        import fitz
        from PIL import Image
    except ImportError:
        print("[paper-analysis] 依赖未安装，正在自动安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pymupdf", "Pillow", "-q"], check=True)
        print("[paper-analysis] 依赖安装完成")


def download_pdf(url: str, output_path: str, timeout: int = 60) -> bool:
    """下载 PDF 到本地文件"""
    print(f"[paper-analysis] 📥 下载 PDF: {url}")
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PaperAnalysisBot/2.0)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp, \
                open(output_path, "wb") as out:
            out.write(resp.read())
        size = os.path.getsize(output_path) / 1024
        print(f"[paper-analysis] ✅ 下载完成: {output_path} ({size:.1f} KB)")
        return True
    except Exception as e:
        print(f"[paper-analysis] ❌ 下载失败: {e}")
        return False


def extract_full_text(pdf_path: str) -> tuple:
    """提取 PDF 全文 + 分页列表"""
    import fitz
    doc = fitz.open(pdf_path)
    pages_text = []
    for page in doc:
        pages_text.append(page.get_text())
    full_text = "\n".join(pages_text)
    return full_text, pages_text, len(doc)


def extract_metadata(pdf_path: str) -> dict:
    """提取 PDF 内嵌元数据"""
    import fitz
    doc = fitz.open(pdf_path)
    meta = {
        "title": doc.metadata.get("title", ""),
        "author": doc.metadata.get("author", ""),
        "subject": doc.metadata.get("subject", ""),
        "keywords": doc.metadata.get("keywords", ""),
        "creator": doc.metadata.get("creator", ""),
        "producer": doc.metadata.get("producer", ""),
        "format": doc.metadata.get("format", ""),
        "page_count": len(doc),
    }
    doc.close()
    return meta


def extract_title_from_page1(first_page_text: str) -> str:
    """从第一页提取标题（启发式：取前几行中最长的一行）"""
    lines = [l.strip() for l in first_page_text.split("\n") if l.strip()]
    if not lines:
        return ""
    # 排除常见的元数据行
    skip_prefixes = ["arXiv:", "DOI:", "http", "www.", "Received:", "Accepted:", "Published:"]
    candidates = []
    for line in lines[:30]:
        if any(line.lower().startswith(p) for p in skip_prefixes):
            continue
        if len(line) > 20 and len(line) < 300:
            candidates.append(line)
    if candidates:
        return max(candidates, key=len)
    return candidates[0] if candidates else ""


def detect_doi(text: str) -> str:
    """从文本中检测 DOI"""
    m = re.search(r"10\.\d{4,}/[^\s\"']+", text)
    return m.group(0).rstrip(".,;:") if m else ""


def detect_arxiv_id(text: str, pages_text: list = None, paper_url: str = "", auto_title: str = "", pdf_meta_title: str = "", paper_name: str = "", project_name: str = "") -> tuple:
    """
    五层优先级提取 arXiv ID (返回 id + 来源标记):
    P1: 下载源 URL 含 arxiv.org → 直接从 URL 提取
    P2: 仅搜索 PDF 前 3 页 (标题/摘要区, 过滤页眉, 避开参考文献)
    P3: 全文搜索 → 正文区行号最小匹配 (排除参考文献区 + 页眉)
    P4: DOI → arXiv API 解析 (最可靠, 适用于 Nature/Springer 出版版)
    P5: 标题搜索 arXiv API (含标题重合度 ≥40% 验证)
    兜底: 空字符串, 宁可留空也不猜错
    """

    # ── P1: 从下载源 URL 提取 ──
    if paper_url:
        m = re.search(r'arxiv\.org/(?:abs|pdf)/([\d.]+)', paper_url, re.IGNORECASE)
        if m:
            return m.group(1), "P1: 下载源URL"

    # ── P2: 仅搜索前 3 页, 排除 arXiv 页眉行 (格式: "arXiv:ID [category] Date") ──
    if pages_text and len(pages_text) >= 1:
        first_pages = pages_text[:min(3, len(pages_text))]
        p2_matches = []
        for pi, page_text in enumerate(first_pages):
            for m in re.finditer(r'arXiv:(\d{4}\.\d{4,}(?:v\d+)?)', page_text, re.IGNORECASE):
                context_line = page_text[max(0,m.start()-5):m.end()+60]
                # 排除 arXiv 页眉格式: "arXiv:ID [category] Date" (来自 PDF 导出的其他论文元数据)
                if re.search(r'\[\w+[-.\w]*\]\s+\d{1,2}\s+\w+\s+\d{4}', context_line):
                    continue
                # 排除纯数字日期格式: "arXiv:ID 10 Nov 2025"
                if re.search(r'\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}', context_line):
                    continue
                p2_matches.append((pi, m.group(1)))
        if p2_matches:
            # 取第一页的匹配优先
            p2_matches.sort(key=lambda x: x[0])
            return p2_matches[0][1], f"P2: 论文前3页(过滤页眉)"

    # ── P3: 全文搜索, 行号最小 + 正文区(References前) + 过滤页眉 ──
    lines = text.split("\n")
    # 找 References 起始: 匹配标题行 或 首个引用条目 "[[1]" / "[1]" / "1. "
    ref_start = len(lines)
    ref_pat = re.compile(r'^\s*(R|r)eferences\s*$|^\s*(B|b)ibliography\s*$')
    cite_start = re.compile(r'^\s*\[+\s*1\s*\]+\s')  # "[1]" or "[[1]]"
    for i, line in enumerate(lines):
        s = line.strip()
        if s and (ref_pat.match(s) or cite_start.match(s) or s == 'References'):
            ref_start = i
            break
    best_line = float('inf')
    best_id = ""
    header_date_pat = re.compile(r'\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}')
    for i, line in enumerate(lines):
        if i >= ref_start:
            continue
        m = re.search(r'arXiv:(\d{4}\.\d{4,}(?:v\d+)?)', line, re.IGNORECASE)
        if m and i < best_line:
            ctx = line[m.start():m.end()+60]
            if header_date_pat.search(ctx):
                continue
            best_line = i
            best_id = m.group(1)
    if best_id:
        return best_id, f"P3: 正文区首匹配(行{best_line+1})"

    # ── P4: DOI → arXiv ID 解析 (Nature/Springer 出版版 PDF 最可靠, 优先于标题搜索) ──
    doi = re.search(r'10\.\d{4,}/[^\s"\']+', text)
    if doi:
        try:
            import urllib.request, urllib.parse
            doi_val = doi.group(0).rstrip('.,;:')
            url = f"http://export.arxiv.org/api/query?search_query=doi:{urllib.parse.quote_plus(doi_val)}&max_results=1"
            req = urllib.request.Request(url, headers={"User-Agent": "PaperAnalysisBot/2.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                api_text = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r'<id>http://arxiv\.org/abs/([\d.]+)', api_text)
            if m:
                return m.group(1), "P4: DOI → arXiv 解析"
        except Exception:
            pass

    # ── P5: arXiv API 标题搜索 (多关键词优先级搜索) ──
    # 从文件名/项目名提取搜索关键词
    candidates = []
    if paper_name and len(paper_name) > 3:
        # 从文件名剥离出版标记: "xxx - Nature...(2024)..." → "xxx"
        clean = re.sub(r'\s*-\s*(Nature|Science|Physical Review|arXiv|IEEE|ACM).*', '', paper_name, flags=re.IGNORECASE)
        clean = re.sub(r'\s*\(\d{4}\).*', '', clean)
        clean = re.sub(r'\s*-\s*s\d+-\w+-\d+.*', '', clean)
        clean = clean.strip()
        if len(clean) > 10:
            candidates.append(("文件名", clean))
    if project_name and len(project_name) > 2:
        candidates.append(("项目名", project_name))
    candidates.append(("PDF元数据", pdf_meta_title or ""))
    candidates.append(("页面提取", auto_title or ""))

    for source, search_title in candidates:
        if not search_title or len(search_title.strip()) < 5:
            continue
        try:
            import urllib.request, urllib.parse
            query = urllib.parse.quote_plus(search_title[:200])
            url = f"http://export.arxiv.org/api/query?search_query=all:{query}&max_results=1"
            req = urllib.request.Request(url, headers={"User-Agent": "PaperAnalysisBot/2.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                api_text = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r'<id>http://arxiv\.org/abs/([\d.]+)', api_text)
            if m:
                api_id = m.group(1)
                entry_match = re.search(r'<entry>.*?</entry>', api_text, re.DOTALL)
                if entry_match:
                    tm = re.search(r'<title>(.*?)</title>', entry_match.group(0))
                    if tm:
                        api_title = tm.group(1).strip()
                        st_lower = search_title.lower().strip()
                        at_lower = api_title.lower()
                        # 子串匹配: 搜索词是否出现在 API 返回的标题中
                        if st_lower in at_lower:
                            return api_id, f"P5: arXiv API 子串匹配({source})"
                        # 分词退一步: 搜索关键词 ≥50% 出现在标题中即可
                        st_parts = [p for p in re.split(r'[\s:\-–—]+', st_lower) if len(p) > 2]
                        if st_parts:
                            hits = sum(1 for p in st_parts if p in at_lower)
                            if hits / len(st_parts) >= 0.5:
                                return api_id, f"P5: arXiv API 分词匹配({source},{hits}/{len(st_parts)})"
                    # 无匹配 → 继续下一个候选
        except Exception:
            pass

    return "", "未检测到"


def segment_sections(full_text: str, pages_text: list) -> dict:
    """按章节分段文本"""
    sections = OrderedDict()
    lines = full_text.split("\n")

    current_section = "Preamble"
    sections[current_section] = []
    section_boundaries = []  # (section_name, line_index)

    for i, line in enumerate(lines):
        stripped = line.strip()
        matched = None
        for sec_name, patterns in SECTION_PATTERNS.items():
            for pat in patterns:
                if re.match(pat, stripped, re.IGNORECASE):
                    matched = sec_name
                    break
            if matched:
                break
        if matched:
            current_section = matched
            section_boundaries.append((matched, i))
            if matched not in sections:
                sections[matched] = []
        sections[current_section].append(line)

    result = {}
    for sec_name, sec_lines in sections.items():
        text = "\n".join(sec_lines).strip()
        if text:
            result[sec_name] = text

    return result


def export_formula_pages(pdf_path: str, output_dir: Path, pages_text: list,
                         max_pages: int = 8) -> list:
    """
    检测公式/表格密集页面并导出为图像。
    启发式: 页文本含大量数学符号、短行、或表格分隔符
    """
    import fitz
    doc = fitz.open(pdf_path)
    img_dir = output_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    formula_indicators = [r"\(", r"\[", r"\\begin\{", r"\\frac", r"\\sum",
                          r"\\int", r"\\prod", r"\\partial", r"\\nabla",
                          r"\\mathbf", r"\\mathbb", r"\\mathcal", r"\\infty",
                          r"equation", r"align", r"\\langle", r"\\rangle"]

    scored_pages = []
    for i, text in enumerate(pages_text):
        score = 0
        for ind in formula_indicators:
            score += len(re.findall(ind, text, re.IGNORECASE)) * 3
        # 检测表格 (管道符分隔的数据行)
        table_lines = len([l for l in text.split("\n") if l.count("|") >= 2])
        score += table_lines * 2
        # 检测 LaTeX 公式环境
        score += text.count("\\begin{") * 5 + text.count("\\end{") * 5
        if score > 0:
            scored_pages.append((i, score))

    scored_pages.sort(key=lambda x: -x[1])

    exported = []
    for idx, (page_num, score) in enumerate(scored_pages[:max_pages]):
        page = doc[page_num]
        # 渲染为高清图像
        mat = page.get_pixmap(dpi=200)
        img_path = img_dir / f"page_{page_num + 1}_formula.png"
        mat.save(str(img_path))
        exported.append({
            "page": page_num + 1,
            "formula_score": score,
            "image_path": str(img_path)
        })
        print(f"[paper-analysis] 📸 导出公式/表格页: p{page_num + 1} (score={score})")

    doc.close()
    return exported


RESULT_FIGURE_CAPTION_RE = re.compile(
    r"^\s*((?:fig(?:ure)?\.?|图)\s*[A-Za-z]?\d+(?:[A-Za-z]|\([A-Za-z0-9]+\))?)"
    r"\s*[:.\-—]?\s*(.+)$",
    re.IGNORECASE,
)
RESULT_FIGURE_POSITIVE_TERMS = (
    "result", "comparison", "prediction", "ground truth", "error", "accuracy",
    "loss", "performance", "ablation", "spectrum", "distribution", "profile",
    "contour", "field", "velocity", "pressure", "temperature", "stress",
    "reconstruction", "generated", "evaluation", "metric", "experiment",
    "结果", "对比", "预测", "真值", "误差", "性能", "消融", "频谱", "分布",
    "剖面", "等值", "流场", "速度", "压力", "温度", "应力", "重建", "评估",
)
RESULT_FIGURE_EXCLUSION_TERMS = (
    "architecture", "framework", "pipeline", "workflow", "overview", "schematic",
    "method", "network structure", "model structure", "algorithm", "flowchart",
    "架构", "框架", "流程", "概览", "示意", "方法", "网络结构", "模型结构", "算法",
)


def _result_figure_id(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip()).replace("Figure", "Fig.").replace("figure", "Fig.")


def _result_figure_context(page_text: str, caption: str, radius: int = 700) -> str:
    normalized = page_text or ""
    position = normalized.casefold().find(caption[:40].casefold())
    if position < 0:
        return normalized[: radius * 2].strip()
    return normalized[max(0, position - radius): position + len(caption) + radius].strip()


def _is_result_figure(caption: str, context: str) -> tuple[bool, str]:
    combined = f"{caption}\n{context}".casefold()
    if any(term in caption.casefold() for term in RESULT_FIGURE_EXCLUSION_TERMS):
        return False, "caption_matches_method_or_structure_figure"
    if any(term in combined for term in RESULT_FIGURE_POSITIVE_TERMS):
        return True, "caption_or_context_matches_experimental_result"
    return False, "insufficient_experimental_result_evidence"


def extract_result_figure_candidates(
    pdf_path: str,
    output_dir: Path,
    pages_text: list[str],
    max_figures: int = 24,
) -> tuple[list[dict], list[dict]]:
    """Detect result figures, but leave every candidate blocked for Agent review."""
    import fitz

    document = fitz.open(pdf_path)
    image_dir = output_dir / "images" / "result_figures"
    image_dir.mkdir(parents=True, exist_ok=True)
    candidates = []
    excluded = []
    seen_ids = set()

    for page_index, page in enumerate(document):
        page_text = pages_text[page_index] if page_index < len(pages_text) else page.get_text()
        blocks = sorted(page.get_text("blocks"), key=lambda item: (item[1], item[0]))
        for block_index, block in enumerate(blocks):
            block_text = str(block[4] or "").strip()
            caption_match = None
            for line in block_text.splitlines():
                caption_match = RESULT_FIGURE_CAPTION_RE.match(line.strip())
                if caption_match:
                    break
            if not caption_match:
                continue
            raw_id, caption_body = caption_match.group(1), caption_match.group(2)
            figure_id = _result_figure_id(raw_id)
            unique_id = f"p{page_index + 1}:{figure_id.casefold()}"
            if unique_id in seen_ids:
                continue
            seen_ids.add(unique_id)
            caption = f"{figure_id} {caption_body}".strip()
            context = _result_figure_context(page_text, caption_body)
            included, reason = _is_result_figure(caption, context)
            base_record = {
                "figure_id": figure_id,
                "caption": caption,
                "source_page": f"p. {page_index + 1}",
                "detection_reason": reason,
                "caption_block": {
                    "x0": round(float(block[0]), 2),
                    "y0": round(float(block[1]), 2),
                    "x1": round(float(block[2]), 2),
                    "y1": round(float(block[3]), 2),
                },
            }
            if not included:
                excluded.append(base_record)
                continue
            if len(candidates) >= max_figures:
                continue

            earlier_bottoms = [
                float(item[3])
                for item in blocks[:block_index]
                if float(item[3]) <= float(block[1])
            ]
            crop_top = max(earlier_bottoms) if earlier_bottoms else 0.0
            if float(block[1]) - crop_top < page.rect.height * 0.12:
                crop_top = max(0.0, float(block[1]) - page.rect.height * 0.55)
            crop = fitz.Rect(
                0,
                max(0.0, crop_top),
                page.rect.width,
                min(page.rect.height, float(block[3])),
            )
            safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", figure_id).strip("_") or f"figure_{len(candidates) + 1}"
            image_path = image_dir / f"page_{page_index + 1}_{safe_id}.png"
            page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=crop, alpha=False).save(str(image_path))

            evidence_candidates = []
            for sentence in re.split(r"(?<=[.!?。！？])\s+", context):
                lowered = sentence.casefold()
                if any(token in lowered for token in (
                    "plot", "computed", "calculated", "evaluated", "normalized",
                    "averaged", "sampled", "shown", "we use", "we compare",
                    "绘制", "计算", "归一化", "平均", "采样", "评估", "对比",
                )):
                    evidence_candidates.append({
                        "text": sentence.strip(),
                        "source_page": f"p. {page_index + 1}",
                    })

            candidates.append({
                **base_record,
                "reference_image_path": str(image_path),
                "category": "experimental_result",
                "case_name": "",
                "status": "blocked",
                "reproduction_evidence": [],
                "evidence_candidates": evidence_candidates[:8],
                "renderer": "generic",
                "data_requirements": [],
                "transforms": [],
                "layout": {},
                "panels": [],
                "blocking_reasons": [
                    "Agent 尚未从正文/实验设置/附录确认关联案例",
                    "Agent 尚未提取带页码的完整绘制方法",
                    "变量、坐标、单位、采样范围和真实数据绑定尚未确认",
                    "分面、坐标轴、图例、色条和数据变换尚未确认",
                ],
            })
    document.close()
    return candidates, excluded


def extract_hyperparams(text: str) -> list:
    """模式匹配提取超参数"""
    found = []
    seen = set()
    for pattern, label in HP_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if not m.groups() or m.lastindex is None:
                value = "✓ (mentioned)"
            else:
                value = m.group(1).strip().replace("×", "×").replace("x", "×")
            key = f"{label}:{value}"
            if key not in seen:
                seen.add(key)
                found.append({
                    "parameter": label,
                    "value": value,
                    "context": text[max(0, m.start() - 60):m.end() + 60].strip()
                })
    return found


def extract_metrics(text: str) -> list:
    """模式匹配提取评估指标"""
    found = []
    seen = set()
    for pattern, label, domain in METRIC_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if m.groups() and m.lastindex and m.lastindex >= 1:
                value = m.group(1).strip()
            else:
                value = "✓ (mentioned)"
            key = f"{label}:{value}"
            if key not in seen:
                seen.add(key)
                found.append({
                    "metric": label,
                    "value": value,
                    "domain": domain,
                    "context": text[max(0, m.start() - 80):m.end() + 80].strip()
                })
    return found


def detect_domain(text: str) -> dict:
    """关键词检测建议领域分类"""
    text_lower = text.lower()
    scores = {}
    for domain, config in DOMAIN_KEYWORDS.items():
        hits = 0
        matched_kw = []
        for kw in config["keywords"]:
            count = len(re.findall(re.escape(kw.lower()), text_lower))
            if count > 0:
                hits += count
                matched_kw.append(kw)
        scores[domain] = {
            "hit_count": hits,
            "threshold": config["threshold"],
            "passed": hits >= config["threshold"],
            "matched_keywords": matched_kw[:20]
        }

    # 按 hit_count 排序
    ranked = sorted(scores.items(), key=lambda x: -x[1]["hit_count"])
    primary = ranked[0] if ranked else (None, None)
    return {
        "primary_domain": primary[0] if primary and primary[1]["passed"] else "⚪ 通用深度学习",
        "primary_confidence": min(primary[1]["hit_count"] / max(primary[1]["threshold"], 1), 1.0) if primary else 0.0,
        "all_scores": OrderedDict(ranked)
    }


def extract_baseline_tables_hint(text: str) -> list:
    """检测包含数值对比的表格区域（给 Agent 提供定位提示）"""
    # 寻找 "Table" 附近包含数字对比的段落
    table_regions = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if re.search(r"Table\s+\d+", line, re.IGNORECASE):
            # 收集后续 20 行
            region = "\n".join(lines[i:min(i + 25, len(lines))])
            # 检查是否含数字对比
            numbers = re.findall(r"[\d]+\.[\d]+", region)
            if len(numbers) >= 3:
                table_regions.append({
                    "caption": line.strip(),
                    "line_start": i + 1,
                    "line_end": min(i + 25, len(lines)),
                    "text": region
                })
    return table_regions[:10]  # 最多返回 10 个


def main():
    parser = argparse.ArgumentParser(description="paper-analysis v2.0: 结构化论文解析")
    parser.add_argument("--github-url", type=str, default="")
    parser.add_argument("--paper-url", type=str, default="")
    parser.add_argument("--paper-path", type=str, default="")
    parser.add_argument("--project-name", type=str, required=True,
                        help="项目名 (必填)，产物输出到 <输出根>/<project-name>/（输出根自动探测，见 FIXED_OUTPUT_ROOT）")
    parser.add_argument("--paper-name", type=str, default="")
    parser.add_argument("--export-images", action="store_true", default=True,
                        help="导出公式/表格页面为图像 (默认开启)")
    parser.add_argument("--no-images", action="store_true",
                        help="不导出图像")
    parser.add_argument("--max-formula-pages", type=int, default=8,
                        help="最大导出公式页数 (默认 8)")
    parser.add_argument("--max-result-figures", type=int, default=24,
                        help="最大实验结果图候选数量 (默认 24)")
    args = parser.parse_args()

    ensure_deps()

    # ── 1. 解析输入源 ──────────────────────────────────────
    pdf_path = args.paper_path
    pdf_source = ""

    if pdf_path:
        if not os.path.exists(pdf_path):
            print(f"[paper-analysis] ❌ 指定的本地 PDF 路径不存在: {pdf_path}")
            sys.exit(1)
        pdf_source = "local"
    elif args.paper_url:
        pdf_source = "paper_url"
    elif args.github_url:
        pdf_source = "github"
    else:
        print("[paper-analysis] ❌ 请提供 --paper-url, --github-url 或 --paper-path")
        sys.exit(1)

    project_name = args.project_name.strip()
    if not project_name:
        print("[paper-analysis] ❌ --project-name 不能为空")
        sys.exit(1)
    paper_name = args.paper_name or project_name

    # 输出目录固定为本轮 step_1；project_name 只作为项目元数据。
    output_dir = _resolve_step_1_output()
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[paper-analysis] 📁 输出目录: {output_dir}")

    # ── 2. 网络下载 ────────────────────────────────────────
    if pdf_source == "paper_url":
        pdf_local = str(output_dir / "paper.pdf")
        if not download_pdf(args.paper_url, pdf_local):
            sys.exit(1)
        pdf_path = pdf_local
    elif pdf_source == "github":
        print(f"[paper-analysis] 🔍 从 GitHub 解析论文链接: {args.github_url}")
        try:
            req = urllib.request.Request(args.github_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                readme = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"[paper-analysis] ❌ GitHub README 获取失败: {e}")
            sys.exit(1)

        arxiv_url = None
        for pattern in [
            r"https://arxiv\.org/pdf/([0-9\.]+)",
            r"https://arxiv\.org/abs/([0-9\.]+)",
            r"arxiv\.org/abs/([0-9\.]+)",
        ]:
            m = re.search(pattern, readme, re.IGNORECASE)
            if m:
                arxiv_id = m.group(1)
                arxiv_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                break
        if not arxiv_url:
            print("[paper-analysis] ❌ 无法从 GitHub README 找到 arXiv 链接")
            sys.exit(1)

        pdf_local = str(output_dir / "paper.pdf")
        if not download_pdf(arxiv_url, pdf_local):
            sys.exit(1)
        pdf_path = pdf_local

    # ── 3. 结构化提取 ──────────────────────────────────────
    print(f"[paper-analysis] 🔬 开始结构化解析: {pdf_path}")

    # 3a. PDF 元数据
    pdf_meta = extract_metadata(pdf_path)
    print(f"[paper-analysis]   📋 PDF 元数据: {pdf_meta['page_count']} 页")

    # 3b. 全文 + 分页文本
    full_text, pages_text, num_pages = extract_full_text(pdf_path)
    print(f"[paper-analysis]   📝 全文提取: {len(full_text):,} 字符")

    # 3c. 标题推断
    auto_title = extract_title_from_page1(pages_text[0]) if pages_text else ""

    # 3d. DOI / arXiv ID (四层优先级)
    doi = detect_doi(full_text)
    source_url = args.paper_url if pdf_source == "paper_url" else (arxiv_url if pdf_source == "github" else "")
    arxiv_id, arxiv_source = detect_arxiv_id(
        full_text, pages_text=list(pages_text) if pages_text else None,
        paper_url=source_url, auto_title=auto_title,
        pdf_meta_title=pdf_meta.get("title", ""),
        paper_name=paper_name,
        project_name=project_name
    )
    if arxiv_id:
        print(f"[paper-analysis]   🔖 arXiv ID: {arxiv_id} ({arxiv_source})")
    else:
        print(f"[paper-analysis]   🔖 arXiv ID: 未检测到 ({arxiv_source})")

    # 3e. 章节分段
    sections = segment_sections(full_text, pages_text)
    section_summary = {k: f"{len(v):,} chars" for k, v in sections.items()}
    print(f"[paper-analysis]   📑 章节分段: {list(sections.keys())}")

    # 3f. 公式/表格页导出
    formula_pages = []
    if args.export_images and not args.no_images:
        formula_pages = export_formula_pages(pdf_path, output_dir, pages_text,
                                              max_pages=args.max_formula_pages)

    # 3g. 领域检测
    domain_info = detect_domain(full_text)
    print(f"[paper-analysis]   🏷️  领域检测: {domain_info['primary_domain']} "
          f"(confidence={domain_info['primary_confidence']:.2f})")

    # 3h. 超参提取
    hyperparams = extract_hyperparams(full_text)
    print(f"[paper-analysis]   ⚙️  提取超参: {len(hyperparams)} 项")

    # 3i. 指标提取
    metrics = extract_metrics(full_text)
    print(f"[paper-analysis]   📊 提取指标: {len(metrics)} 项")

    # 3j. Baseline 表格定位
    baseline_tables = extract_baseline_tables_hint(full_text)
    print(f"[paper-analysis]   📈 定位表格区域: {len(baseline_tables)} 处")

    # 3k. 结果图检测只生成严格阻断的规范骨架；Agent 补齐论文证据后才可 ready。
    result_figures, excluded_figures = extract_result_figure_candidates(
        pdf_path,
        output_dir,
        list(pages_text),
        max_figures=args.max_result_figures,
    )
    print(
        f"[paper-analysis]   🖼️  实验结果图候选: {len(result_figures)} 处"
        f"（排除/证据不足 {len(excluded_figures)} 处）"
    )

    # ── 4. 保存产出物 ──────────────────────────────────────

    # 4a. 纯文本 (向后兼容)
    text_path = output_dir / "paper_text.txt"
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(full_text)

    # 4b. 章节分段文本
    sections_path = output_dir / "paper_sections.json"
    with open(sections_path, "w", encoding="utf-8") as f:
        json.dump(sections, f, ensure_ascii=False, indent=2)

    figure_specs_path = output_dir / "paper_result_figure_specs.json"
    with open(figure_specs_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "schema_version": "1.0",
                "paper": {
                    "paper_name": paper_name,
                    "title": auto_title or pdf_meta.get("title", ""),
                    "doi": doi,
                    "arxiv_id": arxiv_id,
                },
                "figures": result_figures,
                "excluded_candidates": excluded_figures,
                "agent_completion_required": True,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # 4c. 结构化元数据 JSON (核心新增产物)
    meta_output = {
        "version": "2.0",
        "paper_name": paper_name,
        "output_dir": str(output_dir),
        "pdf_metadata": pdf_meta,
        "auto_title": auto_title,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "arxiv_source": arxiv_source,
        "num_pages": num_pages,
        "total_chars": len(full_text),
        "sections": section_summary,
        "domain": domain_info,
        "formula_pages": formula_pages,
        "hyperparams": hyperparams,
        "metrics": metrics,
        "baseline_table_regions": baseline_tables,
        "result_figure_count": len(result_figures),
        "excluded_result_figure_count": len(excluded_figures),
        "files": {
            "full_text": str(text_path),
            "sections_json": str(sections_path),
            "result_figure_specs": str(figure_specs_path),
        }
    }

    meta_path = output_dir / "paper_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta_output, f, ensure_ascii=False, indent=2)

    # 4d. SciML/物理论文的机器可执行复现契约。即使判定为传统 ML 也输出，
    # 让后续步骤使用显式 profile，而不是通过“没有文件”猜测项目类型。
    scientific_contract = build_contract(
        full_text,
        source=str(text_path),
        metadata={
            "paper_name": paper_name,
            "title": auto_title or pdf_meta.get("title", ""),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "domain": domain_info,
        },
    )
    contract_path = output_dir / "scientific_repro_contract.json"
    with open(contract_path, "w", encoding="utf-8") as f:
        json.dump(scientific_contract, f, ensure_ascii=False, indent=2)
    meta_output["files"]["scientific_repro_contract"] = str(contract_path)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta_output, f, ensure_ascii=False, indent=2)

    # ── 5. 完成摘要 ────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  ✅ 结构化论文解析完成！")
    print("=" * 65)
    print(f"  论文名称: {auto_title or paper_name}")
    print(f"  领域判定: {domain_info['primary_domain']} (置信度: {domain_info['primary_confidence']:.0%})")
    print(f"  总字符数: {len(full_text):,}")
    print(f"  章节分段: {len(sections)} 个")
    print(f"  公式图像: {len(formula_pages)} 页")
    print(f"  检测超参: {len(hyperparams)} 项")
    print(f"  检测指标: {len(metrics)} 项")
    print(f"  表格区域: {len(baseline_tables)} 处")
    print(f"  实验结果图候选: {len(result_figures)} 处")
    print()
    print(f"  📄 纯文本:    {text_path}")
    print(f"  📋 结构化元数据: {meta_path}")
    print(f"  🧭 科学复现契约: {contract_path}")
    print(f"  📑 章节分段:  {sections_path}")
    print(f"  📊 结果图规范: {figure_specs_path}")
    if formula_pages:
        print(f"  🖼️  公式图像:  {output_dir / 'images'}/")
    print()
    print(f"  👉 下一步: Agent 请读取 {meta_path} 获取结构化元数据,")
    print(f"     结合 paper_text.txt 全文 + 公式图像, 生成 paper_report.md")
    print("=" * 65)


if __name__ == "__main__":
    main()
