from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import redact_mapping, redact_text


SCHEMA_VERSION = "ar24-cci-state/v2"
VALID_ROLES = ("build", "execution", "recovery")
ACTIVE_STATES = {"creating", "running", "unknown", "release_failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state_root() -> Path:
    return Path(os.environ.get("AR24_CCI_STATE_ROOT", ".ar24/cci"))


class CciStateError(RuntimeError):
    pass


class CciStateStore:
    def __init__(self, path: str | Path) -> None:
        value = Path(path).expanduser().resolve()
        self.path = value if value.suffix == ".json" else value / "cci_state.json"

    @classmethod
    def for_session(
        cls,
        session_id: str,
        root: str | Path | None = None,
    ) -> "CciStateStore":
        if not session_id or "/" in session_id or session_id in {".", ".."}:
            raise ValueError("CCI session_id 只能是非空的单个路径段")
        base = Path(root) if root else default_state_root()
        return cls(base / session_id)

    def initialize(self, session_id: str, run_id: str | None = None) -> dict[str, Any]:
        if self.path.exists():
            state = self.load()
            if state.get("session_id") != session_id:
                raise CciStateError("CCI 状态文件属于其他 session")
            return state
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "run_id": run_id,
            "status": "initialized",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "resource_evidence": "cci_catalog_planned",
            "preflight": None,
            "selection": None,
            "resources": {
                role: {
                    "role": role,
                    "instance_id": None,
                    "result_key": None,
                    "lifecycle_status": "not_created",
                    "created_at": None,
                    "released_at": None,
                    "price": None,
                    "price_unit": None,
                    "last_detail": None,
                }
                for role in VALID_ROLES
            },
            "cleanup_required": [],
            "events": [],
        }
        self.save(state)
        return state

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CciStateError(f"CCI 状态文件不存在: {self.path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CciStateError(f"CCI 状态文件不可读: {self.path}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise CciStateError("CCI 状态文件 schema 无效")
        return value

    def save(self, state: dict[str, Any]) -> Path:
        safe = redact_mapping(state)
        safe["schema_version"] = SCHEMA_VERSION
        safe["updated_at"] = utc_now()
        serialized = json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for name in (
            "ALAYANEW_ACCESS_KEY",
            "ALAYANEW_SECRET_KEY",
            "ALAYANEW_ACCESS_TOKEN",
        ):
            secret = os.environ.get(name)
            if secret and secret in serialized:
                raise CciStateError(f"拒绝把 {name} 写入 CCI 状态")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return self.path

    def add_event(
        self,
        state: dict[str, Any],
        event: str,
        **details: Any,
    ) -> None:
        state.setdefault("events", []).append(
            {
                "at": utc_now(),
                "event": event,
                "details": redact_mapping(details),
            }
        )

    def active_resources(self, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        state = state or self.load()
        active = []
        for role, resource in state.get("resources", {}).items():
            if (
                resource.get("instance_id")
                and resource.get("lifecycle_status") in ACTIVE_STATES
            ):
                active.append(
                    {
                        "role": role,
                        "instance_id": resource["instance_id"],
                        "lifecycle_status": resource["lifecycle_status"],
                        "price": resource.get("price"),
                        "price_unit": resource.get("price_unit"),
                    }
                )
        return active

    def ensure_creation_allowed(self, state: dict[str, Any], role: str) -> None:
        cleanup = state.get("cleanup_required") or []
        if cleanup:
            raise CciStateError(
                "存在 cleanup_required 资源，必须先运行 cleanup；禁止创建新实例"
            )
        active = self.active_resources(state)
        if active:
            raise CciStateError(
                f"检测到未释放的 CCI 资源 {active}；必须先恢复或清理，禁止重复申请"
            )
        if role == "execution":
            build = state["resources"]["build"]
            if build.get("instance_id") and build.get("lifecycle_status") != "released":
                raise CciStateError("构建实例尚未成功释放，不能创建 GPU 执行实例")

    def mark_cleanup_required(
        self,
        state: dict[str, Any],
        *,
        role: str,
        instance_id: str | None,
        reason: str,
    ) -> None:
        entry = {
            "role": role,
            "instance_id": instance_id,
            "reason": redact_text(reason),
            "recorded_at": utc_now(),
        }
        if entry not in state.setdefault("cleanup_required", []):
            state["cleanup_required"].append(entry)
        state["status"] = "cleanup_required"
        if role in state.get("resources", {}):
            state["resources"][role]["lifecycle_status"] = "release_failed"
