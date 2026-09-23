"""Shared preparation-step receipt and ledger recording helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import execution_history


DIRECT_STEP_NAMES = {"step_4", "step_5", "step_6"}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def validate_tracking_args(
    history: str | None,
    run_id: str | None,
    standalone: bool,
) -> None:
    if not run_id:
        raise ValueError(
            "v3 目录协议要求主流水线同时提供 --history 和 --run-id"
        )
    if standalone:
        if history:
            raise ValueError("--standalone 不能与 --history 同时使用")
        return
    if not history:
        raise ValueError("主流水线 runner 必须同时提供 --history 和 --run-id；独立运行需显式使用 --standalone")


def _canonical_digest(payload: Any, evidence_paths: list[str]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    )
    for raw_path in sorted(str(item) for item in evidence_paths):
        path = Path(raw_path).expanduser()
        digest.update(raw_path.encode("utf-8"))
        if path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _ssh_parts(command: str, password: str | None) -> tuple[list[str], dict[str, str] | None]:
    parts = shlex.split(str(command or ""))
    if not parts or parts[0] != "ssh":
        raise ValueError("SSH command must start with ssh")
    if password:
        env = os.environ.copy()
        env["SSHPASS"] = password
        return ["sshpass", "-e", *parts, "python3", "-"], env
    return [*parts, "python3", "-"], None


def _remote_recorder_source(payload: dict[str, Any]) -> str:
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return f"""
import base64, hashlib, json, os, tempfile
from pathlib import Path
payload = json.loads(base64.b64decode({encoded!r}).decode("utf-8"))
history_path = Path(payload["history"]).expanduser().resolve()
history = json.loads(history_path.read_text(encoding="utf-8"))
if history.get("schema_version") != "2.0" or history.get("layout_version") != 3:
    raise SystemExit("remote ledger must use schema_version 2.0 and layout_version 3")
run = next((item for item in history.get("runs", []) if item.get("run_id") == payload["run_id"]), None)
if not isinstance(run, dict):
    raise SystemExit("run_id not found")
if run.get("status") not in ("planned", "running"):
    raise SystemExit("preparation can only be recorded for planned/running runs")
project_root = history_path.parent
if payload["step"] not in ("step_3", "step_4", "step_5", "step_6"):
    raise SystemExit("unsupported preparation step")
receipt_dir = project_root / payload["run_id"] / payload["step"]
log_path = receipt_dir / (payload["step"] + ".log")
receipt_path = receipt_dir / (payload["step"] + "_receipt.json")
receipt_dir.mkdir(parents=True, exist_ok=True)
log_path.write_text(payload["log"], encoding="utf-8")
digest = hashlib.sha256(payload["fingerprint_seed"].encode("utf-8"))
for raw in sorted(payload["evidence_paths"]):
    path = Path(raw).expanduser()
    digest.update(raw.encode("utf-8"))
    if path.is_file():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1048576), b""):
                digest.update(chunk)
fingerprint = digest.hexdigest()
def portable(raw):
    path = Path(raw).expanduser()
    try:
        return str(path.resolve().relative_to(project_root))
    except (OSError, ValueError):
        return str(path)
evidence = [portable(raw) for raw in payload["evidence_paths"]]
evidence += [str(log_path.relative_to(project_root)), str(receipt_path.relative_to(project_root))]
record = {{
    "step": payload["step"],
    "status": payload["status"],
    "fingerprint": fingerprint,
    "started_at": payload["started_at"],
    "ended_at": payload["ended_at"],
    "backend": payload["backend"],
    "evidence": evidence,
    "reason": payload.get("reason"),
    "recorded_at": payload["ended_at"],
}}
receipt_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
run.setdefault("preparation", {{}})[payload["step"]] = record
history["updated_at"] = payload["ended_at"]
fd, temporary = tempfile.mkstemp(dir=history_path.parent, prefix="." + history_path.name + ".", suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(history, handle, ensure_ascii=False, indent=2)
        handle.write("\\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, history_path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print(json.dumps(record, ensure_ascii=False))
"""


def record_runner_result(
    *,
    history: str,
    run_id: str,
    step: str,
    backend: str,
    result_text: str,
    started_at: str,
    fingerprint_payload: Any,
    evidence_paths: list[str],
    skipped_not_required: bool = False,
    reason: str | None = None,
    ssh_command: str | None = None,
    ssh_password: str | None = None,
    cci_state: str | None = None,
) -> str:
    ended_at = now_iso()
    succeeded = str(result_text).startswith("✅")
    status = (
        "skipped_not_required"
        if succeeded and skipped_not_required
        else "completed"
        if succeeded
        else "failed"
    )
    normalized_reason = str(reason or "").strip()
    if status == "skipped_not_required" and not normalized_reason:
        normalized_reason = "该步骤没有投影命令，按计划明确跳过"
    if status == "failed" and not normalized_reason:
        normalized_reason = "runner 执行失败，详见步骤日志"
    fingerprint_seed = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if backend in {"ssh", "cci"}:
        if backend == "ssh" and not ssh_command:
            return f"{result_text}\n❌ 台账写入失败：缺少 SSH 连接信息。"
        if backend == "cci" and not cci_state:
            return f"{result_text}\n❌ 台账写入失败：缺少 CCI 状态文件。"
        payload = {
            "history": history,
            "run_id": run_id,
            "step": step,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "fingerprint_seed": fingerprint_seed,
            "evidence_paths": [str(item) for item in evidence_paths],
            "reason": normalized_reason or None,
            "log": result_text,
            "backend": backend,
        }
        try:
            if backend == "cci":
                scripts_dir = (
                    Path(__file__).resolve().parents[2]
                    / "ar24-instance-manage"
                    / "scripts"
                )
                sys.path.insert(0, str(scripts_dir))
                from cci.transport import execute_state_script

                source = _remote_recorder_source(payload)
                completed_result = execute_state_script(
                    cci_state,
                    "python3 - <<'PY_AR24_RECEIPT'\n"
                    f"{source}\n"
                    "PY_AR24_RECEIPT\n",
                    role="build",
                    timeout=120,
                )
                returncode = completed_result["returncode"]
                output = "\n".join(
                    item
                    for item in (
                        completed_result["stdout"],
                        completed_result["stderr"],
                    )
                    if item
                )
            else:
                command, env = _ssh_parts(ssh_command, ssh_password)
                completed = subprocess.run(
                    command,
                    input=_remote_recorder_source(payload),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                    **({"env": env} if env is not None else {}),
                )
                returncode = completed.returncode
                output = completed.stdout
        except Exception as exc:
            return f"{result_text}\n❌ 远端台账写入失败：{exc}"
        if returncode != 0:
            return f"{result_text}\n❌ 远端台账写入失败：{output}"
        label = "CCI 云端" if backend == "cci" else "远端"
        return f"{result_text}\n✅ {step} {label}执行证据已原子写入台账。"

    ledger_path = Path(history).expanduser().resolve()
    project_root = ledger_path.parent
    if step != "step_3" and step not in DIRECT_STEP_NAMES:
        raise ValueError(f"不支持的准备步骤: {step}")
    receipt_dir = project_root / run_id / step
    log_path = receipt_dir / f"{step}.log"
    receipt_path = receipt_dir / f"{step}_receipt.json"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    log_path.write_text(result_text, encoding="utf-8")
    evidence = []
    for item in [*evidence_paths, str(log_path), str(receipt_path)]:
        candidate = Path(item).expanduser()
        try:
            evidence.append(str(candidate.resolve().relative_to(project_root)))
        except (OSError, ValueError):
            evidence.append(str(candidate))
    fingerprint = _canonical_digest(fingerprint_payload, evidence_paths)
    receipt = {
        "step": step,
        "status": status,
        "fingerprint": fingerprint,
        "started_at": started_at,
        "ended_at": ended_at,
        "backend": "local",
        "evidence": evidence,
        "reason": normalized_reason or None,
    }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        execution_history.record_preparation(
            ledger_path,
            run_id,
            step,
            status=status,
            fingerprint=fingerprint,
            evidence=evidence,
            backend="local",
            reason=normalized_reason or None,
            started_at=started_at,
            ended_at=ended_at,
        )
    except Exception as exc:
        return f"{result_text}\n❌ 本地台账写入失败：{exc}"
    return f"{result_text}\n✅ {step} 执行证据已原子写入台账。"
