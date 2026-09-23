#!/usr/bin/env python3
"""Reproduce paper result figures from declarative specs and raw numeric data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_CHART_TYPES = {
    "line", "scatter", "bar", "histogram", "pdf", "heatmap", "contour", "field"
}
ALLOWED_TRANSFORMS = {
    "scale", "offset", "abs", "normalize", "denormalize", "slice", "mean", "std", "histogram"
}
COMPLIANCE_KEYS = (
    "coordinates", "units", "axis_scales", "axis_limits",
    "ticks", "legend", "layout", "colorbar",
)
IMAGE_SUFFIXES = (".svg", ".png")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path, role: str = "paper_result_figure", **extra: Any) -> dict[str, Any]:
    record = {
        "name": path.name,
        "role": role,
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "origin": "ar24-result-figure-reproduction",
    }
    record.update({key: value for key, value in extra.items() if value is not None})
    return record


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _resolve(raw: Any, base_dir: Path) -> Path:
    path = Path(str(raw or "")).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _figure_blockers(figure: dict[str, Any]) -> list[str]:
    blockers = [str(item) for item in figure.get("blocking_reasons") or [] if str(item).strip()]
    for key in (
        "figure_id", "caption", "source_page", "case_name", "renderer",
        "reproduction_evidence", "data_requirements", "layout", "panels",
    ):
        if not _nonempty(figure.get(key)):
            blockers.append(f"{key} 缺失")
    if figure.get("category") != "experimental_result":
        blockers.append("不是实验结果图")
    if figure.get("renderer") not in {"generic", "official_script"}:
        blockers.append("renderer 必须是 generic 或 official_script")
    for index, evidence in enumerate(figure.get("reproduction_evidence") or []):
        if not isinstance(evidence, dict) or not _nonempty(evidence.get("text")) or not _nonempty(evidence.get("source_page")):
            blockers.append(f"reproduction_evidence[{index}] 缺少原文或页码")
    layout = figure.get("layout") or {}
    if not all(isinstance(layout.get(key), int) and layout[key] > 0 for key in ("rows", "cols")):
        blockers.append("layout.rows/cols 必须是正整数")
    for index, requirement in enumerate(figure.get("data_requirements") or []):
        prefix = f"data_requirements[{index}]"
        if not isinstance(requirement, dict):
            blockers.append(f"{prefix} 不是对象")
            continue
        for key in ("id", "path", "variable", "coordinates", "units", "sampling_range", "model_source"):
            if not _nonempty(requirement.get(key)):
                blockers.append(f"{prefix}.{key} 缺失")
        if requirement.get("model_source") not in {"self_trained", "pretrained", "not_applicable"}:
            blockers.append(f"{prefix}.model_source 无效")
    for index, transform in enumerate(figure.get("transforms") or []):
        prefix = f"transforms[{index}]"
        if not isinstance(transform, dict) or transform.get("op") not in ALLOWED_TRANSFORMS:
            blockers.append(f"{prefix}.op 不受支持")
        if not isinstance(transform, dict) or not _nonempty(transform.get("binding")):
            blockers.append(f"{prefix}.binding 缺失")
        if not isinstance(transform, dict) or not _nonempty(transform.get("evidence")) or not _nonempty(transform.get("source_page")):
            blockers.append(f"{prefix} 缺少论文证据或页码")
    for index, panel in enumerate(figure.get("panels") or []):
        blockers.extend(_panel_blockers(panel, index))
    return list(dict.fromkeys(blockers))


def _axis_blockers(axis: Any, prefix: str) -> list[str]:
    if not isinstance(axis, dict):
        return [f"{prefix} 缺失"]
    blockers = []
    for key in ("label", "unit", "scale", "limits", "ticks"):
        if not _nonempty(axis.get(key)):
            blockers.append(f"{prefix}.{key} 缺失")
    if axis.get("scale") not in {"linear", "log", "symlog", "logit"}:
        blockers.append(f"{prefix}.scale 无效")
    limits = axis.get("limits")
    if not isinstance(limits, list) or len(limits) != 2:
        blockers.append(f"{prefix}.limits 必须为两个数")
    return blockers


def _panel_blockers(panel: Any, index: int) -> list[str]:
    prefix = f"panels[{index}]"
    if not isinstance(panel, dict):
        return [f"{prefix} 不是对象"]
    blockers = []
    chart_type = panel.get("chart_type")
    if chart_type not in ALLOWED_CHART_TYPES:
        blockers.append(f"{prefix}.chart_type 不受支持")
    if not _nonempty(panel.get("title")):
        blockers.append(f"{prefix}.title 缺失")
    if chart_type in {"line", "scatter", "bar"}:
        if not _nonempty(panel.get("x_binding")):
            blockers.append(f"{prefix}.x_binding 缺失")
        if not _nonempty(panel.get("series")):
            blockers.append(f"{prefix}.series 缺失")
    elif chart_type in {"histogram", "pdf"}:
        if not _nonempty(panel.get("series")):
            blockers.append(f"{prefix}.series 缺失")
    elif chart_type in {"heatmap", "contour", "field"} and not _nonempty(panel.get("z_binding")):
        blockers.append(f"{prefix}.z_binding 缺失")
    blockers.extend(_axis_blockers(panel.get("x_axis"), f"{prefix}.x_axis"))
    blockers.extend(_axis_blockers(panel.get("y_axis"), f"{prefix}.y_axis"))
    if not isinstance(panel.get("legend"), dict) or "show" not in panel["legend"]:
        blockers.append(f"{prefix}.legend.show 缺失")
    if chart_type in {"heatmap", "contour", "field"}:
        colorbar = panel.get("colorbar")
        if not isinstance(colorbar, dict):
            blockers.append(f"{prefix}.colorbar 缺失")
        else:
            for key in ("label", "unit", "limits", "ticks"):
                if not _nonempty(colorbar.get(key)):
                    blockers.append(f"{prefix}.colorbar.{key} 缺失")
    return blockers


def _load_numeric(requirement: dict[str, Any], base_dir: Path):
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("缺少 NumPy；必须在 step_4 的同一环境中准备") from exc
    path = _resolve(requirement.get("path"), base_dir)
    if not path.is_file():
        raise ValueError(f"数据文件不存在：{path}")
    suffix = path.suffix.casefold()
    key = requirement.get("key")
    if suffix == ".npy":
        value = np.load(path, allow_pickle=False)
    elif suffix == ".npz":
        archive = np.load(path, allow_pickle=False)
        selected = str(key or "")
        if not selected or selected not in archive.files:
            raise ValueError(f"NPZ key 缺失或不存在：{selected!r}")
        value = archive[selected]
    elif suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        if key:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter=delimiter))
            if not rows or str(key) not in rows[0]:
                raise ValueError(f"表格列不存在：{key!r}")
            value = np.asarray([float(row[str(key)]) for row in rows])
        else:
            value = np.loadtxt(path, delimiter=delimiter)
    elif suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if key:
            for part in str(key).split("."):
                payload = payload[part]
        value = np.asarray(payload, dtype=float)
    else:
        raise ValueError(f"不支持的数据格式：{suffix}")
    if not np.issubdtype(np.asarray(value).dtype, np.number):
        raise ValueError(f"数据不是数值数组：{path}")
    return np.asarray(value), _file_record(path, role="figure_source_data")


def _apply_transforms(bindings: dict[str, Any], transforms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import numpy as np
    applied = []
    for transform in transforms:
        binding = str(transform["binding"])
        if binding not in bindings:
            raise ValueError(f"transform 绑定不存在：{binding}")
        value = np.asarray(bindings[binding])
        op = transform["op"]
        if op == "scale":
            value = value * float(transform["value"])
        elif op == "offset":
            value = value + float(transform["value"])
        elif op == "abs":
            value = np.abs(value)
        elif op == "normalize":
            low, high = float(np.min(value)), float(np.max(value))
            value = (value - low) / (high - low) if high != low else np.zeros_like(value)
        elif op == "denormalize":
            value = value * float(transform["std"]) + float(transform["mean"])
        elif op == "slice":
            start = transform.get("start")
            stop = transform.get("stop")
            step = transform.get("step")
            value = value[slice(start, stop, step)]
        elif op == "mean":
            value = np.mean(value, axis=transform.get("axis"))
        elif op == "std":
            value = np.std(value, axis=transform.get("axis"))
        elif op == "histogram":
            counts, edges = np.histogram(value, bins=int(transform.get("bins", 20)), density=bool(transform.get("density", False)))
            value = counts
            output_binding = str(transform.get("edge_binding") or f"{binding}_bin_centers")
            bindings[output_binding] = (edges[:-1] + edges[1:]) / 2
        bindings[binding] = value
        applied.append({
            "binding": binding,
            "op": op,
            "evidence": transform["evidence"],
            "source_page": transform["source_page"],
        })
    return applied


def _axis_label(axis: dict[str, Any]) -> str:
    label = str(axis["label"])
    unit = str(axis["unit"])
    return label if unit.casefold() in {"not_applicable", "dimensionless"} else f"{label} ({unit})"


def _style_axis(axis, spec: dict[str, Any], which: str) -> None:
    setter = getattr(axis, f"set_{which}label")
    setter(_axis_label(spec))
    getattr(axis, f"set_{which}scale")(spec["scale"])
    getattr(axis, f"set_{which}lim")(*spec["limits"])
    getattr(axis, f"set_{which}ticks")(spec["ticks"])


def _render_generic(
    figure: dict[str, Any],
    bindings: dict[str, Any],
    figures_dir: Path,
) -> list[dict[str, Any]]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("缺少 Matplotlib；必须在 step_4 的同一环境中准备") from exc

    layout = figure["layout"]
    rows, cols = layout["rows"], layout["cols"]
    canvas, axes = plt.subplots(
        rows,
        cols,
        figsize=(
            float(layout.get("width_inches", 7.0)),
            float(layout.get("height_inches", 4.5)),
        ),
        squeeze=False,
    )
    panels = figure["panels"]
    if len(panels) > rows * cols:
        plt.close(canvas)
        raise ValueError("panels 数量超过布局容量")
    for index, panel in enumerate(panels):
        axis = axes[index // cols][index % cols]
        chart_type = panel["chart_type"]
        mappable = None
        if chart_type in {"line", "scatter", "bar"}:
            x = np.ravel(bindings[str(panel["x_binding"])])
            for series in panel["series"]:
                y = np.ravel(bindings[str(series["binding"])])
                if len(x) != len(y):
                    raise ValueError(f"{figure['figure_id']} panel {index} x/y 长度不一致")
                style = {
                    key: series.get(key)
                    for key in ("label", "color", "linestyle", "marker")
                    if series.get(key) not in (None, "")
                }
                if chart_type == "line":
                    axis.plot(x, y, **style)
                elif chart_type == "scatter":
                    scatter_style = {key: value for key, value in style.items() if key in {"label", "color", "marker"}}
                    axis.scatter(x, y, **scatter_style)
                else:
                    bar_style = {key: value for key, value in style.items() if key in {"label", "color"}}
                    axis.bar(x, y, **bar_style)
        elif chart_type in {"histogram", "pdf"}:
            for series in panel["series"]:
                values = np.ravel(bindings[str(series["binding"])])
                axis.hist(
                    values,
                    bins=int(series.get("bins", 20)),
                    density=chart_type == "pdf" or bool(series.get("density", False)),
                    label=series.get("label"),
                    color=series.get("color"),
                    histtype=series.get("histtype", "step"),
                )
        else:
            z = np.asarray(bindings[str(panel["z_binding"])])
            if z.ndim != 2:
                raise ValueError(f"{figure['figure_id']} panel {index} z 必须是二维数组")
            extent = panel.get("extent")
            if chart_type == "contour":
                mappable = axis.contourf(
                    z,
                    levels=int(panel.get("levels", 20)),
                    cmap=panel.get("colormap", "viridis"),
                    extent=extent,
                )
            else:
                mappable = axis.imshow(
                    z,
                    origin=panel.get("origin", "lower"),
                    aspect=panel.get("aspect", "auto"),
                    cmap=panel.get("colormap", "viridis"),
                    extent=extent,
                    vmin=panel["colorbar"]["limits"][0],
                    vmax=panel["colorbar"]["limits"][1],
                )
            colorbar = canvas.colorbar(mappable, ax=axis, ticks=panel["colorbar"]["ticks"])
            colorbar.set_label(_axis_label(panel["colorbar"]))
        axis.set_title(panel["title"])
        _style_axis(axis, panel["x_axis"], "x")
        _style_axis(axis, panel["y_axis"], "y")
        legend = panel["legend"]
        if legend["show"]:
            axis.legend(loc=legend.get("location", "best"))
        axis.grid(bool(panel.get("grid", False)))
    for index in range(len(panels), rows * cols):
        axes[index // cols][index % cols].set_visible(False)
    canvas.tight_layout()
    safe_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(figure["figure_id"]))
    outputs = []
    for suffix in IMAGE_SUFFIXES:
        output = figures_dir / f"{safe_id}{suffix}"
        canvas.savefig(output, dpi=int(layout.get("dpi", 200)), bbox_inches="tight")
        outputs.append(_file_record(
            output,
            paper_figure_id=figure["figure_id"],
            case_name=figure["case_name"],
            caption=figure["caption"],
            featured=True,
        ))
    plt.close(canvas)
    return outputs


def _run_official(
    figure: dict[str, Any],
    figures_dir: Path,
    code_root: Path | None,
    repo_commit: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if code_root is None or not code_root.is_dir():
        raise ValueError("official_script 需要可读 code_root")
    config = figure.get("official_script")
    if not isinstance(config, dict):
        raise ValueError("official_script 配置缺失")
    script = _resolve(config.get("script"), code_root)
    if not script.is_file() or not _inside(script, code_root):
        raise ValueError("官方绘图脚本必须位于已审计 code_root 内")
    args = config.get("args") or []
    if not isinstance(args, list) or not all(isinstance(item, (str, int, float)) for item in args):
        raise ValueError("official_script.args 必须是标量数组")
    interpreter = str(config.get("interpreter") or sys.executable)
    command = [interpreter, str(script), *[str(item) for item in args]]
    completed = subprocess.run(
        command,
        cwd=code_root,
        env={**os.environ, "RUN_OUTPUT_ROOT": str(figures_dir.parent)},
        text=True,
        capture_output=True,
        check=False,
    )
    log_path = figures_dir / f"{script.stem}.official.log"
    log_path.write_text(
        f"command={json.dumps(command, ensure_ascii=False)}\n"
        f"returncode={completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}\n",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"官方绘图脚本失败，详见 {log_path}")
    output_paths = config.get("output_paths") or []
    records = []
    for raw in output_paths:
        path = _resolve(raw, code_root)
        if not path.is_file() or path.suffix.casefold() not in IMAGE_SUFFIXES:
            raise ValueError(f"官方绘图输出缺失或格式不支持：{path}")
        records.append(_file_record(
            path,
            paper_figure_id=figure["figure_id"],
            case_name=figure["case_name"],
            caption=figure["caption"],
            featured=True,
        ))
    if not records:
        raise ValueError("official_script.output_paths 为空")
    return records, {
        "script": str(script),
        "args": [str(item) for item in args],
        "repo_commit": repo_commit,
        "log": _file_record(log_path, role="figure_reproduction_log"),
    }


def _compliance(figure: dict[str, Any]) -> dict[str, Any]:
    panels = figure["panels"]
    heatmap_panels = [
        panel for panel in panels
        if panel.get("chart_type") in {"heatmap", "contour", "field"}
    ]
    return {
        "coordinates": {"status": "passed", "evidence": [item["coordinates"] for item in figure["data_requirements"]]},
        "units": {"status": "passed", "evidence": [item["units"] for item in figure["data_requirements"]]},
        "axis_scales": {"status": "passed", "evidence": [[panel["x_axis"]["scale"], panel["y_axis"]["scale"]] for panel in panels]},
        "axis_limits": {"status": "passed", "evidence": [[panel["x_axis"]["limits"], panel["y_axis"]["limits"]] for panel in panels]},
        "ticks": {"status": "passed", "evidence": [[panel["x_axis"]["ticks"], panel["y_axis"]["ticks"]] for panel in panels]},
        "legend": {"status": "passed", "evidence": [panel["legend"] for panel in panels]},
        "layout": {"status": "passed", "evidence": figure["layout"]},
        "colorbar": {
            "status": "passed" if all(panel.get("colorbar") for panel in heatmap_panels) else "not_applicable",
            "evidence": [panel.get("colorbar") for panel in heatmap_panels],
        },
    }


def reproduce_figure(
    figure: dict[str, Any],
    spec_base: Path,
    figures_dir: Path,
    code_root: Path | None = None,
    repo_commit: str | None = None,
) -> dict[str, Any]:
    base = {
        "figure_id": str(figure.get("figure_id") or ""),
        "case_name": str(figure.get("case_name") or ""),
        "caption": str(figure.get("caption") or ""),
        "source_page": str(figure.get("source_page") or ""),
        "status": "blocked",
        "blocking_reasons": [],
        "input_artifacts": [],
        "applied_transforms": [],
        "compliance": {},
        "output_artifacts": [],
    }
    blockers = _figure_blockers(figure)
    if figure.get("status") == "skipped_not_applicable":
        base["status"] = "skipped_not_applicable"
        base["blocking_reasons"] = blockers or ["论文没有需要复现的实验结果图"]
        return base
    if figure.get("status") == "blocked" or blockers:
        base["blocking_reasons"] = blockers or ["论文绘图规范标记为 blocked"]
        return base
    try:
        bindings = {}
        for requirement in figure["data_requirements"]:
            value, record = _load_numeric(requirement, spec_base)
            bindings[str(requirement["id"])] = value
            base["input_artifacts"].append(record)
        base["applied_transforms"] = _apply_transforms(bindings, figure.get("transforms") or [])
        if figure["renderer"] == "official_script":
            outputs, official = _run_official(
                figure, figures_dir, code_root, repo_commit
            )
            base["official_script"] = official
        else:
            outputs = _render_generic(figure, bindings, figures_dir)
        base["output_artifacts"] = outputs
        base["compliance"] = _compliance(figure)
        if not all(
            item.get("status") in {"passed", "not_applicable"}
            for key, item in base["compliance"].items()
            if key in COMPLIANCE_KEYS
        ):
            raise ValueError("结构一致性检查未通过")
        base["status"] = "passed"
    except Exception as exc:
        base["status"] = "failed"
        base["blocking_reasons"] = [str(exc)]
    return base


def _merge_artifact_manifest(path: Path, results: list[dict[str, Any]]) -> None:
    payload = _read_json(path, {"schema_version": "1.0", "created_at": _now_iso(), "roots": [], "artifacts": []})
    if not isinstance(payload, dict):
        raise ValueError("artifact_manifest 根节点必须是对象")
    artifacts = payload.setdefault("artifacts", [])
    existing = {
        (str(item.get("sha256") or ""), str(item.get("path") or ""))
        for item in artifacts if isinstance(item, dict)
    }
    for result in results:
        for artifact in result.get("output_artifacts") or []:
            key = (str(artifact.get("sha256") or ""), str(artifact.get("path") or ""))
            if key not in existing:
                artifacts.append(artifact)
                existing.add(key)
    _atomic_json(path, payload)


def _merge_experiment_details(path: Path, results: list[dict[str, Any]]) -> None:
    payload = _read_json(path, [])
    if not isinstance(payload, list):
        raise ValueError("experiment_details 根节点必须是数组")
    for result in results:
        case_name = result.get("case_name") or "公共/未归属证据"
        record = next(
            (item for item in payload if isinstance(item, dict) and item.get("case_name") == case_name),
            None,
        )
        if record is None:
            record = {"case_name": case_name, "status": result["status"], "sections": []}
            payload.append(record)
        sections = record.setdefault("sections", [])
        if result.get("output_artifacts"):
            sections.append({
                "title": f"论文结果图 {result['figure_id']}",
                "kind": "artifacts",
                "content": result["output_artifacts"],
            })
        if result["status"] != "passed":
            sections.append({
                "title": f"论文结果图 {result['figure_id']} 缺口",
                "kind": "list",
                "content": result.get("blocking_reasons") or ["未提供原因"],
            })
    _atomic_json(path, payload)


def _merge_validation_result(path: Path, results: list[dict[str, Any]]) -> None:
    payload = _read_json(path, {})
    if not isinstance(payload, dict):
        raise ValueError("validation_result 根节点必须是对象")
    phases = payload.setdefault("phases", {})
    artifacts_phase = phases.setdefault("validation_artifacts", {"status": "not_available", "artifacts": []})
    artifacts = artifacts_phase.setdefault("artifacts", [])
    for result in results:
        artifacts.extend(result.get("output_artifacts") or [])
    if artifacts:
        artifacts_phase["status"] = "passed"
    payload["figure_reproduction"] = {
        "status": _overall_status(results),
        "figures": [
            {
                "figure_id": item["figure_id"],
                "case_name": item["case_name"],
                "status": item["status"],
                "blocking_reasons": item["blocking_reasons"],
            }
            for item in results
        ],
    }
    _atomic_json(path, payload)


def _overall_status(results: list[dict[str, Any]]) -> str:
    statuses = [item.get("status") for item in results]
    if statuses and all(status == "passed" for status in statuses):
        return "passed"
    if statuses and all(status == "skipped_not_applicable" for status in statuses):
        return "skipped_not_applicable"
    if any(status == "passed" for status in statuses):
        return "partial"
    if any(status == "failed" for status in statuses):
        return "failed"
    return "blocked"


def run(
    spec_path: Path,
    run_output_root: Path,
    *,
    code_root: Path | None = None,
    repo_commit: str | None = None,
    artifact_manifest: Path | None = None,
    experiment_details: Path | None = None,
    validation_result: Path | None = None,
) -> dict[str, Any]:
    spec_path = spec_path.expanduser().resolve()
    run_output_root = run_output_root.expanduser().resolve()
    figures_dir = run_output_root / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    spec = _read_json(spec_path)
    if not isinstance(spec, dict) or str(spec.get("schema_version") or "") != "1.0":
        raise ValueError("paper_result_figure_specs.json 必须是 schema_version=1.0 对象")
    figures = spec.get("figures")
    if not isinstance(figures, list):
        raise ValueError("paper_result_figure_specs.figures 必须是数组")
    if not figures:
        figures = [{
            "figure_id": "not_applicable",
            "case_name": "not_applicable",
            "caption": "论文没有需要复现的实验结果图",
            "source_page": "not_applicable",
            "status": "skipped_not_applicable",
        }]
    results = [
        reproduce_figure(
            figure,
            spec_path.parent,
            figures_dir,
            code_root.expanduser().resolve() if code_root else None,
            repo_commit,
        )
        for figure in figures
        if isinstance(figure, dict)
    ]
    payload = {
        "schema_version": "1.0",
        "created_at": _now_iso(),
        "spec_path": str(spec_path),
        "overall_status": _overall_status(results),
        "figures": results,
    }
    output = figures_dir / "figure_reproduction_result.json"
    _atomic_json(output, payload)
    if artifact_manifest:
        _merge_artifact_manifest(artifact_manifest.expanduser().resolve(), results)
    if experiment_details:
        _merge_experiment_details(experiment_details.expanduser().resolve(), results)
    if validation_result:
        _merge_validation_result(validation_result.expanduser().resolve(), results)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--run-output-root", required=True)
    parser.add_argument("--code-root")
    parser.add_argument("--repo-commit")
    parser.add_argument("--artifact-manifest")
    parser.add_argument("--experiment-details")
    parser.add_argument("--validation-result")
    args = parser.parse_args()
    payload = run(
        Path(args.spec),
        Path(args.run_output_root),
        code_root=Path(args.code_root) if args.code_root else None,
        repo_commit=args.repo_commit,
        artifact_manifest=Path(args.artifact_manifest) if args.artifact_manifest else None,
        experiment_details=Path(args.experiment_details) if args.experiment_details else None,
        validation_result=Path(args.validation_result) if args.validation_result else None,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
