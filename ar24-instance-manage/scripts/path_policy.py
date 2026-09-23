#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run-centric v3 path policy for AR24 paper reproduction.

The user-confirmed base directory is the reproduction root.  Shared assets live
at the root while every reproduction attempt owns a complete ``run-NNN`` step
tree.  This module prepares only the root and shared directories; run
directories are reserved atomically by ``execution_history.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path


LAYOUT_SCHEMA = "ar24-path-layout/v3"
LAYOUT_VERSION = 3
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
SHARED_NAMES = ("dataset", "model", "envs")
ROOT_FILE_NAMES = ("path_layout.json", "execution_run_history.json")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
RUN_NAME_RE = re.compile(r"^run-(\d{3,})$")
LEGACY_STEP_RE = re.compile(r"^step_(?:precheck|0|1|2|2_5|3|4|5|6|7|7_5|8)$")


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _absolute_path_error(path: str, label: str) -> str:
    expanded = os.path.expanduser(path)
    if os.name == "nt":
        drive, _ = os.path.splitdrive(expanded)
        if expanded.startswith(("/", "\\")) and not drive:
            return (
                f"{label} must be a fully qualified Windows path such as "
                f"C:\\\\data or a UNC path; POSIX paths such as {path} must "
                "be validated on the SSH/Linux backend"
            )
    if not os.path.isabs(expanded):
        return f"{label} must be an absolute path: {path}"
    return ""


def _is_usable_dir(path: str) -> tuple[bool, str]:
    if not os.path.exists(path):
        return False, f"path does not exist: {path}"
    if not os.path.isdir(path):
        return False, f"path is not a directory: {path}"
    missing = []
    for mode, label in ((os.R_OK, "read"), (os.W_OK, "write"), (os.X_OK, "enter")):
        if not os.access(path, mode):
            missing.append(label)
    if missing:
        return False, f"path lacks {'/'.join(missing)} permission: {path}"
    return True, ""


def validate_base_root(base_root: str) -> dict:
    if not base_root or not str(base_root).strip():
        return {
            "ok": False,
            "status": "invalid_base_root",
            "retryable": True,
            "retry_scope": "base_root",
            "reason": "base root is empty",
        }
    absolute_error = _absolute_path_error(str(base_root), "base root")
    if absolute_error:
        return {
            "ok": False,
            "status": "invalid_base_root",
            "retryable": True,
            "retry_scope": "base_root",
            "reason": absolute_error,
        }
    path = _abs(base_root)
    ok, reason = _is_usable_dir(path)
    return {
        "ok": ok,
        "status": "ready" if ok else "invalid_base_root",
        "retryable": not ok,
        "retry_scope": "base_root" if not ok else None,
        "reason": reason,
        "base_root": path,
    }


def validate_repo_name(repo_name: str) -> str:
    value = str(repo_name or "").strip()
    if (
        not value
        or value in {".", ".."}
        or ".." in value
        or not REPO_NAME_RE.fullmatch(value)
        or Path(value).name != value
    ):
        raise ValueError(
            "repo_name must be one safe path segment containing only "
            "letters, numbers, dot, underscore or hyphen"
        )
    return value


def validate_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not RUN_NAME_RE.fullmatch(value):
        raise ValueError("run_id must use the form run-NNN")
    return value


def run_paths(root: str, repo_name: str, run_id: str) -> dict:
    repo = validate_repo_name(repo_name)
    run = validate_run_id(run_id)
    root_path = Path(_abs(root))
    run_root = root_path / run
    steps = {name: str(run_root / name) for name in STEP_NAMES}
    return {
        "run_id": run,
        "run_root": str(run_root),
        "step_dirs": steps,
        "code_dir": str(run_root / "step_3" / "code"),
        "prep_plan": str(run_root / "step_4" / "prep_plan.json"),
        "audit_report": str(run_root / "step_1" / f"{repo}_Audit_Report.md"),
        "audit_score": str(run_root / "step_1" / "audit_score.json"),
        "report_path": str(
            run_root / "step_8" / f"{repo}_final_reproduce_report.docx"
        ),
    }


def _layout_payload(root: str, repo_name: str) -> dict:
    root_path = Path(root)
    return {
        "schema_version": LAYOUT_SCHEMA,
        "layout_version": LAYOUT_VERSION,
        "repo_name": repo_name,
        "base_root": root,
        "repro_root": root,
        "workspace_root": root,
        "output_root": root,
        "envs_root": str(root_path / "envs"),
        "env_path": str(root_path / "envs" / repo_name),
        "dataset_dir": str(root_path / "dataset"),
        "model_dir": str(root_path / "model"),
        "history_path": str(root_path / "execution_run_history.json"),
        "path_layout": str(root_path / "path_layout.json"),
        "run_name_pattern": "run-NNN",
        "step_names": list(STEP_NAMES),
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _validate_existing_entries(root_path: Path) -> tuple[bool, str]:
    unexpected = []
    legacy = []
    for item in root_path.iterdir():
        name = item.name
        if name == "ar24" or LEGACY_STEP_RE.fullmatch(name):
            legacy.append(name)
            continue
        if (
            name in SHARED_NAMES
            or name in ROOT_FILE_NAMES
            or RUN_NAME_RE.fullmatch(name)
        ):
            continue
        unexpected.append(name)
    if legacy:
        return (
            False,
            "base root contains an incompatible legacy layout: "
            + ", ".join(sorted(legacy)),
        )
    if unexpected:
        return (
            False,
            "base root is dedicated to one reproduction project and contains "
            "unsupported entries: " + ", ".join(sorted(unexpected)),
        )
    return True, ""


def _prepare_layout(root: str, repo_name: str) -> tuple[bool, str, dict | None]:
    root_path = Path(root)
    valid, reason = _validate_existing_entries(root_path)
    if not valid:
        return False, reason, None

    expected_layout = _layout_payload(root, repo_name)
    layout_path = root_path / "path_layout.json"
    if layout_path.exists():
        if not layout_path.is_file():
            return False, f"path layout is not a file: {layout_path}", None
        try:
            existing = json.loads(layout_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return False, f"existing path layout is unreadable: {exc}", None
        if (
            existing.get("schema_version") != LAYOUT_SCHEMA
            or existing.get("layout_version") != LAYOUT_VERSION
        ):
            return False, "existing path layout is not compatible with v3", None
        if existing.get("repo_name") != repo_name:
            return (
                False,
                "base root belongs to a different repository: "
                f"{existing.get('repo_name')!r}",
                None,
            )
        if existing.get("repro_root") != root:
            return False, "existing path layout points to a different root", None
        mismatched = [
            key
            for key in (
                "base_root",
                "workspace_root",
                "output_root",
                "envs_root",
                "env_path",
                "dataset_dir",
                "model_dir",
                "history_path",
            )
            if existing.get(key) != expected_layout[key]
        ]
        if mismatched:
            return (
                False,
                "existing path layout contains incompatible paths: "
                + ", ".join(mismatched),
                None,
            )

    for name in SHARED_NAMES:
        target = root_path / name
        if target.exists() and not target.is_dir():
            return False, f"shared layout path is not a directory: {target}", None

    for item in root_path.iterdir():
        if not RUN_NAME_RE.fullmatch(item.name):
            continue
        if not item.is_dir():
            return False, f"run path is not a directory: {item}", None
        for step in STEP_NAMES:
            step_path = item / step
            if not step_path.is_dir():
                return (
                    False,
                    f"existing run is missing required step directory: {step_path}",
                    None,
                )

    history_path = root_path / "execution_run_history.json"
    if history_path.exists():
        if not history_path.is_file():
            return False, f"execution history is not a file: {history_path}", None
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return False, f"existing execution history is unreadable: {exc}", None
        if (
            history.get("schema_version") != "2.0"
            or history.get("layout_version") != LAYOUT_VERSION
        ):
            return False, "existing execution history is not compatible with v3", None
        if history.get("project_name") != repo_name:
            return (
                False,
                "base root execution history belongs to a different repository: "
                f"{history.get('project_name')!r}",
                None,
            )
        expected_history_paths = {
            "workspace_root": root,
        }
        shared = history.get("shared_paths")
        if not isinstance(shared, dict):
            return False, "existing execution history lacks v3 shared_paths", None
        expected_history_paths.update(
            {
                "dataset_dir": expected_layout["dataset_dir"],
                "model_dir": expected_layout["model_dir"],
                "envs_root": expected_layout["envs_root"],
                "env_path": expected_layout["env_path"],
            }
        )
        if history.get("workspace_root") != expected_history_paths["workspace_root"]:
            return False, "existing execution history points to a different root", None
        for key in ("dataset_dir", "model_dir", "envs_root", "env_path"):
            if shared.get(key) != expected_history_paths[key]:
                return (
                    False,
                    f"existing execution history has incompatible shared path: {key}",
                    None,
                )

    for name in SHARED_NAMES:
        (root_path / name).mkdir(parents=True, exist_ok=True)
    (root_path / "envs" / repo_name).mkdir(parents=True, exist_ok=True)

    layout = expected_layout
    _atomic_write_json(layout_path, layout)
    return True, "", layout


def derive_repro_paths(repro_root: str, repo_name: str) -> dict:
    repo = validate_repo_name(repo_name)
    root = _abs(repro_root)
    payload = _layout_payload(root, repo)
    payload["project_root"] = root
    payload["conda_envs_root"] = payload["envs_root"]
    return payload


def prepare_repro_paths(base_root: str, repo_name: str) -> dict:
    try:
        repo = validate_repo_name(repo_name)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "invalid_repo_name",
            "retryable": False,
            "retry_scope": None,
            "reason": str(exc),
        }
    base = validate_base_root(base_root)
    if not base["ok"]:
        return base

    root = base["base_root"]
    layout_ok, layout_reason, layout = _prepare_layout(root, repo)
    if not layout_ok:
        return {
            "ok": False,
            "status": "legacy_or_foreign_layout",
            "retryable": True,
            "retry_scope": "base_root",
            "base_root": root,
            "repro_root": root,
            "reason": layout_reason,
        }
    paths = derive_repro_paths(root, repo)
    paths.update(layout or {})
    paths.update(
        {
            "ok": True,
            "status": "ready",
            "retryable": False,
            "retry_scope": None,
            "reason": "",
        }
    )
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate AR24 v3 reproduction paths")
    parser.add_argument(
        "--base-root",
        required=True,
        help="Existing dedicated reproduction root, e.g. /root/nas/project",
    )
    parser.add_argument("--repo-name", required=True, help="Repository/project name")
    args = parser.parse_args()

    result = prepare_repro_paths(args.base_root, args.repo_name)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("ok") else 2)


if __name__ == "__main__":
    main()
