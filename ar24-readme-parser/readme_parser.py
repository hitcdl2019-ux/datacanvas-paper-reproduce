import hashlib
import re
import shlex
from urllib.parse import urlparse


FENCE_RE = re.compile(r"^\s{0,3}(```+|~~~+)\s*([A-Za-z0-9_.+-]*)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
URL_RE = re.compile(r"https?://[^\s<>)\]]+")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)

DEPENDENCY_PREFIXES = (
    ("pip", "install"),
    ("pip3", "install"),
    ("python", "-m", "pip", "install"),
    ("python3", "-m", "pip", "install"),
    ("uv", "pip", "install"),
    ("conda", "install"),
    ("mamba", "install"),
    ("micromamba", "install"),
)
RUNTIME_DEPENDENCY_PREFIXES = (
    ("julia", "--project", "-e"),
    ("julia", "-e"),
)
DATA_PREFIXES = (("gdown",), ("wget",), ("curl",), ("tar",), ("unzip",))
WEIGHT_PREFIXES = (("hf", "download"), ("huggingface-cli", "download"))
BUILD_PREFIXES = (
    ("python", "setup.py"),
    ("python3", "setup.py"),
    ("cmake",),
    ("make",),
    ("ninja",),
)
EDITABLE_INSTALL_PREFIXES = (
    ("pip", "install", "-e"),
    ("pip3", "install", "-e"),
    ("python", "-m", "pip", "install", "-e"),
    ("python3", "-m", "pip", "install", "-e"),
    ("uv", "pip", "install", "-e"),
)
RUN_PREFIXES = (
    ("python",),
    ("python3",),
    ("bash",),
    ("sh",),
    ("torchrun",),
    ("accelerate",),
    ("deepspeed",),
    ("julia",),
    ("mpirun",),
    ("mpiexec",),
    ("blockMesh",),
    ("foamRun",),
    ("simpleFoam",),
    ("pimpleFoam",),
    ("decomposePar",),
    ("reconstructPar",),
)
CONTEXT_PREFIXES = (
    ("conda", "create"),
    ("conda", "activate"),
    ("mamba", "create"),
    ("micromamba", "create"),
    ("git", "clone"),
    ("cd",),
)
COMPILED_PACKAGE_HINTS = {
    "apex",
    "flash-attn",
    "flash_attn",
    "kaolin",
    "nvdiffrast",
    "pytorch3d",
    "spconv",
}
MANUAL_DOWNLOAD_HINTS = (
    "baidu",
    "extract code",
    "extraction code",
    "提取码",
    "google drive",
    "request access",
    "apply",
    "agreement",
    "contact authors",
    "license agreement",
    "academic email",
)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


def parse_readme_text(text: str, source: str = "README.md") -> dict:
    lines = text.splitlines()
    blocks = []
    heading_path = []
    index = 0

    while index < len(lines):
        line = lines[index]
        line_no = index + 1

        fence = FENCE_RE.match(line)
        if fence:
            block, index = _consume_fenced_code(lines, index, heading_path, source)
            blocks.append(block)
            continue

        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_path = _update_heading_path(heading_path, level, title)
            blocks.append(
                _make_block(
                    "heading",
                    source,
                    line_no,
                    line_no,
                    [line],
                    heading_path,
                    heading_level=level,
                    title=title,
                )
            )
            index += 1
            continue

        if not line.strip():
            start = index
            while index < len(lines) and not lines[index].strip():
                index += 1
            blocks.append(
                _make_block(
                    "blank",
                    source,
                    start + 1,
                    index,
                    lines[start:index],
                    heading_path,
                )
            )
            continue

        if _is_table_start(lines, index):
            start = index
            index += 2
            while index < len(lines) and _is_table_line(lines[index]):
                index += 1
            raw_lines = lines[start:index]
            blocks.append(
                _make_block(
                    "table",
                    source,
                    start + 1,
                    index,
                    raw_lines,
                    heading_path,
                    table=_parse_table(raw_lines),
                )
            )
            continue

        if LIST_RE.match(line):
            start = index
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if not candidate.strip():
                    break
                if HEADING_RE.match(candidate) or FENCE_RE.match(candidate) or _is_table_start(lines, index):
                    break
                if LIST_RE.match(candidate) or candidate.startswith((" ", "\t")):
                    index += 1
                    continue
                break
            blocks.append(
                _make_block(
                    "list",
                    source,
                    start + 1,
                    index,
                    lines[start:index],
                    heading_path,
                )
            )
            continue

        if line.lstrip().startswith("<"):
            start = index
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith("<"):
                index += 1
            blocks.append(
                _make_block(
                    "html",
                    source,
                    start + 1,
                    index,
                    lines[start:index],
                    heading_path,
                )
            )
            continue

        start = index
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if (
                not candidate.strip()
                or HEADING_RE.match(candidate)
                or FENCE_RE.match(candidate)
                or _is_table_start(lines, index)
                or LIST_RE.match(candidate)
                or candidate.lstrip().startswith("<")
            ):
                break
            index += 1
        blocks.append(
            _make_block(
                "paragraph",
                source,
                start + 1,
                index,
                lines[start:index],
                heading_path,
            )
        )

    covered = []
    for block in blocks:
        covered.extend(range(block["line_start"], block["line_end"] + 1))
    expected = list(range(1, len(lines) + 1))
    uncovered = [line_no for line_no in expected if line_no not in set(covered)]

    return {
        "source": source,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "line_count": len(lines),
        "block_count": len(blocks),
        "blocks": blocks,
        "coverage": {
            "covered_line_count": len(covered),
            "uncovered_lines": uncovered,
            "has_overlap": len(covered) != len(set(covered)),
        },
    }


def analyze_readme_text(text: str, source: str = "README.md") -> dict:
    document = parse_readme_text(text, source=source)
    commands = []
    links = []
    issues = []
    unprojected = []
    block_semantics = []
    projection = {
        "dependency_cmds": [],
        "runtime_dependency_cmds": [],
        "data_cmds": [],
        "weight_cmds": [],
        "deferred_step7_cmds": [],
        "restricted_datasets": [],
        "physics_asset_links": [],
    }

    for block in document["blocks"]:
        section_kind = classify_section(block["heading_path"], block["raw"])
        projected_steps = set()
        block_links = [_classify_link(link, block, section_kind) for link in block["links"]]
        links.extend(block_links)

        for issue in _manual_download_issues(block, block_links):
            _append_unique_dict(issues, issue)
            if section_kind == "data":
                _append_unique_dict(
                    projection["restricted_datasets"],
                    {
                        "source": source,
                        "section": _section_title(block),
                        "reason": issue["issue_code"],
                        "source_block_id": block["id"],
                    },
                )

        for link in block_links:
            if link["kind"] == "hf_dataset":
                _append_unique(projection["data_cmds"], _hf_dataset_command(link["repo_id"]))
                projected_steps.add("step_5")
            elif link["kind"] == "hf_model":
                _append_unique(projection["weight_cmds"], _hf_model_command(link["repo_id"]))
                projected_steps.add("step_6")
            elif link["kind"] == "scientific_asset_bundle":
                _append_unique_dict(projection["physics_asset_links"], link)
                projected_steps.add("step_5" if section_kind == "data" else "step_6" if section_kind == "weight" else "step_4")

        for cmd in _commands_from_block(block):
            classified = classify_command(cmd, section_kind, block)
            commands.append(classified)
            if classified["target_step"] == "step_4":
                target = "runtime_dependency_cmds" if classified["kind"] == "runtime_dependency" else "dependency_cmds"
                _append_unique(projection[target], cmd)
                projected_steps.add("step_4")
            elif classified["target_step"] == "step_5":
                _append_unique(projection["data_cmds"], cmd)
                projected_steps.add("step_5")
            elif classified["target_step"] == "step_6":
                _append_unique(projection["weight_cmds"], cmd)
                projected_steps.add("step_6")
            elif classified["target_step"] == "step_7":
                _append_unique(projection["deferred_step7_cmds"], cmd)
                projected_steps.add("step_7")
            elif classified["issue_code"]:
                _append_unique_dict(issues, _issue_from_command(classified))

            if classified["target_step"] == "step_7" and _has_placeholder(cmd):
                _append_unique_dict(
                    issues,
                    {
                        "issue_code": "PLACEHOLDER_REQUIRED",
                        "message": f"Command contains placeholders: {cmd}",
                        "source": source,
                        "source_block_id": block["id"],
                        "lines": [block["line_start"], block["line_end"]],
                    },
                )

        reason = _unprojected_reason(block, section_kind)
        if not projected_steps and block["type"] != "blank" and not reason:
            reason = _default_unprojected_reason(block, section_kind)
        if block["type"] != "blank":
            block_semantics.append(
                {
                    "source": source,
                    "source_block_id": block["id"],
                    "lines": [block["line_start"], block["line_end"]],
                    "type": block["type"],
                    "section_kind": section_kind or "general",
                    "projected_steps": sorted(projected_steps),
                    "reason": "PROJECTED" if projected_steps else reason,
                }
            )
        if reason and not projected_steps:
            _append_unique_dict(
                unprojected,
                {
                    "source": source,
                    "source_block_id": block["id"],
                    "lines": [block["line_start"], block["line_end"]],
                    "type": block["type"],
                    "heading_path": block["heading_path"],
                    "reason": reason,
                },
            )

    environment_bundle = _environment_bundle(document["raw"] if "raw" in document else text, commands)
    return {
        "documents": [document],
        "commands": commands,
        "links": links,
        "issues": issues,
        "unprojected_blocks": unprojected,
        "block_semantics": block_semantics,
        "step_projection": projection,
        "environment_bundle": environment_bundle,
        "summary": {
            "document_count": 1,
            "block_count": document["block_count"],
            "command_count": len(commands),
            "link_count": len(links),
            "issue_count": len(issues),
        },
    }


def merge_analyses(analyses: list[dict]) -> dict:
    merged = {
        "documents": [],
        "commands": [],
        "links": [],
        "issues": [],
        "unprojected_blocks": [],
        "block_semantics": [],
        "step_projection": {
            "dependency_cmds": [],
            "runtime_dependency_cmds": [],
            "data_cmds": [],
            "weight_cmds": [],
            "deferred_step7_cmds": [],
            "restricted_datasets": [],
            "physics_asset_links": [],
        },
        "environment_bundle": {"schema_version": "1.0", "runtimes": [], "system_capabilities": []},
    }
    for analysis in analyses:
        merged["documents"].extend(analysis.get("documents", []))
        merged["commands"].extend(analysis.get("commands", []))
        merged["links"].extend(analysis.get("links", []))
        for issue in analysis.get("issues", []):
            _append_unique_dict(merged["issues"], issue)
        for block in analysis.get("unprojected_blocks", []):
            _append_unique_dict(merged["unprojected_blocks"], block)
        for block in analysis.get("block_semantics", []):
            _append_unique_dict(merged["block_semantics"], block)
        projection = analysis.get("step_projection", {})
        for key in ("dependency_cmds", "runtime_dependency_cmds", "data_cmds", "weight_cmds", "deferred_step7_cmds"):
            for cmd in projection.get(key, []):
                _append_unique(merged["step_projection"][key], cmd)
        for item in projection.get("restricted_datasets", []):
            _append_unique_dict(merged["step_projection"]["restricted_datasets"], item)
        for item in projection.get("physics_asset_links", []):
            _append_unique_dict(merged["step_projection"]["physics_asset_links"], item)
        bundle = analysis.get("environment_bundle") or {}
        for key in ("runtimes", "system_capabilities"):
            for item in bundle.get(key, []):
                _append_unique_dict(merged["environment_bundle"][key], item)

    merged["summary"] = {
        "document_count": len(merged["documents"]),
        "block_count": sum(doc.get("block_count", 0) for doc in merged["documents"]),
        "command_count": len(merged["commands"]),
        "link_count": len(merged["links"]),
        "issue_count": len(merged["issues"]),
        "unprojected_block_count": len(merged["unprojected_blocks"]),
    }
    return merged


def classify_section(heading_path: list[str], raw: str) -> str | None:
    nearest_heading = heading_path[-1] if heading_path else ""
    text = f"{nearest_heading}\n{raw[:400]}".lower()
    if any(token in text for token in ("citation", "citing", "bibtex")):
        return "citation"
    if any(token in text for token in ("copyright", "license")):
        return "license_or_copyright"
    if "acknowledg" in text:
        return "acknowledgement"
    if "news" in text or "updates" in text:
        return "news"
    if any(token in text for token in ("model zoo", "pretrained", "checkpoint", "weight")):
        return "weight"
    if "evaluate" in text or "evaluation" in text or "benchmarking" in text:
        return "evaluation"
    if "inference" in text or "infer" in text:
        return "inference"
    if "demo" in text:
        return "demo"
    if "train" in text or "fine-tun" in text:
        return "train"
    if re.search(r"\b(data|dataset|datasets|benchmark)\b", text):
        return "data"
    if any(token in text for token in ("install", "setup", "requirement", "environment")):
        return "dependency"
    return None


def classify_command(cmd: str, section_kind: str | None, block: dict | None = None) -> dict:
    parts = _split_command(cmd)
    issue_code = None
    target_step = None
    command_kind = "unknown"

    if not parts:
        issue_code = "UNPARSEABLE_COMMAND"
    elif _is_step7_build_command(parts):
        target_step = "step_7"
        command_kind = "build_or_compiled_dependency"
    elif _starts_with_any(parts, RUNTIME_DEPENDENCY_PREFIXES) and ("Pkg.instantiate" in cmd or "Pkg.add" in cmd):
        target_step = "step_4"
        command_kind = "runtime_dependency"
    elif _starts_with_any(parts, DEPENDENCY_PREFIXES):
        target_step = "step_4"
        command_kind = "dependency"
    elif _starts_with_any(parts, WEIGHT_PREFIXES):
        target_step = "step_6" if section_kind == "weight" else "step_7"
        command_kind = "weight_download" if target_step == "step_6" else "runtime_command"
    elif _starts_with_any(parts, DATA_PREFIXES):
        target_step = "step_5" if section_kind == "data" else "step_7"
        command_kind = "data_download" if target_step == "step_5" else "runtime_command"
    elif _starts_with_any(parts, RUN_PREFIXES) and section_kind in {"train", "inference", "demo", "evaluation"}:
        target_step = "step_7"
        command_kind = section_kind
    elif _starts_with_any(parts, CONTEXT_PREFIXES):
        issue_code = "PARSED_NOT_EXECUTABLE"
        command_kind = "environment_context"
    else:
        issue_code = "UNSUPPORTED_COMMAND_PREFIX"

    return {
        "command": cmd,
        "kind": command_kind,
        "target_step": target_step,
        "issue_code": issue_code,
        "source_block_id": block.get("id") if block else None,
        "lines": [block["line_start"], block["line_end"]] if block else None,
    }


def _consume_fenced_code(lines: list[str], start: int, heading_path: list[str], source: str):
    opener = FENCE_RE.match(lines[start])
    marker = opener.group(1)[0]
    language = opener.group(2).strip()
    index = start + 1
    status = "parsed"
    close_re = re.compile(rf"^\s{{0,3}}{re.escape(marker * 3)}")
    while index < len(lines):
        if close_re.match(lines[index]):
            index += 1
            break
        index += 1
    else:
        status = "unclosed_fence"
    block = _make_block(
        "fenced_code",
        source,
        start + 1,
        index,
        lines[start:index],
        heading_path,
        code_language=language,
        parse_status=status,
    )
    return block, index


def _make_block(
    block_type: str,
    source: str,
    line_start: int,
    line_end: int,
    raw_lines: list[str],
    heading_path: list[str],
    **extra,
) -> dict:
    raw = "\n".join(raw_lines)
    block = {
        "id": f"{source}:{line_start}-{line_end}",
        "type": block_type,
        "source": source,
        "line_start": line_start,
        "line_end": line_end,
        "heading_path": list(heading_path),
        "raw": raw,
        "links": _extract_links(raw),
        "inline_code": INLINE_CODE_RE.findall(raw),
        "parse_status": extra.pop("parse_status", "parsed"),
    }
    block.update(extra)
    return block


def _update_heading_path(path: list[str], level: int, title: str) -> list[str]:
    updated = list(path[: max(level - 1, 0)])
    updated.append(title)
    return updated


def _is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and _is_table_line(lines[index])
        and TABLE_SEPARATOR_RE.match(lines[index + 1]) is not None
    )


def _is_table_line(line: str) -> bool:
    return "|" in line and bool(line.strip())


def _parse_table(lines: list[str]) -> dict:
    if len(lines) < 2:
        return {"headers": [], "rows": []}
    headers = _split_table_row(lines[0])
    rows = []
    for raw in lines[2:]:
        cells = _split_table_row(raw)
        if not any(cells):
            continue
        row = {}
        for index, header in enumerate(headers):
            row[header or f"column_{index + 1}"] = cells[index] if index < len(cells) else ""
        rows.append(row)
    return {"headers": headers, "rows": rows}


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _extract_links(raw: str) -> list[dict]:
    links = []
    seen = set()
    for match in MARKDOWN_LINK_RE.finditer(raw):
        url = _clean_url(match.group(2))
        seen.add(url)
        links.append({"text": match.group(1), "url": url})
    for match in URL_RE.finditer(raw):
        url = _clean_url(match.group(0))
        if url not in seen:
            links.append({"text": "", "url": url})
            seen.add(url)
    return links


def _clean_url(url: str) -> str:
    return url.rstrip(".,;:'\"")


def _classify_link(link: dict, block: dict, section_kind: str | None) -> dict:
    url = link["url"]
    kind = "external"
    repo_id = None
    parsed = urlparse(url)
    lowered_path = parsed.path.lower()
    if lowered_path.endswith(IMAGE_SUFFIXES):
        kind = "image"
    elif parsed.netloc.lower() == "huggingface.co":
        repo_id = _hf_repo_id(parsed.path)
        if parsed.path.startswith("/datasets/"):
            kind = "hf_dataset"
        elif parsed.path.startswith("/spaces/"):
            kind = "hf_space"
        elif repo_id:
            kind = "hf_model"
    elif "drive.google.com" in parsed.netloc.lower():
        kind = "manual_download"
    elif "baidu" in parsed.netloc.lower():
        kind = "manual_download"
    elif "arxiv.org" in parsed.netloc.lower():
        kind = "paper"
    elif "zenodo.org" in parsed.netloc.lower() or "figshare.com" in parsed.netloc.lower():
        kind = "scientific_asset_bundle"

    return {
        "text": link.get("text", ""),
        "url": url,
        "kind": kind,
        "repo_id": repo_id,
        "section_kind": section_kind,
        "source_block_id": block["id"],
        "lines": [block["line_start"], block["line_end"]],
    }


def _hf_repo_id(path: str) -> str | None:
    parts = [part for part in path.strip("/").split("/") if part]
    if parts and parts[0] in {"datasets", "spaces"}:
        parts = parts[1:]
    if len(parts) < 2:
        return None
    return "/".join(parts[:2])


def _hf_dataset_command(repo_id: str) -> str:
    return (
        f'hf download {repo_id} --repo-type dataset '
        f'--local-dir "$DATASET_DIR"/{_safe_name(repo_id)}'
    )


def _hf_model_command(repo_id: str) -> str:
    return f'hf download {repo_id} --local-dir "$MODEL_DIR"/{_safe_name(repo_id)}'


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", value).strip("_")


def _manual_download_issues(block: dict, links: list[dict]) -> list[dict]:
    issues = []
    lowered = block["raw"].lower()
    manual_link = any(link["kind"] == "manual_download" for link in links)
    manual_text = any(hint in lowered for hint in MANUAL_DOWNLOAD_HINTS)
    if manual_link or manual_text:
        issues.append(
            {
                "issue_code": "MANUAL_DOWNLOAD_REQUIRED",
                "message": "README describes data or files that may require manual download/access.",
                "source": block["source"],
                "source_block_id": block["id"],
                "lines": [block["line_start"], block["line_end"]],
            }
        )
    return issues


def _commands_from_block(block: dict) -> list[str]:
    raw = block["raw"]
    if block["type"] == "fenced_code":
        lines = raw.splitlines()[1:]
        if lines and FENCE_RE.match(lines[-1]):
            lines = lines[:-1]
    else:
        lines = raw.splitlines()
    return _commands_from_lines(lines)


def _commands_from_lines(lines) -> list[str]:
    commands = []
    pending = ""
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("$ "):
            raw = raw[2:].strip()
        if raw.endswith("\\"):
            pending += raw[:-1].strip() + " "
            continue
        raw = (pending + raw).strip()
        pending = ""
        if _looks_like_command(raw):
            _append_unique(commands, raw)
    if pending and _looks_like_command(pending.strip()):
        _append_unique(commands, pending.strip())
    return commands


def _looks_like_command(cmd: str) -> bool:
    parts = _split_command(cmd)
    if not parts:
        return False
    return _starts_with_any(
        parts,
        DEPENDENCY_PREFIXES
        + RUNTIME_DEPENDENCY_PREFIXES
        + DATA_PREFIXES
        + WEIGHT_PREFIXES
        + BUILD_PREFIXES
        + EDITABLE_INSTALL_PREFIXES
        + RUN_PREFIXES
        + CONTEXT_PREFIXES,
    )


def _environment_bundle(text: str, commands: list[dict]) -> dict:
    lowered = text.casefold()
    runtimes = []
    capabilities = []
    runtime_markers = {
        "python": ("python", "pytorch", "requirements.txt", "conda"),
        "julia": ("julia", "project.toml", "manifest.toml", "bson"),
    }
    capability_markers = {
        "openfoam": ("openfoam", "rheotool", "simplefoam", "pimplefoam", "blockmesh"),
        "mpi": ("mpirun", "mpiexec", "mpi"),
        "compiler": ("cmake", "make", "gcc", "g++", "compile"),
        "container": ("docker", "podman", "singularity", "apptainer"),
    }
    for name, markers in runtime_markers.items():
        hits = [marker for marker in markers if marker in lowered]
        if hits:
            runtimes.append({"name": name, "required": True, "evidence": hits})
    for name, markers in capability_markers.items():
        hits = [marker for marker in markers if marker in lowered]
        if hits:
            capabilities.append({"name": name, "required": True, "evidence": hits})
    return {"schema_version": "1.0", "runtimes": runtimes, "system_capabilities": capabilities}


def _split_command(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return []


def _starts_with_any(parts: list[str], prefixes: tuple[tuple[str, ...], ...]) -> bool:
    return any(tuple(parts[: len(prefix)]) == prefix for prefix in prefixes)


def _is_step7_build_command(parts: list[str]) -> bool:
    if _starts_with_any(parts, BUILD_PREFIXES + EDITABLE_INSTALL_PREFIXES):
        return True
    pip_args = _pip_install_args(parts)
    if pip_args is None:
        return False
    for arg in pip_args:
        lowered = arg.lower()
        if lowered in {"-e", "--editable"} or lowered.startswith("--editable="):
            return True
        if lowered.startswith("git+"):
            return True
        if _is_compiled_package_arg(lowered):
            return True
    return False


def _pip_install_args(parts: list[str]) -> list[str] | None:
    if len(parts) >= 2 and parts[:2] in (["pip", "install"], ["pip3", "install"]):
        return parts[2:]
    if len(parts) >= 4 and parts[:4] in (
        ["python", "-m", "pip", "install"],
        ["python3", "-m", "pip", "install"],
    ):
        return parts[4:]
    if len(parts) >= 3 and parts[0] == "uv" and parts[1:3] == ["pip", "install"]:
        return _drop_uv_python_target(parts[3:])
    return None


def _drop_uv_python_target(args: list[str]) -> list[str]:
    cleaned = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--python":
            skip_next = True
            continue
        if arg.startswith("--python="):
            continue
        cleaned.append(arg)
    return cleaned


def _is_compiled_package_arg(arg: str) -> bool:
    if arg.startswith("-"):
        return False
    normalized = arg.replace("_", "-")
    name = re.split(r"[@=<>~!;\[]", normalized, maxsplit=1)[0]
    normalized_hints = {hint.replace("_", "-") for hint in COMPILED_PACKAGE_HINTS}
    if name in normalized_hints:
        return True
    if normalized.endswith(".whl"):
        wheel_name = normalized.rsplit("/", 1)[-1]
        package = wheel_name.split("-", 1)[0]
        return package in normalized_hints or any(
            wheel_name.startswith(f"{hint}-") for hint in normalized_hints
        )
    return False


def _has_placeholder(cmd: str) -> bool:
    return bool(re.search(r"\b[A-Z][A-Z0-9_]{2,}\b", cmd))


def _issue_from_command(command: dict) -> dict:
    return {
        "issue_code": command["issue_code"],
        "message": f"Command was parsed but not projected: {command['command']}",
        "source_block_id": command["source_block_id"],
        "lines": command["lines"],
    }


def _unprojected_reason(block: dict, section_kind: str | None) -> str | None:
    if block["type"] == "blank":
        return None
    if section_kind == "citation":
        return "CITATION_ONLY"
    if section_kind in {"license_or_copyright", "acknowledgement", "news"}:
        return section_kind.upper()
    if block["type"] == "table" and section_kind not in {"weight", "data"}:
        return "TABLE_PARSED_AS_METADATA"
    return None


def _default_unprojected_reason(block: dict, section_kind: str | None) -> str:
    if block["type"] == "heading":
        return "STRUCTURAL_HEADING"
    if block["type"] == "table":
        return "TABLE_PARSED_AS_METADATA"
    if section_kind:
        return f"{section_kind.upper()}_CONTEXT"
    return "PARSED_AS_CONTEXT"


def _section_title(block: dict) -> str:
    if block["heading_path"]:
        return block["heading_path"][-1]
    return "README"


def _append_unique(values: list, value):
    if value not in values:
        values.append(value)


def _append_unique_dict(values: list[dict], value: dict):
    marker = repr(sorted(value.items()))
    if all(repr(sorted(existing.items())) != marker for existing in values):
        values.append(value)
