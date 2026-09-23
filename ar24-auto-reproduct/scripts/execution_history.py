#!/usr/bin/env python3
"""Persist and validate multi-run reproduction history for the AR24 pipeline."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "2.0"
LAYOUT_VERSION = 3
LEGACY_SCHEMA_VERSIONS = {"1.0", "1.1"}
RUN_STATUSES = {
    "reserved",
    "planned",
    "running",
    "passed",
    "partial",
    "failed",
    "skipped",
}
TERMINAL_STATUSES = {"passed", "partial", "failed", "skipped"}
STEP_NAMES = (
    "step_precheck",
    "step_0",
    "step_1",
    "step_2",
    "step_2_5",
    "step_3",
    "step_4",
    "step_5",
    "step_6",
    "step_7",
    "step_7_5",
    "step_8",
)
CONTROL_STEPS = ("step_precheck", "step_0", "step_1", "step_2", "step_2_5")
RUN_ID_RE = re.compile(r"^run-(\d{3,})$")
PREPARATION_STEPS = ("step_3", "step_4", "step_5", "step_6")
PREPARATION_STATUSES = {
    "completed",
    "reused",
    "skipped_not_required",
    "failed",
    "not_reached",
}
READY_PREPARATION_STATUSES = {"completed", "reused", "skipped_not_required"}
VALIDATION_PHASES = (
    "validation_preflight",
    "reference_acquisition",
    "prediction_acquisition",
    "field_alignment",
    "quantitative_validation",
    "physics_validation",
    "validation_artifacts",
)
REPORT_DECISIONS = {
    "generate_report",
    "continue_existing",
    "custom_execution",
    "report_requirement",
}
REPORT_DECISION_PROMPT = (
    "请选择下一步（必须由用户本人明确回复，沉默、超时或时间不足不代表授权）：\n"
    "1. 直接生成最终 DOCX\n"
    "2. 继续其他已有候选计划\n"
    "3. 输入其他要求（请在 3 后写明要求）"
)
REPORT_DECISION_INSTRUCTION = (
    "把 board、prompt 与 confirmation_id 原样展示给用户并立即结束当前回复；"
    "没有用户的新回复时不得调用 decide，也不得进入 step_8 或生成任何最终报告。"
)
REQUIRED_AUDIT_DIMENSIONS = {
    "documentation_entry",
    "environment",
    "artifacts",
    "migration_cost",
    "experiment_reproduction",
    "physics_fidelity",
    "risk_gate",
}


class HistoryValidationError(ValueError):
    """Raised when a history ledger cannot safely drive execution or reporting."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    text = str(value).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(text)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = _now_iso()
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _atomic_write_payload(path: Path, payload: Any) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def new_history(project_name: str) -> dict[str, Any]:
    timestamp = _now_iso()
    normalized_project_name = str(project_name).strip()
    return {
        "schema_version": SCHEMA_VERSION,
        "layout_version": LAYOUT_VERSION,
        "project_name": normalized_project_name,
        "created_at": timestamp,
        "updated_at": timestamp,
        "runs": [],
        "report_requirements": [],
        "final_artifacts": {
            "audit_report": {
                "path": None,
                "required": True,
                "format": "markdown",
            },
            "reproduction_report": {
                "path": None,
                "required": True,
                "format": "docx",
            },
        },
        "report_gate": {
            "status": "not_ready",
            "decision": None,
            "requested_at": None,
            "prompt_displayed_at": None,
            "decided_at": None,
            "confirmation_id": None,
            "decision_source": None,
            "user_response": None,
            "user_request": None,
        },
    }


def _canonical_audit_report_path(
    history: dict[str, Any],
    artifact_base_dir: str | Path,
) -> Path:
    project_name = str(history.get("project_name") or "").strip()
    if (
        not project_name
        or project_name in {".", ".."}
        or Path(project_name).name != project_name
    ):
        raise HistoryValidationError("project_name 无效，无法定位规范审计报告")
    runs = history.get("runs") if isinstance(history.get("runs"), list) else []
    gate = history.get("report_gate") if isinstance(history.get("report_gate"), dict) else {}
    gate_run_id = str(gate.get("run_id") or "").strip()
    candidates = [
        run
        for run in runs
        if isinstance(run, dict) and run.get("status") in TERMINAL_STATUSES
    ]
    selected = next(
        (run for run in candidates if run.get("run_id") == gate_run_id),
        candidates[-1] if candidates else None,
    )
    if not isinstance(selected, dict):
        raise HistoryValidationError("没有可定位审计报告的终态 run")
    configured = str(selected.get("audit_report") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(artifact_base_dir).expanduser().resolve() / path
        return path.resolve()
    run_id = str(selected.get("run_id") or "").strip()
    return (
        Path(artifact_base_dir).expanduser().resolve()
        / run_id
        / "step_1"
        / f"{project_name}_Audit_Report.md"
    )


def _validate_required_audit_report(
    history: dict[str, Any],
    artifact_base_dir: str | Path,
) -> tuple[Path, str | None]:
    try:
        audit_path = _canonical_audit_report_path(history, artifact_base_dir)
    except HistoryValidationError as exc:
        return Path(artifact_base_dir), str(exc)
    if not audit_path.is_file():
        return audit_path, f"最终产物缺少规范审计报告：{audit_path}"
    try:
        content = audit_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return audit_path, f"规范审计报告不可读：{audit_path}（{exc}）"
    if not content.strip():
        return audit_path, f"规范审计报告为空：{audit_path}"
    score_path = audit_path.parent / "audit_score.json"
    if not score_path.is_file():
        return audit_path, f"规范审计报告缺少配套机器评分：{score_path}"
    try:
        scorecard = json.loads(score_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return audit_path, f"配套机器评分不可读或不是有效 JSON：{score_path}（{exc}）"
    resource_gate = scorecard.get("resource_gate") if isinstance(scorecard, dict) else None
    physics_gate = scorecard.get("physics_gate") if isinstance(scorecard, dict) else None
    overall_score = scorecard.get("overall_score") if isinstance(scorecard, dict) else None
    if (
        not isinstance(scorecard, dict)
        or scorecard.get("scoring_profile") != "sciml_physics"
        or not isinstance(overall_score, (int, float))
        or isinstance(overall_score, bool)
        or not 0 <= overall_score <= 100
        or not str(scorecard.get("verdict") or "").strip()
        or not REQUIRED_AUDIT_DIMENSIONS.issubset(scorecard.get("dimensions") or {})
        or not isinstance(resource_gate, dict)
        or not {"fit_status", "action", "recommended_resource", "message"}.issubset(resource_gate)
        or not isinstance(physics_gate, dict)
        or not {"action", "execution_blockers", "claim_blockers", "message"}.issubset(physics_gate)
    ):
        return audit_path, f"配套机器评分缺少 SciML/计算物理 v4 必需字段：{score_path}"
    for name, dimension in scorecard["dimensions"].items():
        if not isinstance(dimension, dict):
            return audit_path, f"机器评分维度 {name} 不是对象：{score_path}"
        score = dimension.get("score", dimension.get("earned"))
        maximum = dimension.get("max", dimension.get("max_score"))
        if (
            isinstance(score, (int, float))
            and isinstance(maximum, (int, float))
            and score < maximum
            and not (
                dimension.get("deductions")
                or dimension.get("evidence")
                or dimension.get("assessment")
                or dimension.get("message")
            )
        ):
            return audit_path, f"机器评分维度 {name} 存在扣分但缺少证据：{score_path}"
    return audit_path, None


def _normalized_confirmation_response(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return "".join(text.split()).rstrip(".。!")


def _decision_from_user_response(value: Any) -> str | None:
    normalized = _normalized_confirmation_response(value)
    if normalized in {
        "1",
        "1直接生成最终docx",
        "直接生成最终docx",
        "生成最终docx",
        "生成docx",
        "输出docx",
    }:
        return "generate_report"
    if normalized in {
        "2",
        "2继续其他已有候选计划",
        "继续其他已有候选计划",
        "继续其他计划",
        "继续执行",
    }:
        return "continue_existing"
    if normalized == "3" or normalized.startswith(("3:", "3：", "3输入其他要求")):
        return "custom"
    return None


def _new_confirmation_gate(
    status: str = "awaiting_user",
    run_id: str | None = None,
) -> dict[str, Any]:
    timestamp = _now_iso()
    return {
        "status": status,
        "decision": None,
        "requested_at": timestamp,
        "prompt_displayed_at": timestamp if status == "awaiting_user" else None,
        "decided_at": None,
        "confirmation_id": secrets.token_hex(8),
        "decision_source": None,
        "user_response": None,
        "user_request": None,
        "run_id": run_id,
    }


def _cci_board_detail(run: dict[str, Any]) -> str:
    cloud = run.get("cloud_resources")
    if not isinstance(cloud, dict):
        return "cci / ⚠缺少云资源证据"
    resources = cloud.get("resources")
    if not isinstance(resources, dict):
        return "cci / ⚠缺少实例资源证据"
    execution = resources.get("execution")
    if not isinstance(execution, dict):
        return "cci / ⚠缺少 GPU 执行实例证据"
    instance_id = execution.get("instance_id") or "未创建"
    price = execution.get("price")
    price_unit = execution.get("price_unit") or ""
    lifecycle = execution.get("lifecycle_status") or "未知"
    price_text = f"{price} {price_unit}".strip() if price not in (None, "") else "价格未提供"
    warning = "⚠仍在计费" if lifecycle == "running" else f"状态={lifecycle}"
    state_path = cloud.get("state_path") or "<cci_state.json>"
    return (
        f"cci / GPU={instance_id} / {price_text} / {warning} / "
        f"清理: cci_manager.py --state {state_path} release --role execution"
    )


def _report_decision_board(history: dict[str, Any], run: dict[str, Any]) -> str:
    project_name = str(history.get("project_name") or "未命名项目").strip()
    run_status = str(run.get("status") or "").strip().casefold()
    if run_status == "passed":
        execution_status = "✅完成"
    elif run_status == "partial":
        execution_status = "✅部分完成"
    else:
        execution_status = "❌中止"
    run_detail = f"{run.get('run_id', 'unknown')} / {run_status or 'unknown'}"
    preparation = run.get("preparation") if isinstance(run.get("preparation"), dict) else {}
    summary = (
        run.get("terminal_summary")
        if isinstance(run.get("terminal_summary"), dict)
        else {}
    )
    terminal_stage = str(summary.get("stage") or "").strip()
    terminal_reason = str(summary.get("reason") or "本轮在此终止").strip()

    def control_state(step: str, completed_detail: str) -> tuple[str, str]:
        if terminal_stage not in CONTROL_STEPS:
            return "✅完成", completed_detail
        terminal_index = CONTROL_STEPS.index(terminal_stage)
        step_index = CONTROL_STEPS.index(step)
        if step_index < terminal_index:
            return "✅完成", completed_detail
        if step_index == terminal_index:
            return (
                "⏭跳过" if run_status == "skipped" else "❌中止",
                terminal_reason,
            )
        return "⏸未到达", f"本轮止于 {terminal_stage}"

    def preparation_state(step: str) -> tuple[str, str]:
        record = preparation.get(step) if isinstance(preparation, dict) else None
        status = str(record.get("status") or "").strip().casefold() if isinstance(record, dict) else ""
        if status in {"completed", "reused", "skipped_not_required"}:
            return "✅完成", status
        if status in {"failed", "blocked"}:
            return "❌中止", status
        if run_status in {"passed", "partial"}:
            return "✅完成", "由本轮终态确认"
        return "⏸未到达", "台账未记录该准备步骤终态"

    preparation_rows = {
        step: preparation_state(step)
        for step in PREPARATION_STEPS
    }
    backend_detail = (
        _cci_board_detail(run)
        if str(run.get("backend") or "").casefold() == "cci"
        else str(run.get("backend") or "状态见执行记录")
    )
    step_7_state = (
        ("⏸未到达", f"本轮止于 {terminal_stage}")
        if terminal_stage in CONTROL_STEPS or terminal_stage in PREPARATION_STEPS
        else (execution_status, run_detail)
    )
    rows = [
        ("P", "step_precheck 公共复现仓库预检", *control_state("step_precheck", "状态见执行记录")),
        ("0", "step_0 后端选择+算力探测+自检", *control_state("step_0", backend_detail)),
        ("1", "step_1 双重审计", *control_state("step_1", "审计报告已归档到本轮 step_1")),
        ("2", "step_2 可行性熔断", *control_state("step_2", "状态见执行记录")),
        (
            "2.5",
            "step_2.5 执行计划选择",
            *control_state(
                "step_2_5",
                str(run.get("selected_option_id") or "已绑定完整计划"),
            ),
        ),
        ("3", "step_3 拉取代码", *preparation_rows["step_3"]),
        ("4", "step_4 项目分析+依赖准备", *preparation_rows["step_4"]),
        ("5", "step_5 数据下载", *preparation_rows["step_5"]),
        ("6", "step_6 模型权重下载", *preparation_rows["step_6"]),
        ("7", "step_7 编译+推理", *step_7_state),
        ("8", "step_8 生成报告", "⏸等待用户确认", "尚未获得 DOCX 授权"),
    ]
    lines = [
        f"#### 📊 复现流水线实时看板: {project_name}",
        "",
        "| 序号 | 执行步骤 | 当前状态 | 核心产出 / 详情 |",
        "| :--- | :--- | :--- | :--- |",
    ]
    lines.extend(f"| {number} | {step} | {status} | {detail} |" for number, step, status, detail in rows)
    lines.extend(["", "---"])
    return "\n".join(lines)


def _decision_envelope(
    history: dict[str, Any],
    run: dict[str, Any],
    *,
    compatibility_mode: bool = False,
) -> dict[str, Any]:
    gate = history["report_gate"]
    return {
        "run_id": run.get("run_id"),
        "run": run,
        "pause_required": True,
        "next_action": "await_user_report_decision",
        "board": _report_decision_board(history, run),
        "report_gate": gate,
        "status": gate["status"],
        "confirmation_id": gate["confirmation_id"],
        "prompt": REPORT_DECISION_PROMPT,
        "instruction": REPORT_DECISION_INSTRUCTION,
        "compatibility_mode": compatibility_mode,
    }


def _load_json_reference(value: Any, base_dir: str | Path | None) -> tuple[Any, str | None]:
    if isinstance(value, (dict, list)):
        return value, None
    text = str(value or "").strip()
    if not text:
        return None, "缺失"
    path = Path(text).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir) / path
    try:
        path = path.resolve()
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"不可读或不是有效 JSON：{path}（{exc}）"


def _selected_case_names(plan: Any) -> list[str]:
    if not isinstance(plan, dict):
        return []
    if isinstance(plan.get("selected_execution_plan"), dict):
        plan = plan["selected_execution_plan"]
    raw_items = plan.get("execution_items")
    if not isinstance(raw_items, list):
        return []
    names = []
    for item in raw_items:
        if isinstance(item, dict):
            name = str(item.get("case_name") or item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _result_figure_contract(plan: Any) -> tuple[bool, list[str]]:
    if not isinstance(plan, dict):
        return False, []
    if isinstance(plan.get("selected_execution_plan"), dict):
        plan = plan["selected_execution_plan"]
    if "result_figures" not in plan:
        return False, []
    raw = plan.get("result_figures")
    if not isinstance(raw, list):
        return True, []
    identifiers = []
    for item in raw:
        if isinstance(item, dict):
            identifier = str(item.get("figure_id") or item.get("id") or "").strip()
        else:
            identifier = str(item or "").strip()
        if identifier and identifier not in identifiers:
            identifiers.append(identifier)
    return True, identifiers


def _validate_figure_file_record(
    item: Any,
    prefix: str,
    base_dir: str | Path | None,
) -> list[str]:
    if not isinstance(item, dict):
        return [f"{prefix} 必须是对象"]
    errors = []
    raw_path = str(item.get("path") or "").strip()
    if not raw_path:
        return [f"{prefix}.path 缺失"]
    path = Path(raw_path).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir) / path
    path = path.resolve()
    if not path.is_file():
        return [f"{prefix}.path 不存在：{path}"]
    size = item.get("size_bytes")
    digest = str(item.get("sha256") or "").strip()
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        errors.append(f"{prefix}.size_bytes 缺失或无效")
    elif size != path.stat().st_size:
        errors.append(f"{prefix}.size_bytes 与实际文件不一致")
    if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
        errors.append(f"{prefix}.sha256 缺失或无效")
    elif _sha256(path).casefold() != digest.casefold():
        errors.append(f"{prefix}.sha256 与实际文件不匹配")
    return errors


def _validate_figure_reproduction(
    value: Any,
    plan: Any,
    prefix: str,
    base_dir: str | Path | None,
    *,
    run_status: str,
) -> list[str]:
    declared, targets = _result_figure_contract(plan)
    if not declared:
        return []
    payload, error = _load_json_reference(value, base_dir)
    if error:
        return [f"{prefix}.figure_reproduction_result {error}"]
    if not isinstance(payload, dict):
        return [f"{prefix}.figure_reproduction_result 必须是对象"]
    errors = []
    if str(payload.get("schema_version") or "") != "1.0":
        errors.append(f"{prefix}.figure_reproduction_result.schema_version 必须是 1.0")
    results = payload.get("figures")
    if not isinstance(results, list) or not results:
        return errors + [f"{prefix}.figure_reproduction_result.figures 为空"]
    by_id = {}
    for index, result in enumerate(results):
        item_prefix = f"{prefix}.figure_reproduction_result.figures[{index}]"
        if not isinstance(result, dict):
            errors.append(f"{item_prefix} 必须是对象")
            continue
        figure_id = str(result.get("figure_id") or "").strip()
        status = str(result.get("status") or "").strip().casefold()
        if not figure_id:
            errors.append(f"{item_prefix}.figure_id 缺失")
        elif figure_id in by_id:
            errors.append(f"{item_prefix}.figure_id 重复：{figure_id}")
        else:
            by_id[figure_id] = result
        if status not in {"passed", "blocked", "failed", "skipped_not_applicable"}:
            errors.append(f"{item_prefix}.status 无效：{status or '缺失'}")
        if status == "passed":
            for collection in ("input_artifacts", "output_artifacts"):
                records = result.get(collection)
                if not isinstance(records, list) or not records:
                    errors.append(f"{item_prefix}.{collection} 为空")
                    continue
                for record_index, record in enumerate(records):
                    errors.extend(
                        _validate_figure_file_record(
                            record,
                            f"{item_prefix}.{collection}[{record_index}]",
                            base_dir,
                        )
                    )
            compliance = result.get("compliance")
            if not isinstance(compliance, dict):
                errors.append(f"{item_prefix}.compliance 缺失")
            else:
                for name in (
                    "coordinates", "units", "axis_scales", "axis_limits",
                    "ticks", "legend", "layout", "colorbar",
                ):
                    check = compliance.get(name)
                    if not isinstance(check, dict) or check.get("status") not in {
                        "passed", "not_applicable",
                    }:
                        errors.append(f"{item_prefix}.compliance.{name} 未通过")
        elif not result.get("blocking_reasons"):
            errors.append(f"{item_prefix}.blocking_reasons 为空")
    if targets:
        missing = [figure_id for figure_id in targets if figure_id not in by_id]
        if missing:
            errors.append(
                f"{prefix}.figure_reproduction_result 缺少目标图：{', '.join(missing)}"
            )
        if run_status == "passed":
            not_passed = [
                figure_id for figure_id in targets
                if figure_id in by_id and by_id[figure_id].get("status") != "passed"
            ]
            if not_passed:
                errors.append(
                    f"{prefix}.figure_reproduction_result 目标图未全部通过，"
                    f"本轮不得标记 passed：{', '.join(not_passed)}"
                )
    elif (
        payload.get("overall_status") != "skipped_not_applicable"
        or not all(
            isinstance(item, dict) and item.get("status") == "skipped_not_applicable"
            for item in results
        )
    ):
        errors.append(
            f"{prefix}.figure_reproduction_result 无目标图时必须显式 skipped_not_applicable"
        )
    return errors


def _validate_preparation(
    run: dict[str, Any],
    prefix: str,
    *,
    require_ready: bool,
) -> list[str]:
    errors = []
    preparation = run.get("preparation")
    if not isinstance(preparation, dict):
        preparation = {}
    for step in PREPARATION_STEPS:
        item_prefix = f"{prefix}.preparation.{step}"
        record = preparation.get(step)
        if not isinstance(record, dict):
            errors.append(f"{item_prefix} 缺失")
            continue
        status = str(record.get("status") or "").strip().casefold()
        if status not in PREPARATION_STATUSES:
            errors.append(f"{item_prefix}.status 无效：{status or '缺失'}")
        elif require_ready and status not in READY_PREPARATION_STATUSES:
            errors.append(f"{item_prefix}.status={status}，不能进入成功或部分完成终态")
        if not str(record.get("fingerprint") or "").strip():
            errors.append(f"{item_prefix}.fingerprint 缺失")
        evidence = record.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{item_prefix}.evidence 为空")
        if status in {"skipped_not_required", "failed", "not_reached"} and not str(
            record.get("reason") or ""
        ).strip():
            errors.append(f"{item_prefix}.reason 缺失")
        if not str(record.get("started_at") or record.get("recorded_at") or "").strip():
            errors.append(f"{item_prefix}.started_at 缺失")
        if not str(record.get("ended_at") or record.get("recorded_at") or "").strip():
            errors.append(f"{item_prefix}.ended_at 缺失")
        if str(record.get("backend") or run.get("backend") or "").strip() not in {"local", "ssh", "cci"}:
            errors.append(f"{item_prefix}.backend 必须是 local、ssh 或 cci")
    return errors


def _validate_outcomes(run: dict[str, Any], prefix: str) -> list[str]:
    outcomes = run.get("actual_test_outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        return [f"{prefix}.actual_test_outcomes 为空"]
    errors = []
    outcome_names = []
    for index, item in enumerate(outcomes):
        item_prefix = f"{prefix}.actual_test_outcomes[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_prefix} 必须是对象")
            continue
        name = str(item.get("case_name") or item.get("name") or "").strip()
        if not name:
            errors.append(f"{item_prefix}.case_name 缺失")
        else:
            outcome_names.append(name)
        if not str(item.get("status") or "").strip():
            errors.append(f"{item_prefix}.status 缺失")
        if not str(item.get("output_detail") or item.get("summary") or "").strip():
            errors.append(f"{item_prefix}.output_detail 缺失")
    selected_names = _selected_case_names(run.get("selected_execution_plan"))
    duplicates = sorted({name for name in outcome_names if outcome_names.count(name) > 1})
    if duplicates:
        errors.append(f"{prefix}.actual_test_outcomes 案例重复：{', '.join(duplicates)}")
    if not selected_names:
        errors.append(f"{prefix}.selected_execution_plan.execution_items 为空")
    else:
        missing = [name for name in selected_names if name not in outcome_names]
        extra = [name for name in outcome_names if name not in selected_names]
        if missing:
            errors.append(f"{prefix}.actual_test_outcomes 缺少计划案例：{', '.join(missing)}")
        if extra:
            errors.append(f"{prefix}.actual_test_outcomes 包含未选择案例：{', '.join(extra)}")
    return errors


def _validate_passed_outcome_statuses(run: dict[str, Any], prefix: str) -> list[str]:
    errors = []
    accepted = {"passed", "completed", "success", "succeeded", "ok"}
    for index, item in enumerate(run.get("actual_test_outcomes") or []):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().casefold()
        if status not in accepted:
            errors.append(
                f"{prefix}.actual_test_outcomes[{index}].status={status or '缺失'}，"
                "不能用于 passed 终态"
            )
    return errors


def _validate_compute_usage(value: Any, prefix: str, base_dir: str | Path | None) -> list[str]:
    payload, error = _load_json_reference(value, base_dir)
    if error:
        return [f"{prefix}.compute_usage {error}"]
    if not isinstance(payload, dict):
        return [f"{prefix}.compute_usage 必须是对象"]
    reproduction = payload.get("reproduction")
    if not isinstance(reproduction, dict):
        return [f"{prefix}.compute_usage.reproduction 缺失"]
    errors = []
    for key in ("end_to_end_hours", "step_7_hours"):
        number = reproduction.get(key)
        if not isinstance(number, (int, float)) or isinstance(number, bool) or number < 0:
            errors.append(f"{prefix}.compute_usage.reproduction.{key} 必须是非负数")
    phases = reproduction.get("phases")
    if not isinstance(phases, list) or not phases:
        errors.append(f"{prefix}.compute_usage.reproduction.phases 为空")
    else:
        for index, phase in enumerate(phases):
            phase_prefix = f"{prefix}.compute_usage.reproduction.phases[{index}]"
            if not isinstance(phase, dict):
                errors.append(f"{phase_prefix} 必须是对象")
                continue
            for key in ("name", "status", "started_at", "ended_at", "resource_type", "device_model"):
                if not str(phase.get(key) or "").strip():
                    errors.append(f"{phase_prefix}.{key} 缺失")
            duration = phase.get("duration_hours")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
                errors.append(f"{phase_prefix}.duration_hours 必须是非负数")
    return errors


def _validate_artifact_manifest(value: Any, prefix: str, base_dir: str | Path | None) -> list[str]:
    payload, error = _load_json_reference(value, base_dir)
    if error:
        return [f"{prefix}.artifact_manifest {error}"]
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        return [f"{prefix}.artifact_manifest.artifacts 缺失"]
    if not payload["artifacts"]:
        return [f"{prefix}.artifact_manifest.artifacts 为空"]
    errors = []
    for index, artifact in enumerate(payload["artifacts"]):
        item_prefix = f"{prefix}.artifact_manifest.artifacts[{index}]"
        if not isinstance(artifact, dict):
            errors.append(f"{item_prefix} 必须是对象")
            continue
        path_text = str(artifact.get("path") or "").strip()
        if not path_text:
            errors.append(f"{item_prefix}.path 缺失")
            continue
        path = Path(path_text).expanduser()
        if not path.is_absolute() and base_dir is not None:
            path = Path(base_dir) / path
        path = path.resolve()
        if path.is_file():
            size = artifact.get("size_bytes")
            digest = str(artifact.get("sha256") or "").strip()
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                errors.append(f"{item_prefix}.size_bytes 缺失")
            elif size != path.stat().st_size:
                errors.append(
                    f"{item_prefix}.size_bytes 与实际文件不一致：记录 {size}，实际 {path.stat().st_size}"
                )
            if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
                errors.append(f"{item_prefix}.sha256 缺失或无效")
            elif _sha256(path).casefold() != digest.casefold():
                errors.append(f"{item_prefix}.sha256 与实际文件不匹配")
        elif path.is_dir():
            contents = artifact.get("contents_manifest")
            if not str(contents or "").strip():
                errors.append(f"{item_prefix}.contents_manifest 缺失（目录必须提供内容清单）")
            else:
                manifest_path = Path(str(contents)).expanduser()
                if not manifest_path.is_absolute() and base_dir is not None:
                    manifest_path = Path(base_dir) / manifest_path
                if not manifest_path.resolve().is_file():
                    errors.append(
                        f"{item_prefix}.contents_manifest 不存在：{manifest_path.resolve()}"
                    )
        else:
            errors.append(f"{item_prefix}.path 不存在：{path}")
    return errors


def _validate_validation_result(
    value: Any,
    prefix: str,
    base_dir: str | Path | None,
    selected_case_names: list[str] | None = None,
    require_complete: bool = False,
) -> list[str]:
    payload, error = _load_json_reference(value, base_dir)
    if error:
        return [f"{prefix}.validation_result {error}"]
    if not isinstance(payload, dict):
        return [f"{prefix}.validation_result 必须是对象"]
    errors = []
    phases = payload.get("phases")
    if not isinstance(phases, dict):
        errors.append(f"{prefix}.validation_result.phases 缺失")
        phases = {}
    for name in VALIDATION_PHASES:
        phase = phases.get(name)
        if not isinstance(phase, dict):
            errors.append(f"{prefix}.validation_result.phases.{name} 缺失")
        elif not str(phase.get("status") or "").strip():
            errors.append(f"{prefix}.validation_result.phases.{name}.status 缺失")
    claim = payload.get("claim_gate")
    if not isinstance(claim, dict) or not str(claim.get("achieved_level") or "").strip():
        errors.append(f"{prefix}.validation_result.claim_gate.achieved_level 缺失")
    selected_names = selected_case_names or _selected_case_names(
        payload.get("selected_execution_plan")
    )
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append(f"{prefix}.validation_result.cases 为空")
    else:
        case_names = {
            str(case.get("case_name") or "").strip()
            for case in cases
            if isinstance(case, dict)
        }
        if selected_names:
            missing = [name for name in selected_names if name not in case_names]
            if missing:
                errors.append(f"{prefix}.validation_result.cases 缺少案例：{', '.join(missing)}")
        for index, case in enumerate(cases):
            case_prefix = f"{prefix}.validation_result.cases[{index}]"
            if not isinstance(case, dict):
                errors.append(f"{case_prefix} 必须是对象")
                continue
            if not str(case.get("case_name") or "").strip():
                errors.append(f"{case_prefix}.case_name 缺失")
            result_path_text = str(case.get("validation_result_path") or "").strip()
            if not result_path_text:
                errors.append(f"{case_prefix}.validation_result_path 缺失")
            else:
                result_path = Path(result_path_text).expanduser()
                if not result_path.is_absolute() and base_dir is not None:
                    result_path = Path(base_dir) / result_path
                result_path = result_path.resolve()
                if not result_path.is_file():
                    errors.append(f"{case_prefix}.validation_result_path 不存在：{result_path}")
                else:
                    try:
                        case_result = json.loads(result_path.read_text(encoding="utf-8"))
                        if not isinstance(case_result, dict):
                            errors.append(f"{case_prefix}.validation_result_path 必须指向 JSON 对象")
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        errors.append(f"{case_prefix}.validation_result_path 不是可读 JSON：{result_path}")
            case_claim = case.get("claim_gate")
            if not isinstance(case_claim, dict) or not str(
                case_claim.get("achieved_level") or ""
            ).strip():
                errors.append(f"{case_prefix}.claim_gate.achieved_level 缺失")
    if require_complete:
        required_lists = (
            ("quantitative_validation", "metrics"),
            ("physics_validation", "checks"),
            ("validation_artifacts", "artifacts"),
        )
        for phase_name, field_name in required_lists:
            phase = phases.get(phase_name)
            values = phase.get(field_name) if isinstance(phase, dict) else None
            if not isinstance(values, list) or not values:
                errors.append(
                    f"{prefix}.validation_result.phases.{phase_name}.{field_name} 为空"
                )
        validation_artifacts = (
            phases.get("validation_artifacts", {}).get("artifacts")
            if isinstance(phases.get("validation_artifacts"), dict)
            else []
        )
        for index, artifact in enumerate(validation_artifacts or []):
            artifact_prefix = (
                f"{prefix}.validation_result.phases.validation_artifacts.artifacts[{index}]"
            )
            if not isinstance(artifact, dict):
                errors.append(f"{artifact_prefix} 必须是对象")
                continue
            path_text = str(artifact.get("path") or "").strip()
            artifact_path = Path(path_text).expanduser() if path_text else None
            if artifact_path is not None and not artifact_path.is_absolute() and base_dir is not None:
                artifact_path = Path(base_dir) / artifact_path
            if artifact_path is None or not artifact_path.resolve().is_file():
                errors.append(f"{artifact_prefix}.path 不存在")
                continue
            artifact_path = artifact_path.resolve()
            size = artifact.get("size_bytes")
            digest = str(artifact.get("sha256") or "").strip()
            if size != artifact_path.stat().st_size:
                errors.append(f"{artifact_prefix}.size_bytes 与实际文件不一致")
            if (
                len(digest) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in digest)
                or _sha256(artifact_path).casefold() != digest.casefold()
            ):
                errors.append(f"{artifact_prefix}.sha256 缺失或与实际文件不匹配")
    return errors


def _validate_terminal_summary(run: dict[str, Any], prefix: str) -> list[str]:
    summary = run.get("terminal_summary")
    if not isinstance(summary, dict):
        return [f"{prefix}.terminal_summary 缺失"]
    errors = []
    if not str(summary.get("stage") or "").strip():
        errors.append(f"{prefix}.terminal_summary.stage 缺失")
    if not str(summary.get("reason") or "").strip():
        errors.append(f"{prefix}.terminal_summary.reason 缺失")
    evidence = summary.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{prefix}.terminal_summary.evidence 为空")
    return errors


def _validate_cci_cloud_resources(
    run: dict[str, Any],
    prefix: str,
) -> list[str]:
    if str(run.get("backend") or "").strip().casefold() != "cci":
        return []
    cloud = run.get("cloud_resources")
    if not isinstance(cloud, dict):
        return [f"{prefix}.cloud_resources CCI 后端必须提供脱敏云资源证据"]
    errors = []
    if not str(cloud.get("state_path") or "").strip():
        errors.append(f"{prefix}.cloud_resources.state_path 缺失")
    resources = cloud.get("resources")
    if not isinstance(resources, dict):
        return errors + [f"{prefix}.cloud_resources.resources 缺失"]
    for role in ("build", "execution", "recovery"):
        if not isinstance(resources.get(role), dict):
            errors.append(f"{prefix}.cloud_resources.resources.{role} 缺失")
    build = resources.get("build") if isinstance(resources.get("build"), dict) else {}
    if build.get("lifecycle_status") in {"creating", "running", "unknown"}:
        errors.append(f"{prefix}.cloud_resources.resources.build 尚未释放")
    summary = run.get("terminal_summary")
    terminal_stage = (
        str(summary.get("stage") or "").strip()
        if isinstance(summary, dict)
        else ""
    )
    execution = (
        resources.get("execution")
        if isinstance(resources.get("execution"), dict)
        else {}
    )
    if terminal_stage == "step_7" or str(run.get("status") or "") in {"passed", "partial"}:
        if not str(execution.get("instance_id") or "").strip():
            errors.append(
                f"{prefix}.cloud_resources.resources.execution.instance_id 缺失"
            )
        if execution.get("lifecycle_status") != "running":
            errors.append(
                f"{prefix}.cloud_resources.resources.execution 在 step_7.5 必须保持 running 并显示计费提示"
            )
        if execution.get("price") in (None, ""):
            errors.append(
                f"{prefix}.cloud_resources.resources.execution.price 缺失"
            )
    return errors


def _validate_terminal_run(
    run: dict[str, Any],
    prefix: str,
    *,
    base_dir: str | Path | None,
) -> list[str]:
    status = str(run.get("status") or "").strip().casefold()
    if status not in TERMINAL_STATUSES:
        return []
    summary = (
        run.get("terminal_summary")
        if isinstance(run.get("terminal_summary"), dict)
        else {}
    )
    terminal_stage = str(summary.get("stage") or "").strip()
    if terminal_stage in CONTROL_STEPS:
        errors = _validate_terminal_summary(run, prefix)
        errors.extend(_validate_cci_cloud_resources(run, prefix))
        if status in {"passed", "partial"}:
            errors.append(
                f"{prefix}.status={status} 与早期终止阶段 {terminal_stage} 不一致"
            )
        return errors
    require_ready = status in {"passed", "partial"}
    errors = _validate_preparation(run, prefix, require_ready=require_ready)
    errors.extend(_validate_terminal_summary(run, prefix))
    errors.extend(_validate_cci_cloud_resources(run, prefix))
    if status in {"passed", "partial"}:
        errors.extend(_validate_outcomes(run, prefix))
        errors.extend(_validate_compute_usage(run.get("compute_usage"), prefix, base_dir))
        contract, contract_error = _load_json_reference(run.get("scientific_repro_contract"), base_dir)
        if contract_error or not isinstance(contract, dict) or not contract:
            errors.append(f"{prefix}.scientific_repro_contract {contract_error or '为空'}")
        errors.extend(_validate_artifact_manifest(run.get("artifact_manifest"), prefix, base_dir))
        errors.extend(
            _validate_validation_result(
                run.get("validation_result"),
                prefix,
                base_dir,
                _selected_case_names(run.get("selected_execution_plan")),
                require_complete=status == "passed",
            )
        )
        errors.extend(
            _validate_figure_reproduction(
                run.get("figure_reproduction_result"),
                run.get("selected_execution_plan"),
                prefix,
                base_dir,
                run_status=status,
            )
        )
        summary = run.get("terminal_summary")
        if isinstance(summary, dict) and str(summary.get("stage") or "").strip() != "step_7":
            errors.append(f"{prefix}.terminal_summary.stage 必须是 step_7")
        if status == "passed":
            errors.extend(_validate_passed_outcome_statuses(run, prefix))
            details = run.get("experiment_details")
            if not isinstance(details, list) or not details:
                errors.append(f"{prefix}.experiment_details 为空")
    else:
        preparation = run.get("preparation") if isinstance(run.get("preparation"), dict) else {}
        if status == "failed" and terminal_stage in PREPARATION_STEPS:
            failed_index = PREPARATION_STEPS.index(terminal_stage)
            failed_record = preparation.get(terminal_stage)
            if not isinstance(failed_record, dict) or failed_record.get("status") != "failed":
                errors.append(
                    f"{prefix}.preparation.{terminal_stage}.status 必须是 failed"
                )
            for later_step in PREPARATION_STEPS[failed_index + 1:]:
                later_record = preparation.get(later_step)
                if not isinstance(later_record, dict) or later_record.get("status") != "not_reached":
                    errors.append(
                        f"{prefix}.preparation.{later_step}.status 必须是 not_reached"
                    )
        if terminal_stage == "step_7":
            errors.extend(_validate_preparation(run, prefix, require_ready=True))
            errors.extend(_validate_outcomes(run, prefix))
            errors.extend(
                _validate_validation_result(
                    run.get("validation_result"),
                    prefix,
                    base_dir,
                    _selected_case_names(run.get("selected_execution_plan")),
                )
            )
            errors.extend(
                _validate_figure_reproduction(
                    run.get("figure_reproduction_result"),
                    run.get("selected_execution_plan"),
                    prefix,
                    base_dir,
                    run_status=status,
                )
            )
    return errors


def validate_history(
    history: Any,
    *,
    for_report: bool = False,
    artifact_base_dir: str | Path | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(history, dict):
        return ["台账根节点必须是对象"]
    if history.get("schema_version") != SCHEMA_VERSION:
        version = history.get("schema_version")
        if version in LEGACY_SCHEMA_VERSIONS:
            errors.append(
                f"旧台账 schema_version={version} 必须通过 repair-run 补录真实证据并升级到 {SCHEMA_VERSION}"
            )
        else:
            errors.append(f"不支持的 schema_version：{version!r}")
    if history.get("layout_version") != LAYOUT_VERSION:
        errors.append(
            f"不支持的 layout_version：{history.get('layout_version')!r}，"
            f"必须是 {LAYOUT_VERSION}"
        )
    runs = history.get("runs")
    if not isinstance(runs, list):
        return errors + ["runs 必须是数组"]
    canonical_root = (
        Path(artifact_base_dir).expanduser().resolve()
        if artifact_base_dir is not None
        else None
    )
    if canonical_root is not None:
        workspace_root = str(history.get("workspace_root") or "").strip()
        if not workspace_root or Path(workspace_root).expanduser().resolve() != canonical_root:
            errors.append("workspace_root 与台账所在 v3 根目录不一致")
        shared = history.get("shared_paths")
        expected_shared = {
            "dataset_dir": canonical_root / "dataset",
            "model_dir": canonical_root / "model",
            "envs_root": canonical_root / "envs",
            "env_path": canonical_root / "envs" / str(history.get("project_name") or ""),
        }
        if not isinstance(shared, dict):
            errors.append("shared_paths 缺失")
        else:
            for key, expected in expected_shared.items():
                raw = str(shared.get(key) or "").strip()
                if not raw or Path(raw).expanduser().resolve() != expected:
                    errors.append(f"shared_paths.{key} 与 v3 根目录不一致")

    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    terminal_count = 0
    running_ids: list[str] = []
    for index, run in enumerate(runs, start=1):
        prefix = f"runs[{index - 1}]"
        if not isinstance(run, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            errors.append(f"{prefix}.run_id 缺失")
        elif run_id in seen_ids:
            errors.append(f"run_id 重复：{run_id}")
        else:
            seen_ids.add(run_id)
        sequence = run.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            errors.append(f"{prefix}.sequence 必须是正整数")
        elif sequence in seen_sequences:
            errors.append(f"sequence 重复：{sequence}")
        else:
            seen_sequences.add(sequence)
        status = str(run.get("status") or "").strip().casefold()
        if status not in RUN_STATUSES:
            errors.append(f"{prefix}.status 无效：{status or '缺失'}")
        if status == "running":
            running_ids.append(run_id or prefix)
        if status in TERMINAL_STATUSES:
            terminal_count += 1
            if for_report:
                errors.extend(
                    _validate_terminal_run(
                        run,
                        prefix,
                        base_dir=artifact_base_dir,
                    )
                )
        summary = (
            run.get("terminal_summary")
            if isinstance(run.get("terminal_summary"), dict)
            else {}
        )
        terminal_stage = str(summary.get("stage") or "").strip()
        plan_required = status in {"planned", "running"} or (
            status in TERMINAL_STATUSES and terminal_stage not in CONTROL_STEPS
        )
        if plan_required and not isinstance(
            run.get("selected_execution_plan"), (dict, list, str)
        ):
            errors.append(f"{prefix}.selected_execution_plan 缺失")
        run_root = str(run.get("run_root") or "").strip()
        step_dirs = run.get("step_dirs")
        if not run_root:
            errors.append(f"{prefix}.run_root 缺失")
        if not isinstance(step_dirs, dict) or any(
            not str(step_dirs.get(step) or "").strip() for step in STEP_NAMES
        ):
            errors.append(f"{prefix}.step_dirs 缺少 v3 完整步骤路径")
        shared_paths = run.get("shared_paths")
        if not isinstance(shared_paths, dict) or any(
            not str(shared_paths.get(key) or "").strip()
            for key in ("dataset_dir", "model_dir", "envs_root", "env_path")
        ):
            errors.append(f"{prefix}.shared_paths 缺少 v3 共享目录路径")
        if canonical_root is not None and run_id:
            expected_run_root = canonical_root / run_id
            if not run_root or Path(run_root).expanduser().resolve() != expected_run_root:
                errors.append(f"{prefix}.run_root 与 run_id 不一致")
            if isinstance(step_dirs, dict):
                for step in STEP_NAMES:
                    raw = str(step_dirs.get(step) or "").strip()
                    expected = expected_run_root / step
                    if not raw or Path(raw).expanduser().resolve() != expected:
                        errors.append(f"{prefix}.step_dirs.{step} 与 v3 布局不一致")
            if isinstance(shared_paths, dict):
                for key, expected in expected_shared.items():
                    raw = str(shared_paths.get(key) or "").strip()
                    if not raw or Path(raw).expanduser().resolve() != expected:
                        errors.append(f"{prefix}.shared_paths.{key} 与根共享目录不一致")
            expected_artifacts = {
                "code_dir": expected_run_root / "step_3" / "code",
                "audit_report": expected_run_root
                / "step_1"
                / f"{history.get('project_name')}_Audit_Report.md",
                "audit_score": expected_run_root / "step_1" / "audit_score.json",
                "report_path": expected_run_root
                / "step_8"
                / f"{history.get('project_name')}_final_reproduce_report.docx",
                "run_output_root": expected_run_root / "step_7",
                "history_path": canonical_root / "execution_run_history.json",
            }
            for key, expected in expected_artifacts.items():
                raw = str(run.get(key) or "").strip()
                if not raw or Path(raw).expanduser().resolve() != expected:
                    errors.append(f"{prefix}.{key} 与 v3 布局不一致")

    ordered_sequences = [
        run.get("sequence")
        for run in runs
        if isinstance(run, dict) and isinstance(run.get("sequence"), int)
    ]
    if ordered_sequences != sorted(ordered_sequences):
        errors.append("runs 必须按 sequence 升序保存")

    if for_report:
        if artifact_base_dir is None:
            errors.append("缺少最终产物目录，无法校验规范审计报告")
        else:
            _, audit_error = _validate_required_audit_report(history, artifact_base_dir)
            if audit_error:
                errors.append(audit_error)
        if running_ids:
            errors.append("存在未解释的 running 记录：" + ", ".join(running_ids))
        if terminal_count == 0:
            errors.append("没有已实际执行的终态记录可用于报告")
        gate = history.get("report_gate")
        if not isinstance(gate, dict):
            errors.append("report_gate 缺失")
        elif gate.get("status") != "confirmed" or gate.get("decision") != "generate_report":
            errors.append("用户尚未明确确认生成 DOCX")
        else:
            if gate.get("decision_source") != "explicit_user_reply":
                errors.append("报告授权缺少明确用户回复来源")
            if not str(gate.get("confirmation_id") or "").strip():
                errors.append("报告授权缺少一次性确认编号")
            if not str(gate.get("prompt_displayed_at") or "").strip():
                errors.append("报告授权前未展示三项选择")
            if _decision_from_user_response(gate.get("user_response")) != "generate_report":
                errors.append("报告授权未包含用户对“生成最终 DOCX”的明确原始回复")
    return errors


def load_history(path: str | Path, *, for_report: bool = False) -> dict[str, Any]:
    ledger_path = Path(path).expanduser().resolve()
    try:
        history = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoryValidationError(f"无法读取执行台账 {ledger_path}：{exc}") from exc
    errors = validate_history(
        history,
        for_report=for_report,
        artifact_base_dir=ledger_path.parent,
    )
    if errors:
        raise HistoryValidationError("；".join(errors))
    return history


def _load_for_update(path: str | Path) -> tuple[Path, dict[str, Any]]:
    ledger_path = Path(path).expanduser().resolve()
    return ledger_path, load_history(ledger_path)


def _find_run(history: dict[str, Any], run_id: str) -> dict[str, Any]:
    for run in history["runs"]:
        if run.get("run_id") == run_id:
            return run
    raise HistoryValidationError(f"未找到 run_id：{run_id}")


def _reset_gate(history: dict[str, Any], status: str = "not_ready") -> None:
    history["report_gate"] = {
        "status": status,
        "decision": None,
        "requested_at": None,
        "prompt_displayed_at": None,
        "decided_at": None,
        "confirmation_id": None,
        "decision_source": None,
        "user_response": None,
        "user_request": None,
    }


def initialize(path: str | Path, project_name: str) -> dict[str, Any]:
    ledger_path = Path(path).expanduser().resolve()
    if ledger_path.exists():
        return load_history(ledger_path)
    history = new_history(project_name)
    history["workspace_root"] = str(ledger_path.parent)
    history["shared_paths"] = {
        "dataset_dir": str(ledger_path.parent / "dataset"),
        "model_dir": str(ledger_path.parent / "model"),
        "envs_root": str(ledger_path.parent / "envs"),
        "env_path": str(ledger_path.parent / "envs" / project_name),
    }
    _atomic_write(ledger_path, history)
    return history


def _run_paths(ledger_path: Path, project_name: str, run_id: str) -> dict[str, Any]:
    run_root = ledger_path.parent / run_id
    step_dirs = {step: str(run_root / step) for step in STEP_NAMES}
    shared_paths = {
        "dataset_dir": str(ledger_path.parent / "dataset"),
        "model_dir": str(ledger_path.parent / "model"),
        "envs_root": str(ledger_path.parent / "envs"),
        "env_path": str(ledger_path.parent / "envs" / project_name),
    }
    return {
        "run_root": str(run_root),
        "step_dirs": step_dirs,
        "shared_paths": shared_paths,
        **shared_paths,
        "history_path": str(ledger_path),
        "run_output_root": step_dirs["step_7"],
        "code_dir": str(run_root / "step_3" / "code"),
        "audit_report": str(
            run_root / "step_1" / f"{project_name}_Audit_Report.md"
        ),
        "audit_score": str(run_root / "step_1" / "audit_score.json"),
        "report_path": str(
            run_root / "step_8" / f"{project_name}_final_reproduce_report.docx"
        ),
    }


def reserve_run(
    path: str | Path,
    *,
    backend: str | None = None,
    source_run_id: str | None = None,
    cloud_resources: Any = None,
) -> dict[str, Any]:
    ledger_path, history = _load_for_update(path)
    normalized_backend = str(backend or "").strip().casefold() or None
    if normalized_backend not in {None, "local", "ssh", "cci"}:
        raise HistoryValidationError("执行后端必须是 local、ssh 或 cci")

    ledger_sequences = [
        run.get("sequence", 0)
        for run in history["runs"]
        if isinstance(run, dict)
    ]
    directory_sequences = [
        int(match.group(1))
        for item in ledger_path.parent.iterdir()
        if item.is_dir() and (match := RUN_ID_RE.fullmatch(item.name))
    ]
    sequence = max([*ledger_sequences, *directory_sequences, 0]) + 1
    run_id = f"run-{sequence:03d}"
    paths = _run_paths(ledger_path, history["project_name"], run_id)
    source_run = None
    if source_run_id:
        source_run = _find_run(history, source_run_id)
        if source_run.get("status") not in TERMINAL_STATUSES:
            raise HistoryValidationError(
                f"{source_run_id} 尚未终结，不能作为新运行的控制面快照来源"
            )
    run_root = Path(paths["run_root"])
    try:
        run_root.mkdir(parents=False, exist_ok=False)
        for step in STEP_NAMES:
            Path(paths["step_dirs"][step]).mkdir()
    except FileExistsError as exc:
        raise HistoryValidationError(f"run 目录已存在，禁止覆盖：{run_root}") from exc
    except OSError as exc:
        raise HistoryValidationError(f"无法创建完整 run 目录：{exc}") from exc

    if source_run is not None:
        for step in CONTROL_STEPS[:-1]:
            source_dir = Path(source_run["step_dirs"][step])
            if source_dir.is_dir():
                shutil.copytree(
                    source_dir,
                    Path(paths["step_dirs"][step]),
                    dirs_exist_ok=True,
                )

    run = {
        "run_id": run_id,
        "sequence": sequence,
        "selected_option_id": None,
        "selected_execution_plan": None,
        "status": "reserved",
        "backend": normalized_backend,
        "cloud_resources": cloud_resources if isinstance(cloud_resources, dict) else {},
        "reserved_at": _now_iso(),
        "bound_at": None,
        "started_at": None,
        "ended_at": None,
        **paths,
        "control_snapshot_source_run_id": (
            source_run.get("run_id") if isinstance(source_run, dict) else None
        ),
        "preparation": {},
        "reuse_decisions": {},
        "actual_test_outcomes": [],
        "experiment_details": [],
        "compute_usage": {},
        "scientific_repro_contract": None,
        "artifact_manifest": None,
        "validation_result": None,
        "figure_reproduction_result": None,
        "terminal_summary": None,
        "repair_history": [],
    }
    history["runs"].append(run)
    _reset_gate(history)
    _atomic_write(ledger_path, history)
    return run


def bind_plan(
    path: str | Path,
    run_id: str,
    selected_execution_plan: Any,
    *,
    selected_option_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(selected_execution_plan, (dict, list, str)):
        raise HistoryValidationError("selected_execution_plan 必须是完整计划对象")
    ledger_path, history = _load_for_update(path)
    run = _find_run(history, run_id)
    if run.get("status") != "reserved":
        raise HistoryValidationError(
            f"{run_id} 只有 reserved 状态可以绑定计划，当前为 {run.get('status')}"
        )
    plan_path = Path(run["step_dirs"]["step_2_5"]) / "selected_execution_plan.json"
    _atomic_write_payload(plan_path, selected_execution_plan)
    run["selected_option_id"] = selected_option_id
    run["selected_execution_plan"] = selected_execution_plan
    run["selected_execution_plan_path"] = str(plan_path)
    run["status"] = "planned"
    run["bound_at"] = _now_iso()
    _reset_gate(history)
    _atomic_write(ledger_path, history)
    return run


def add_run(
    path: str | Path,
    selected_execution_plan: Any,
    *,
    selected_option_id: str | None = None,
    backend: str | None = None,
    cloud_resources: Any = None,
) -> dict[str, Any]:
    reserved = reserve_run(
        path,
        backend=backend,
        cloud_resources=cloud_resources,
    )
    return bind_plan(
        path,
        reserved["run_id"],
        selected_execution_plan,
        selected_option_id=selected_option_id,
    )


def start_run(path: str | Path, run_id: str, started_at: str | None = None) -> dict[str, Any]:
    ledger_path, history = _load_for_update(path)
    run = _find_run(history, run_id)
    if run.get("status") != "planned":
        raise HistoryValidationError(f"{run_id} 只有 planned 状态可以开始，当前为 {run.get('status')}")
    run["status"] = "running"
    run["started_at"] = started_at or _now_iso()
    _reset_gate(history)
    _atomic_write(ledger_path, history)
    return run


def record_preparation(
    path: str | Path,
    run_id: str,
    step: str,
    *,
    status: str,
    fingerprint: str,
    evidence: Any = None,
    backend: str | None = None,
    reason: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> dict[str, Any]:
    if step not in PREPARATION_STEPS:
        raise HistoryValidationError(f"准备步骤必须是 {', '.join(PREPARATION_STEPS)}")
    normalized_status = str(status or "").strip().casefold()
    if normalized_status not in PREPARATION_STATUSES:
        raise HistoryValidationError(
            "准备步骤状态必须是 " + "、".join(sorted(PREPARATION_STATUSES))
        )
    if not str(fingerprint or "").strip():
        raise HistoryValidationError("准备步骤 fingerprint 不能为空")
    normalized_evidence = evidence if isinstance(evidence, list) else []
    if not normalized_evidence:
        raise HistoryValidationError("准备步骤 evidence 不能为空")
    normalized_reason = str(reason or "").strip()
    if normalized_status in {"skipped_not_required", "failed", "not_reached"} and not normalized_reason:
        raise HistoryValidationError(f"准备步骤状态 {normalized_status} 必须提供 reason")
    ledger_path, history = _load_for_update(path)
    run = _find_run(history, run_id)
    normalized_backend = str(backend or run.get("backend") or "").strip().casefold()
    if normalized_backend not in {"local", "ssh", "cci"}:
        raise HistoryValidationError("准备步骤 backend 必须是 local、ssh 或 cci")
    timestamp = _now_iso()
    run.setdefault("preparation", {})[step] = {
        "status": normalized_status,
        "fingerprint": str(fingerprint or "").strip(),
        "evidence": normalized_evidence,
        "backend": normalized_backend,
        "reason": normalized_reason or None,
        "started_at": started_at or timestamp,
        "ended_at": ended_at or timestamp,
        "recorded_at": timestamp,
    }
    _atomic_write(ledger_path, history)
    return run["preparation"][step]


def finish_run(
    path: str | Path,
    run_id: str,
    *,
    status: str,
    ended_at: str | None = None,
    actual_test_outcomes: Any = None,
    experiment_details: Any = None,
    compute_usage: Any = None,
    scientific_repro_contract: Any = None,
    artifact_manifest: Any = None,
    validation_result: Any = None,
    figure_reproduction_result: Any = None,
    terminal_summary: Any = None,
    cloud_resources: Any = None,
) -> dict[str, Any]:
    normalized_status = str(status).strip().casefold()
    if normalized_status not in TERMINAL_STATUSES:
        raise HistoryValidationError("终态必须是 passed、partial、failed 或 skipped")
    ledger_path, history = _load_for_update(path)
    run = _find_run(history, run_id)
    if run.get("status") not in {"reserved", "planned", "running"}:
        raise HistoryValidationError(f"{run_id} 已是终态 {run.get('status')}，不能覆盖")
    if not run.get("started_at"):
        run["started_at"] = ended_at or _now_iso()
    candidate = copy.deepcopy(run)
    candidate["status"] = normalized_status
    candidate["ended_at"] = ended_at or _now_iso()
    updates = {
        "actual_test_outcomes": actual_test_outcomes,
        "experiment_details": experiment_details,
        "compute_usage": compute_usage,
        "scientific_repro_contract": scientific_repro_contract,
        "artifact_manifest": artifact_manifest,
        "validation_result": validation_result,
        "figure_reproduction_result": figure_reproduction_result,
        "terminal_summary": terminal_summary,
        "cloud_resources": cloud_resources,
    }
    for key, value in updates.items():
        if value is not None:
            candidate[key] = value
    terminal_errors = _validate_terminal_run(
        candidate,
        f"runs[{history['runs'].index(run)}]",
        base_dir=ledger_path.parent,
    )
    if terminal_errors:
        raise HistoryValidationError("；".join(terminal_errors))
    run.clear()
    run.update(candidate)
    history["report_gate"] = _new_confirmation_gate("awaiting_user", run_id)
    _atomic_write(ledger_path, history)
    envelope = _decision_envelope(history, run)
    _atomic_write_payload(
        Path(run["step_dirs"]["step_7_5"]) / "finish_run_result.json",
        envelope,
    )
    return envelope


def repair_run(
    path: str | Path,
    run_id: str,
    *,
    reason: str,
    preparation: Any = None,
    actual_test_outcomes: Any = None,
    experiment_details: Any = None,
    compute_usage: Any = None,
    scientific_repro_contract: Any = None,
    artifact_manifest: Any = None,
    validation_result: Any = None,
    figure_reproduction_result: Any = None,
    terminal_summary: Any = None,
    cloud_resources: Any = None,
) -> dict[str, Any]:
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise HistoryValidationError("repair-run 必须提供非空 reason")
    ledger_path = Path(path).expanduser().resolve()
    try:
        history = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoryValidationError(f"无法读取待修复台账 {ledger_path}：{exc}") from exc
    if history.get("schema_version") not in LEGACY_SCHEMA_VERSIONS | {SCHEMA_VERSION}:
        raise HistoryValidationError(f"不支持修复 schema_version={history.get('schema_version')!r}")
    if history.get("layout_version") != LAYOUT_VERSION:
        raise HistoryValidationError(
            "repair-run 只允许补录 v3 布局中的证据；旧路径布局不得自动迁移或覆盖"
        )
    run = _find_run(history, run_id)
    if str(run.get("status") or "").strip().casefold() not in TERMINAL_STATUSES:
        raise HistoryValidationError("repair-run 只允许修复已终态轮次")
    updates = {
        "preparation": preparation,
        "actual_test_outcomes": actual_test_outcomes,
        "experiment_details": experiment_details,
        "compute_usage": compute_usage,
        "scientific_repro_contract": scientific_repro_contract,
        "artifact_manifest": artifact_manifest,
        "validation_result": validation_result,
        "figure_reproduction_result": figure_reproduction_result,
        "terminal_summary": terminal_summary,
        "cloud_resources": cloud_resources,
    }
    provided = {key: value for key, value in updates.items() if value is not None}
    if not provided:
        raise HistoryValidationError("repair-run 至少需要提供一个真实证据字段")
    def repair_summary(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {
                "type": "object",
                "keys": sorted(str(key) for key in value),
                "item_count": len(value),
            }
        if isinstance(value, list):
            return {"type": "array", "item_count": len(value)}
        if value is None:
            return {"type": "null", "item_count": 0}
        return {"type": type(value).__name__, "value_present": bool(str(value).strip())}

    before = {
        key: hashlib.sha256(
            json.dumps(run.get(key), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        for key in provided
    }
    before_summary = {key: repair_summary(run.get(key)) for key in provided}
    for key, value in provided.items():
        run[key] = value
    after = {
        key: hashlib.sha256(
            json.dumps(run.get(key), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        for key in provided
    }
    after_summary = {key: repair_summary(run.get(key)) for key in provided}
    history["schema_version"] = SCHEMA_VERSION
    run.setdefault("repair_history", []).append(
        {
            "repaired_at": _now_iso(),
            "reason": normalized_reason,
            "fields": sorted(provided),
            "before_sha256": before,
            "after_sha256": after,
            "before_summary": before_summary,
            "after_summary": after_summary,
            "source": "explicit_evidence_repair",
        }
    )
    validation_errors = _validate_terminal_run(
        run,
        f"runs[{history['runs'].index(run)}]",
        base_dir=ledger_path.parent,
    )
    if validation_errors:
        _reset_gate(history)
    else:
        history["report_gate"] = _new_confirmation_gate("awaiting_user", run_id)
    _atomic_write(ledger_path, history)
    result = {
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        "repaired_fields": sorted(provided),
        "report_gate": history["report_gate"],
        "validation_errors": validation_errors,
    }
    if not validation_errors:
        result.update(_decision_envelope(history, run))
    return result


def request_report_decision(path: str | Path) -> dict[str, Any]:
    """Deprecated compatibility helper for legacy gates and prompt redisplay."""
    ledger_path, history = _load_for_update(path)
    if any(run.get("status") == "running" for run in history["runs"]):
        raise HistoryValidationError("仍有 running 执行，不能进入报告前确认")
    if not any(run.get("status") in TERMINAL_STATUSES for run in history["runs"]):
        raise HistoryValidationError("尚无已执行实验，不能进入报告前确认")
    gate = history.get("report_gate")
    if not isinstance(gate, dict) or gate.get("status") not in {"awaiting_prompt", "awaiting_user"}:
        raise HistoryValidationError(
            "request-decision 仅用于恢复旧 awaiting_prompt 台账或重新显示已有提示；"
            "必须先由 finish-run 创建报告确认门"
        )
    confirmation_id = str(gate.get("confirmation_id") or "").strip()
    if not confirmation_id:
        raise HistoryValidationError("报告选择缺少一次性确认编号")
    timestamp = _now_iso()
    gate["status"] = "awaiting_user"
    gate["prompt_displayed_at"] = gate.get("prompt_displayed_at") or timestamp
    _atomic_write(ledger_path, history)
    terminal_runs = [
        run for run in history["runs"] if run.get("status") in TERMINAL_STATUSES
    ]
    return _decision_envelope(history, terminal_runs[-1], compatibility_mode=True)


def decide_report_gate(
    path: str | Path,
    decision: str,
    *,
    confirmation_id: str | None = None,
    user_response: str | None = None,
    user_request: str | None = None,
) -> dict[str, Any]:
    normalized = str(decision).strip().casefold()
    if normalized not in REPORT_DECISIONS:
        raise HistoryValidationError("无效决策：" + normalized)
    if normalized in {"custom_execution", "report_requirement"} and not str(user_request or "").strip():
        raise HistoryValidationError("自定义执行或报告补充要求不能为空")
    ledger_path, history = _load_for_update(path)
    if any(run.get("status") == "running" for run in history["runs"]):
        raise HistoryValidationError("仍有 running 执行，不能进行报告决策")
    terminal_runs = [run for run in history["runs"] if run.get("status") in TERMINAL_STATUSES]
    if not terminal_runs:
        raise HistoryValidationError("尚无已执行实验，不能进行报告决策")
    current_gate = history.get("report_gate")
    if not isinstance(current_gate, dict) or current_gate.get("status") != "awaiting_user":
        raise HistoryValidationError("finish-run 尚未生成并展示三项选择，不能记录报告决策")
    expected_confirmation_id = str(current_gate.get("confirmation_id") or "").strip()
    if not expected_confirmation_id or str(confirmation_id or "").strip() != expected_confirmation_id:
        raise HistoryValidationError("一次性确认编号缺失或不匹配")
    raw_user_response = str(user_response or "").strip()
    response_decision = _decision_from_user_response(raw_user_response)
    expected_response_decision = (
        "custom" if normalized in {"custom_execution", "report_requirement"} else normalized
    )
    if response_decision != expected_response_decision:
        raise HistoryValidationError(
            "用户原始回复与所记录决策不一致；必须使用明确的 1、2 或“3 + 具体要求”"
        )
    timestamp = _now_iso()
    if normalized == "report_requirement":
        history.setdefault("report_requirements", []).append({
            "request": str(user_request).strip(),
            "recorded_at": timestamp,
            "confirmation_id": expected_confirmation_id,
            "decision_source": "explicit_user_reply",
            "user_response": raw_user_response,
        })
        active_run_id = str(current_gate.get("run_id") or terminal_runs[-1].get("run_id"))
        history["report_gate"] = _new_confirmation_gate(
            "awaiting_user", active_run_id
        )
        _atomic_write(ledger_path, history)
        active_run = _find_run(history, active_run_id)
        _atomic_write_payload(
            Path(active_run["step_dirs"]["step_7_5"])
            / "report_requirement_decision.json",
            {
                "decision": normalized,
                "confirmation_id": expected_confirmation_id,
                "decision_source": "explicit_user_reply",
                "user_response": raw_user_response,
                "user_request": str(user_request).strip(),
                "next_report_gate": history["report_gate"],
            },
        )
        return _decision_envelope(history, terminal_runs[-1])
    elif normalized == "generate_report":
        gate_status = "confirmed"
    else:
        gate_status = "continued"
    history["report_gate"] = {
        "status": gate_status,
        "decision": normalized,
        "requested_at": current_gate.get("requested_at") or timestamp,
        "prompt_displayed_at": current_gate.get("prompt_displayed_at"),
        "decided_at": timestamp,
        "confirmation_id": expected_confirmation_id,
        "decision_source": "explicit_user_reply",
        "user_response": raw_user_response,
        "user_request": str(user_request).strip() if user_request else None,
        "run_id": current_gate.get("run_id") or terminal_runs[-1].get("run_id"),
    }
    active_run = _find_run(history, history["report_gate"]["run_id"])
    if normalized == "generate_report":
        history["final_artifacts"] = {
            "authorized_run_id": active_run["run_id"],
            "audit_report": {
                "path": active_run["audit_report"],
                "required": True,
                "format": "markdown",
            },
            "reproduction_report": {
                "path": active_run["report_path"],
                "required": True,
                "format": "docx",
            },
        }
    _atomic_write(ledger_path, history)
    _atomic_write_payload(
        Path(active_run["step_dirs"]["step_7_5"]) / "report_gate_decision.json",
        history["report_gate"],
    )
    return history["report_gate"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_available(evidence: Any, base_dir: Path) -> tuple[bool, str]:
    entries = evidence if isinstance(evidence, list) else [evidence]
    entries = [entry for entry in entries if entry not in (None, "", [], {})]
    if not entries:
        return False, "复用证据缺失"
    for entry in entries:
        if isinstance(entry, str):
            raw_path = entry.strip()
            checksum = ""
        elif isinstance(entry, dict):
            raw_path = str(entry.get("path") or "").strip()
            checksum = str(entry.get("sha256") or "").strip().casefold()
        else:
            return False, "复用证据格式无效"
        if not raw_path:
            return False, "复用证据路径缺失"
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if not path.exists():
            return False, f"证据路径不存在：{path}"
        if checksum:
            if not path.is_file():
                return False, f"带校验和的证据不是文件：{path}"
            if _sha256(path).casefold() != checksum:
                return False, f"证据校验和不匹配：{path}"
    return True, "证据存在且校验通过"


def calculate_reuse(
    path: str | Path,
    run_id: str,
    requirements: dict[str, Any],
) -> dict[str, Any]:
    ledger_path, history = _load_for_update(path)
    current = _find_run(history, run_id)
    prior_runs = [
        run for run in history["runs"]
        if run.get("sequence", 0) < current.get("sequence", 0)
        and run.get("status") in TERMINAL_STATUSES
    ]
    decisions: dict[str, Any] = {}
    upstream_reusable = bool(prior_runs)
    for step in PREPARATION_STEPS:
        requirement = requirements.get(step) if isinstance(requirements, dict) else None
        required_fingerprint = (
            str(requirement.get("fingerprint") or "").strip()
            if isinstance(requirement, dict)
            else str(requirement or "").strip()
        )
        selected_run = None
        selected_preparation = None
        invalid_match = None
        completed_seen = False
        if upstream_reusable and required_fingerprint:
            for candidate in reversed(prior_runs):
                prior = candidate.get("preparation", {}).get(step, {})
                if prior.get("status") != "completed":
                    continue
                completed_seen = True
                if str(prior.get("fingerprint") or "").strip() != required_fingerprint:
                    continue
                evidence_ok, evidence_reason = _evidence_available(
                    prior.get("evidence"),
                    ledger_path.parent,
                )
                if evidence_ok:
                    selected_run = candidate
                    selected_preparation = prior
                    break
                if invalid_match is None:
                    invalid_match = (candidate, prior, evidence_reason)

        if not prior_runs:
            reused = False
            reason = "不存在可复用的上一轮终态记录"
        elif not upstream_reusable:
            reused = False
            reason = "上游准备步骤已失效，必须从最早受影响步骤重新执行"
        elif not required_fingerprint:
            reused = False
            reason = "新计划需求指纹缺失，按安全规则重新执行"
        elif selected_run is not None:
            reused = True
            reason = "证据存在且校验通过"
        elif invalid_match is not None:
            reused = False
            reason = invalid_match[2]
        elif completed_seen:
            reused = False
            reason = "需求指纹发生变化"
        else:
            reused = False
            reason = "历史轮次中该步骤均未完整完成"

        evidence_source = (
            (selected_run, selected_preparation)
            if selected_run is not None
            else (invalid_match[0], invalid_match[1])
            if invalid_match is not None
            else (None, None)
        )
        decisions[step] = {
            "reused": reused,
            "reason": reason,
            "source_run_id": evidence_source[0].get("run_id") if evidence_source[0] else None,
            "fingerprint": (
                str(evidence_source[1].get("fingerprint") or "").strip() or None
                if evidence_source[1]
                else required_fingerprint or None
            ),
            "evidence": evidence_source[1].get("evidence") if evidence_source[1] else [],
        }
        if step == "step_3":
            decisions[step]["source_code_dir"] = (
                evidence_source[0].get("code_dir") if evidence_source[0] else None
            )
            decisions[step]["target_code_dir"] = current.get("code_dir")
            decisions[step]["materialization_required"] = reused
        if not reused:
            upstream_reusable = False
    decisions["step_7"] = {
        "reused": False,
        "reason": "每轮实验必须重新执行 step_7",
        "source_run_id": None,
        "fingerprint": None,
        "evidence": [],
    }
    current["reuse_decisions"] = decisions
    _atomic_write(ledger_path, history)
    return decisions


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, help="execution_run_history.json 路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--project-name", required=True)

    reserve_parser = subparsers.add_parser("reserve-run")
    reserve_parser.add_argument("--backend", choices=["local", "ssh", "cci"])
    reserve_parser.add_argument("--source-run-id")
    reserve_parser.add_argument("--cloud-resources")

    bind_parser = subparsers.add_parser("bind-plan")
    bind_parser.add_argument("--run-id", required=True)
    bind_parser.add_argument("--plan-json", required=True)
    bind_parser.add_argument("--option-id")

    add_parser = subparsers.add_parser("add-run")
    add_parser.add_argument("--plan-json", required=True)
    add_parser.add_argument("--option-id")
    add_parser.add_argument("--backend")
    add_parser.add_argument("--cloud-resources")

    start_parser = subparsers.add_parser("start-run")
    start_parser.add_argument("--run-id", required=True)
    start_parser.add_argument("--started-at")

    prep_parser = subparsers.add_parser("record-preparation")
    prep_parser.add_argument("--run-id", required=True)
    prep_parser.add_argument("--step", choices=PREPARATION_STEPS, required=True)
    prep_parser.add_argument("--status", required=True)
    prep_parser.add_argument("--fingerprint", required=True)
    prep_parser.add_argument("--evidence-json")
    prep_parser.add_argument("--backend", choices=["local", "ssh", "cci"])
    prep_parser.add_argument("--reason")
    prep_parser.add_argument("--started-at")
    prep_parser.add_argument("--ended-at")

    finish_parser = subparsers.add_parser("finish-run")
    finish_parser.add_argument("--run-id", required=True)
    finish_parser.add_argument("--status", choices=sorted(TERMINAL_STATUSES), required=True)
    finish_parser.add_argument("--ended-at")
    finish_parser.add_argument("--actual-test-outcomes")
    finish_parser.add_argument("--experiment-details")
    finish_parser.add_argument("--compute-usage")
    finish_parser.add_argument("--scientific-repro-contract")
    finish_parser.add_argument("--artifact-manifest")
    finish_parser.add_argument("--validation-result")
    finish_parser.add_argument("--figure-reproduction-result")
    finish_parser.add_argument("--terminal-summary")
    finish_parser.add_argument("--cloud-resources")

    repair_parser = subparsers.add_parser("repair-run")
    repair_parser.add_argument("--run-id", required=True)
    repair_parser.add_argument("--reason", required=True)
    repair_parser.add_argument("--preparation")
    repair_parser.add_argument("--actual-test-outcomes")
    repair_parser.add_argument("--experiment-details")
    repair_parser.add_argument("--compute-usage")
    repair_parser.add_argument("--scientific-repro-contract")
    repair_parser.add_argument("--artifact-manifest")
    repair_parser.add_argument("--validation-result")
    repair_parser.add_argument("--figure-reproduction-result")
    repair_parser.add_argument("--terminal-summary")
    repair_parser.add_argument("--cloud-resources")

    decision_parser = subparsers.add_parser("decide")
    decision_parser.add_argument("--decision", choices=sorted(REPORT_DECISIONS), required=True)
    decision_parser.add_argument("--confirmation-id", required=True)
    decision_parser.add_argument("--user-response", required=True)
    decision_parser.add_argument("--request")

    subparsers.add_parser("request-decision")

    reuse_parser = subparsers.add_parser("reuse-plan")
    reuse_parser.add_argument("--run-id", required=True)
    reuse_parser.add_argument("--requirements-json", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--for-report", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "init":
            result = initialize(args.history, args.project_name)
        elif args.command == "reserve-run":
            result = reserve_run(
                args.history,
                backend=args.backend,
                source_run_id=args.source_run_id,
                cloud_resources=_json_value(args.cloud_resources),
            )
        elif args.command == "bind-plan":
            result = bind_plan(
                args.history,
                args.run_id,
                _json_value(args.plan_json),
                selected_option_id=args.option_id,
            )
        elif args.command == "add-run":
            result = add_run(
                args.history,
                _json_value(args.plan_json),
                selected_option_id=args.option_id,
                backend=args.backend,
                cloud_resources=_json_value(args.cloud_resources),
            )
        elif args.command == "start-run":
            result = start_run(args.history, args.run_id, args.started_at)
        elif args.command == "record-preparation":
            result = record_preparation(
                args.history,
                args.run_id,
                args.step,
                status=args.status,
                fingerprint=args.fingerprint,
                evidence=_json_value(args.evidence_json),
                backend=args.backend,
                reason=args.reason,
                started_at=args.started_at,
                ended_at=args.ended_at,
            )
        elif args.command == "finish-run":
            result = finish_run(
                args.history,
                args.run_id,
                status=args.status,
                ended_at=args.ended_at,
                actual_test_outcomes=_json_value(args.actual_test_outcomes),
                experiment_details=_json_value(args.experiment_details),
                compute_usage=_json_value(args.compute_usage),
                scientific_repro_contract=_json_value(args.scientific_repro_contract),
                artifact_manifest=_json_value(args.artifact_manifest),
                validation_result=_json_value(args.validation_result),
                figure_reproduction_result=_json_value(args.figure_reproduction_result),
                terminal_summary=_json_value(args.terminal_summary),
                cloud_resources=_json_value(args.cloud_resources),
            )
        elif args.command == "repair-run":
            result = repair_run(
                args.history,
                args.run_id,
                reason=args.reason,
                preparation=_json_value(args.preparation),
                actual_test_outcomes=_json_value(args.actual_test_outcomes),
                experiment_details=_json_value(args.experiment_details),
                compute_usage=_json_value(args.compute_usage),
                scientific_repro_contract=_json_value(args.scientific_repro_contract),
                artifact_manifest=_json_value(args.artifact_manifest),
                validation_result=_json_value(args.validation_result),
                figure_reproduction_result=_json_value(args.figure_reproduction_result),
                terminal_summary=_json_value(args.terminal_summary),
                cloud_resources=_json_value(args.cloud_resources),
            )
        elif args.command == "decide":
            result = decide_report_gate(
                args.history,
                args.decision,
                confirmation_id=args.confirmation_id,
                user_response=args.user_response,
                user_request=args.request,
            )
        elif args.command == "request-decision":
            result = request_report_decision(args.history)
        elif args.command == "reuse-plan":
            requirements = _json_value(args.requirements_json)
            if not isinstance(requirements, dict):
                raise HistoryValidationError("requirements-json 必须解析为对象")
            result = calculate_reuse(args.history, args.run_id, requirements)
        else:
            result = load_history(args.history, for_report=args.for_report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (HistoryValidationError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
