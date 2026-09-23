#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cci.client import (
    AlayaNewCciClient,
    MissingCciCredentials,
    MissingCciDependency,
)
from cci.models import CciImage, CciProduct, CciResourceSpec, NasStorage
from cci.provider import CciCloudProvider, extract_instance_id
from cci.redaction import redact_mapping, redact_text
from cci.state import CciStateError, CciStateStore, utc_now
from cci.transport import CciTransport, CciTransportError, parse_last_json_line
from path_policy import (
    LAYOUT_SCHEMA,
    LAYOUT_VERSION,
    SHARED_NAMES,
    STEP_NAMES,
    prepare_repro_paths,
    validate_repo_name,
)


DEFAULT_CONFIRMATION_TTL_SECONDS = 24 * 60 * 60
REMOTE_EVIDENCE_KEYS = {
    "remote_path",
    "nas_mount_path",
    "repro_base_root",
    "repro_root",
}


def _provider() -> CciCloudProvider:
    return CciCloudProvider(AlayaNewCciClient.from_env())


def _remap_path_values(
    value: Any,
    old_root: str,
    new_root: str,
    *,
    key: str | None = None,
    preserve_remote: bool = False,
) -> Any:
    if preserve_remote and key in REMOTE_EVIDENCE_KEYS:
        return value
    if isinstance(value, dict):
        return {
            item_key: _remap_path_values(
                item_value,
                old_root,
                new_root,
                key=str(item_key),
                preserve_remote=(
                    preserve_remote or str(item_key) == "cloud_resources"
                ),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [
            _remap_path_values(
                item,
                old_root,
                new_root,
                preserve_remote=preserve_remote,
            )
            for item in value
        ]
    if isinstance(value, str):
        if value == old_root:
            return new_root
        prefix = old_root.rstrip("/") + "/"
        if value.startswith(prefix):
            return new_root.rstrip("/") + "/" + value[len(prefix):]
    return value


def _remap_v3_control_files(
    root: str | Path,
    *,
    old_root: str,
    new_root: str,
) -> None:
    root_path = Path(root).expanduser().resolve()
    for name in ("path_layout.json", "execution_run_history.json"):
        path = root_path / name
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        remapped = _remap_path_values(payload, old_root, new_root)
        temporary = path.with_name(f".{path.name}.cci-remap")
        try:
            temporary.write_text(
                json.dumps(remapped, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _remote_remap_script(
    remote_root: str,
    *,
    old_root: str,
    new_root: str,
) -> str:
    return "\n".join(
        [
            "set -e",
            "python3 - <<'PY_AR24_REMAP'",
            "import json, os",
            "from pathlib import Path",
            f"root = Path({remote_root!r})",
            f"old_root = {old_root!r}",
            f"new_root = {new_root!r}",
            f"preserve = {sorted(REMOTE_EVIDENCE_KEYS)!r}",
            "def remap(value, key=None, preserve_remote=False):",
            "    if preserve_remote and key in preserve:",
            "        return value",
            "    if isinstance(value, dict):",
            "        return {item_key: remap(item_value, str(item_key), preserve_remote or str(item_key) == 'cloud_resources') for item_key, item_value in value.items()}",
            "    if isinstance(value, list):",
            "        return [remap(item, preserve_remote=preserve_remote) for item in value]",
            "    if isinstance(value, str):",
            "        if value == old_root:",
            "            return new_root",
            "        prefix = old_root.rstrip('/') + '/'",
            "        if value.startswith(prefix):",
            "            return new_root.rstrip('/') + '/' + value[len(prefix):]",
            "    return value",
            "for name in ('path_layout.json', 'execution_run_history.json'):",
            "    path = root / name",
            "    if not path.is_file():",
            "        continue",
            "    payload = remap(json.loads(path.read_text(encoding='utf-8')))",
            "    temporary = path.with_name('.' + path.name + '.cci-remap')",
            "    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')",
            "    os.replace(temporary, path)",
            "PY_AR24_REMAP",
        ]
    )


def preflight(
    store: CciStateStore,
    *,
    session_id: str,
    run_id: str | None = None,
    provider: CciCloudProvider | None = None,
    aidc_id: int = 5,
) -> dict[str, Any]:
    state = store.initialize(session_id, run_id=run_id)
    active = store.active_resources(state)
    if active or state.get("cleanup_required"):
        provider = provider or _provider()
        status_resources(store, provider=provider)
        state = store.load()
        active = store.active_resources(state)
    if active or state.get("cleanup_required"):
        state["status"] = "cleanup_required"
        store.save(state)
        return {
            "status": "cleanup_required",
            "state_path": str(store.path),
            "active_resources": active,
            "cleanup_required": state.get("cleanup_required", []),
            "next_action": f"python3 {Path(__file__).name} --state {store.path} cleanup",
        }

    provider = provider or _provider()
    products = [
        item
        for item in provider.list_products(aidc_id=aidc_id)
        if item.aidc_id == aidc_id
    ]
    images = provider.list_public_images(aidc_id=aidc_id)
    nas_items = [item for item in provider.list_nas() if item.aidc_id == aidc_id]
    snapshot = {
        "generated_at": utc_now(),
        "aidc_id": aidc_id,
        "resource_evidence": "cci_catalog_planned",
        "cpu_products": [
            item.to_safe_dict()
            for item in products
            if item.gpu_count == 0
        ],
        "gpu_products": [
            item.to_safe_dict()
            for item in products
            if item.gpu_count > 0
        ],
        "images": [item.to_safe_dict() for item in images if item.aidc_id == aidc_id],
        "nas": [item.to_safe_dict() for item in nas_items],
    }
    missing: list[str] = []
    if not snapshot["cpu_products"]:
        missing.append("available_cpu_product")
    if not snapshot["gpu_products"]:
        missing.append("available_gpu_product")
    if not snapshot["images"]:
        missing.append("image")
    if not any(item.get("writable") for item in snapshot["nas"]):
        missing.append("existing_writable_nas")

    state["preflight"] = snapshot
    state["selection"] = None
    state["status"] = "waiting_for_input" if missing else "waiting_for_selection"
    state["missing_inputs"] = missing
    store.add_event(state, "preflight_completed", missing_inputs=missing)
    store.save(state)
    return {
        "status": state["status"],
        "state_path": str(store.path),
        "resource_evidence": "cci_catalog_planned",
        "preflight": snapshot,
        "missing_inputs": missing,
        "next_action": (
            "明确选择 CPU 构建规格、GPU 执行规格、同一镜像和已有 NAS，"
            "并确认 NAS 内项目专用精确根目录后运行 confirm-selection"
        ),
    }


def confirm_selection(
    store: CciStateStore,
    *,
    repo_name: str,
    run_id: str | None,
    exact_base_root: str,
    build_product_code: str,
    execution_product_code: str,
    image_key: str,
    nas_id: str,
    confirmation_id: str,
    user_response: str,
    ttl_seconds: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
) -> dict[str, Any]:
    state = store.load()
    snapshot = state.get("preflight")
    if not isinstance(snapshot, dict):
        raise CciStateError("必须先执行最新只读 preflight")
    if not confirmation_id.strip() or not user_response.strip():
        raise CciStateError("必须保存用户明确确认的 confirmation_id 和原始回复证据")
    if ttl_seconds <= 0:
        raise CciStateError("确认有效期必须大于 0")
    try:
        repo = validate_repo_name(repo_name)
    except ValueError as exc:
        raise CciStateError(str(exc)) from exc

    build = _find(snapshot["cpu_products"], "product_code", build_product_code)
    execution = _find(
        snapshot["gpu_products"], "product_code", execution_product_code
    )
    image = _find(snapshot["images"], "selection_key", image_key)
    nas = _find(snapshot["nas"], "storage_id", nas_id)
    if not build.get("available") or not execution.get("available"):
        raise CciStateError("所选规格在最新预检中无库存")
    if not nas.get("mount_path"):
        raise CciStateError("所选 NAS 缺少挂载路径")
    if nas.get("writable") is False:
        raise CciStateError("所选 NAS 在最新预检中不是可写状态")
    mount_path = PurePosixPath(str(nas["mount_path"]))
    if not mount_path.is_absolute() or str(mount_path) == "/":
        raise CciStateError("NAS 挂载路径必须是非根目录的绝对路径")
    if not str(exact_base_root or "").strip():
        raise CciStateError("必须明确确认 NAS 上的精确复现基准目录")
    base_root = PurePosixPath(str(exact_base_root).strip())
    if (
        not base_root.is_absolute()
        or str(base_root) == "/"
        or ".." in base_root.parts
    ):
        raise CciStateError("精确复现基准目录必须是 NAS 内非根目录绝对路径")
    if base_root != mount_path and mount_path not in base_root.parents:
        raise CciStateError("精确复现基准目录必须位于所选 NAS 挂载路径内")

    aidc_ids = {
        int(build["aidc_id"]),
        int(execution["aidc_id"]),
        int(image["aidc_id"]),
        int(nas["aidc_id"]),
    }
    if len(aidc_ids) != 1:
        raise CciStateError("构建规格、GPU 规格、镜像和 NAS 必须属于同一 AIDC")

    confirmed_at = datetime.now(timezone.utc)
    staging_base = (
        Path(os.environ.get("AR24_CCI_STAGING_ROOT", ".ar24/cci-staging"))
        / str(state["session_id"])
    ).resolve()
    staging_base.mkdir(parents=True, exist_ok=True)
    staging_layout = prepare_repro_paths(str(staging_base), repo)
    if not staging_layout.get("ok"):
        raise CciStateError(
            f"无法创建 CCI 本地 v3 暂存布局: {staging_layout.get('reason')}"
        )
    selection = {
        "run_id": run_id,
        "repo_name": repo,
        "aidc_id": aidc_ids.pop(),
        "build_product": build,
        "execution_product": execution,
        "image": image,
        "nas": nas,
        "nas_mount_path": str(mount_path),
        "repro_base_root": str(base_root),
        "repro_root": str(base_root),
        "local_staging_root": staging_layout["repro_root"],
        "confirmation": {
            "confirmation_id": confirmation_id,
            "decision_source": "explicit_user_reply",
            "user_response": redact_text(user_response),
            "confirmed_at": confirmed_at.isoformat(),
            "expires_at": (
                confirmed_at + timedelta(seconds=ttl_seconds)
            ).isoformat(),
        },
    }
    if run_id:
        state["run_id"] = run_id
    state["selection"] = selection
    state["status"] = "selection_confirmed"
    state["missing_inputs"] = []
    store.add_event(
        state,
        "selection_confirmed",
        repo_name=repo,
        confirmation_id=confirmation_id,
        build_product_code=build_product_code,
        execution_product_code=execution_product_code,
        image_key=image_key,
        nas_id=nas_id,
    )
    store.save(state)
    return {
        "status": "selection_confirmed",
        "state_path": str(store.path),
        "selection": selection,
        "billing_warning": (
            "平台自动停止与自动释放已关闭；实例创建后将持续计费，"
            "直到 AR24 主动 release 成功"
        ),
    }


def create_resource(
    store: CciStateStore,
    *,
    role: str,
    provider: CciCloudProvider | None = None,
    plan_requirements: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if role not in {"build", "execution", "recovery"}:
        raise CciStateError(f"不支持的 CCI 实例角色: {role}")
    state = store.load()
    selection = _valid_selection(state)
    existing_run_id = str(state.get("run_id") or selection.get("run_id") or "").strip()
    requested_run_id = str(run_id or "").strip()
    if existing_run_id and requested_run_id and existing_run_id != requested_run_id:
        raise CciStateError("CCI 状态已绑定其他 run_id，禁止复用付费资源")
    effective_run_id = existing_run_id or requested_run_id
    if not effective_run_id:
        raise CciStateError("创建实例前必须用 --run-id 绑定 step_2.5 生成的执行轮次")
    state["run_id"] = effective_run_id
    selection["run_id"] = effective_run_id
    state["selection"] = selection
    store.save(state)
    store.ensure_creation_allowed(state, role)
    if role == "execution" and plan_requirements:
        selection["plan_requirements"] = {
            key: value
            for key, value in plan_requirements.items()
            if value is not None
        }
        state["selection"] = selection
        store.save(state)
    provider = provider or _provider()

    aidc_id = int(selection["aidc_id"])
    selected_product = (
        selection["execution_product"]
        if role == "execution"
        else selection["build_product"]
    )
    products = provider.list_products(aidc_id=aidc_id)
    current_product = next(
        (
            item
            for item in products
            if item.product_code == selected_product["product_code"]
            and item.aidc_id == aidc_id
        ),
        None,
    )
    if current_product is None or not current_product.available:
        raise CciStateError("创建前刷新库存失败：已确认规格不存在或已无库存")
    current_product_safe = current_product.to_safe_dict()
    changed_fields = [
        key
        for key in (
            "cpu_cores",
            "memory_gb",
            "gpu_name",
            "gpu_count",
            "vram_gb",
            "price",
            "price_unit",
        )
        if current_product_safe.get(key) != selected_product.get(key)
    ]
    if changed_fields:
        raise CciStateError(
            "创建前刷新发现已确认规格或价格发生变化："
            + "、".join(changed_fields)
            + "；必须重新 preflight 并由用户明确确认"
        )

    current_images = provider.list_public_images(aidc_id=aidc_id)
    selected_image_key = selection["image"]["selection_key"]
    image = next(
        (item for item in current_images if item.selection_key == selected_image_key),
        None,
    )
    if image is None:
        raise CciStateError("创建前刷新失败：已确认镜像不再可用")
    current_nas = provider.list_nas()
    nas = next(
        (
            item
            for item in current_nas
            if item.storage_id == selection["nas"]["storage_id"]
            and item.aidc_id == aidc_id
        ),
        None,
    )
    if (
        nas is None
        or not nas.writable
        or nas.mount_path != selection["nas"]["mount_path"]
        or nas.storage_type != selection["nas"].get("storage_type")
    ):
        raise CciStateError("创建前刷新失败：已确认 NAS 不存在或挂载路径发生变化")

    spec = CciResourceSpec(
        run_id=effective_run_id,
        role=role,
        product=current_product,
        image=image,
        nas=nas,
        aidc_id=aidc_id,
    )
    resource = state["resources"][role]
    resource.update(
        {
            "lifecycle_status": "creating",
            "price": current_product.price,
            "price_unit": current_product.price_unit,
            "created_at": utc_now(),
        }
    )
    state["status"] = f"{role}_creating"
    store.add_event(state, "create_started", role=role)
    store.save(state)
    try:
        result_key = provider.create_cci_instance(spec)
        resource["result_key"] = result_key
        store.save(state)
        result = provider.poll_cci_result(result_key)
        instance_id = extract_instance_id(result)
    except Exception as exc:
        if getattr(exc, "instance_id", None):
            resource["instance_id"] = str(exc.instance_id)
        store.mark_cleanup_required(
            state,
            role=role,
            instance_id=resource.get("instance_id"),
            reason=f"CCI 创建结果不明确: {exc}",
        )
        store.add_event(state, "create_ambiguous", role=role, error=str(exc))
        store.save(state)
        raise CciStateError(
            "CCI 创建结果不明确，已进入 cleanup_required；"
            f"请运行 cleanup 并核对 result_key={resource.get('result_key')}"
        ) from exc

    resource.update(
        {
            "instance_id": instance_id,
            "lifecycle_status": "running",
            "last_detail": _safe_instance_summary(result),
        }
    )
    state["status"] = f"{role}_running"
    store.add_event(
        state,
        "create_succeeded",
        role=role,
        instance_id=instance_id,
        result_key=resource.get("result_key"),
    )
    store.save(state)
    return {
        "status": "running",
        "role": role,
        "instance_id": instance_id,
        "price": resource.get("price"),
        "price_unit": resource.get("price_unit"),
        "auto_stop_enable": False,
        "auto_release_enable": False,
        "billing_warning": "实例正在计费；必须在终态路径中主动 release",
    }


def status_resources(
    store: CciStateStore,
    *,
    provider: CciCloudProvider | None = None,
) -> dict[str, Any]:
    state = store.load()
    provider = provider or _provider()
    details: list[dict[str, Any]] = []
    for role, resource in state["resources"].items():
        instance_id = resource.get("instance_id")
        if not instance_id or resource.get("lifecycle_status") == "released":
            continue
        try:
            detail = _safe_instance_summary(provider.instance_detail(instance_id))
            resource["last_detail"] = detail
            remote_status = str(
                detail.get("status")
                or detail.get("sub_status")
                or detail.get("state")
                or ""
            ).strip().casefold()
            if remote_status in {
                "released",
                "deleted",
                "terminated",
                "not_found",
                "not found",
            }:
                resource["lifecycle_status"] = "released"
                resource["released_at"] = (
                    detail.get("released_at")
                    or utc_now()
                )
                resource.update(_cost_evidence(resource))
            details.append({"role": role, "instance_id": instance_id, "detail": detail})
        except Exception as exc:
            resource["lifecycle_status"] = (
                "missing" if _already_released(exc) else "unknown"
            )
            details.append(
                {
                    "role": role,
                    "instance_id": instance_id,
                    "status": resource["lifecycle_status"],
                    "error": redact_text(exc),
                }
            )
    store.save(state)
    return {
        "status": state["status"],
        "active_resources": store.active_resources(state),
        "details": details,
        "cleanup_required": state.get("cleanup_required", []),
    }


def confirm_measured_resources(
    store: CciStateStore,
    *,
    confirmation_id: str,
    user_response: str,
    minimum_gpu_count: int | None = None,
    minimum_vram_gb: float | None = None,
) -> dict[str, Any]:
    if not confirmation_id.strip() or not user_response.strip():
        raise CciStateError("必须提供用户明确回复的 confirmation_id 和原始证据")
    state = store.load()
    selection = state.get("selection") or {}
    resource = state.get("resources", {}).get("execution") or {}
    measured = resource.get("measured_probe")
    if not isinstance(measured, dict) or not measured.get("resource_mismatches"):
        raise CciStateError("没有待重新确认的 GPU 实测资源差异")
    actual_count = int(measured.get("gpu_count") or 0)
    actual_vram = min(
        (
            float(item["vram_gb"])
            for item in measured.get("gpus", [])
            if item.get("vram_gb") is not None
        ),
        default=None,
    )
    if minimum_gpu_count is not None and actual_count < minimum_gpu_count:
        raise CciStateError("实测 GPU 数量仍低于重新确认后的计划最低要求")
    if minimum_vram_gb is not None and (
        actual_vram is None or actual_vram < minimum_vram_gb
    ):
        raise CciStateError("实测显存仍低于重新确认后的计划最低要求")
    if measured.get("path_policy", {}).get("status") != "ready":
        raise CciStateError("NAS 派生路径不可写，不能通过资源重新确认")

    selection["plan_requirements"] = {
        "minimum_gpu_count": minimum_gpu_count,
        "minimum_vram_gb": minimum_vram_gb,
    }
    selection["measured_resource_confirmation"] = {
        "confirmation_id": confirmation_id,
        "decision_source": "explicit_user_reply",
        "user_response": redact_text(user_response),
        "confirmed_at": utc_now(),
        "accepted_gpu_count": actual_count,
        "accepted_minimum_vram_gb": minimum_vram_gb,
    }
    measured["step_7_allowed"] = True
    measured["resource_mismatches_acknowledged"] = list(
        measured["resource_mismatches"]
    )
    state["selection"] = selection
    state["status"] = "execution_running"
    store.add_event(
        state,
        "measured_resources_confirmed",
        confirmation_id=confirmation_id,
        instance_id=resource.get("instance_id"),
    )
    store.save(state)
    return {
        "status": "measured_resources_confirmed",
        "instance_id": resource.get("instance_id"),
        "step_7_allowed": True,
        "measured_gpu_count": actual_count,
        "measured_minimum_vram_gb": actual_vram,
    }


def ensure_execution_ready(store: CciStateStore) -> None:
    state = store.load()
    resource = state.get("resources", {}).get("execution") or {}
    measured = resource.get("measured_probe")
    if not isinstance(measured, dict) or not measured.get("step_7_allowed"):
        raise CciStateError(
            "GPU 实例尚未完成真实探测与资源适配确认，禁止执行 step_7"
        )


def release_resource(
    store: CciStateStore,
    *,
    role: str,
    provider: CciCloudProvider | None = None,
) -> dict[str, Any]:
    state = store.load()
    if role not in state["resources"]:
        raise CciStateError(f"不支持的 CCI 实例角色: {role}")
    resource = state["resources"][role]
    instance_id = resource.get("instance_id")
    if not instance_id or resource.get("lifecycle_status") == "released":
        return {"status": "skipped", "role": role, "reason": "no_active_resource"}
    provider = provider or _provider()
    try:
        provider.release_instance(instance_id)
    except Exception as exc:
        if not _already_released(exc):
            store.mark_cleanup_required(
                state,
                role=role,
                instance_id=instance_id,
                reason=str(exc),
            )
            store.add_event(
                state,
                "release_failed",
                role=role,
                instance_id=instance_id,
                error=str(exc),
            )
            store.save(state)
            return {
                "status": "cleanup_required",
                "role": role,
                "instance_id": instance_id,
                "billing_status": "unknown_or_active",
                "manual_cleanup_command": (
                    f"python3 {Path(__file__).name} --state {store.path} cleanup"
                ),
                "error": redact_text(exc),
            }
    resource["lifecycle_status"] = "released"
    resource["released_at"] = utc_now()
    resource.update(_cost_evidence(resource))
    state["cleanup_required"] = [
        item
        for item in state.get("cleanup_required", [])
        if item.get("role") != role
    ]
    state["status"] = (
        "selection_confirmed"
        if role == "build"
        else "resources_released"
    )
    store.add_event(
        state,
        "release_succeeded",
        role=role,
        instance_id=instance_id,
    )
    store.save(state)
    return {
        "status": "released",
        "role": role,
        "instance_id": instance_id,
        "released_at": resource["released_at"],
    }


def cleanup_resources(
    store: CciStateStore,
    *,
    provider: CciCloudProvider | None = None,
) -> dict[str, Any]:
    state = store.load()
    provider = provider or _provider()
    results: list[dict[str, Any]] = []
    for role in ("execution", "recovery", "build"):
        resource = state["resources"][role]
        if not resource.get("instance_id") and resource.get("result_key"):
            try:
                result = provider.poll_cci_result(resource["result_key"])
                resource["instance_id"] = extract_instance_id(result)
                resource["lifecycle_status"] = "unknown"
                store.save(state)
            except Exception as exc:
                results.append(
                    {
                        "status": "cleanup_required",
                        "role": role,
                        "instance_id": None,
                        "result_key": resource.get("result_key"),
                        "error": redact_text(exc),
                    }
                )
                continue
        results.append(release_resource(store, role=role, provider=provider))
        state = store.load()
    remaining = store.active_resources(state)
    cleanup = state.get("cleanup_required", [])
    return {
        "status": "released" if not remaining and not cleanup else "cleanup_required",
        "results": results,
        "active_resources": remaining,
        "cleanup_required": cleanup,
    }


def probe_resource(
    store: CciStateStore,
    *,
    role: str,
    provider: CciCloudProvider | None = None,
) -> dict[str, Any]:
    from probe import build_remote_probe_script

    transport = CciTransport.from_state(
        store.path,
        role=role,
        provider=provider,
    )
    result = transport.execute_script(build_remote_probe_script(), timeout=120)
    if result["returncode"] != 0:
        return {
            "backend": "cci",
            "role": role,
            "status": "error",
            "ready": False,
            "blockers": [result["stderr"] or result["stdout"]],
        }
    measured = parse_last_json_line(result["stdout"])
    state = store.load()
    selection = state.get("selection") or {}
    repro_root = selection.get("repro_root")
    path_policy: dict[str, Any] = {
        "status": "error",
        "repro_root": repro_root,
    }
    if repro_root:
        quoted_root = shlex.quote(str(repro_root))
        repo_name = str(selection.get("repo_name") or "")
        remote_layout = {
            "schema_version": LAYOUT_SCHEMA,
            "repo_name": repo_name,
            "layout_version": LAYOUT_VERSION,
            "base_root": repro_root,
            "repro_root": repro_root,
            "workspace_root": repro_root,
            "output_root": repro_root,
            "envs_root": f"{repro_root}/envs",
            "env_path": f"{repro_root}/envs/{repo_name}",
            "dataset_dir": f"{repro_root}/dataset",
            "model_dir": f"{repro_root}/model",
            "history_path": f"{repro_root}/execution_run_history.json",
            "path_layout": f"{repro_root}/path_layout.json",
            "run_name_pattern": "run-NNN",
            "step_names": list(STEP_NAMES),
        }
        layout_source = "\n".join(
            [
                "import json, os, pathlib",
                f"root = pathlib.Path({str(repro_root)!r})",
                "import re",
                f"shared = set({list(SHARED_NAMES)!r})",
                "root_files = {'path_layout.json', 'execution_run_history.json'}",
                "run_re = re.compile(r'^run-\\d{3,}$')",
                f"steps = {list(STEP_NAMES)!r}",
                "root.mkdir(parents=True, exist_ok=True)",
                "unexpected = sorted(p.name for p in root.iterdir() if p.name not in shared and p.name not in root_files and not run_re.fullmatch(p.name))",
                "if unexpected:",
                "    raise SystemExit('unsupported legacy or foreign layout: ' + ', '.join(unexpected))",
                "for name in shared:",
                "    target = root / name",
                "    if target.exists() and not target.is_dir():",
                "        raise SystemExit('layout path is not a directory: ' + str(target))",
                "for name in root_files:",
                "    target = root / name",
                "    if target.exists() and not target.is_file():",
                "        raise SystemExit('layout root file is not a file: ' + str(target))",
                "for run_dir in (p for p in root.iterdir() if run_re.fullmatch(p.name)):",
                "    if not run_dir.is_dir():",
                "        raise SystemExit('run path is not a directory: ' + str(run_dir))",
                "    missing = [name for name in steps if not (run_dir / name).is_dir()]",
                "    if missing:",
                "        raise SystemExit('run is missing required steps: ' + ', '.join(missing))",
                f"payload = {remote_layout!r}",
                "layout_path = root / 'path_layout.json'",
                "if layout_path.exists():",
                "    try:",
                "        existing = json.loads(layout_path.read_text(encoding='utf-8'))",
                "    except Exception as exc:",
                "        raise SystemExit('incompatible path_layout.json: ' + str(exc))",
                f"    if existing.get('schema_version') != {LAYOUT_SCHEMA!r} or existing.get('layout_version') != {LAYOUT_VERSION!r}:",
                "        raise SystemExit('incompatible path_layout.json schema or layout version')",
                f"    if existing.get('repo_name') != {repo_name!r}:",
                "        raise SystemExit('path_layout.json belongs to a different repo_name')",
                "    if pathlib.Path(str(existing.get('repro_root') or '')).resolve() != root.resolve():",
                "        raise SystemExit('path_layout.json points to a different repro_root')",
                "for name in shared:",
                "    (root / name).mkdir(parents=True, exist_ok=True)",
                f"(root / 'envs' / {repo_name!r}).mkdir(parents=True, exist_ok=True)",
                "temporary = layout_path.with_name('.path_layout.json.tmp')",
                "temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')",
                "os.replace(temporary, layout_path)",
            ]
        )
        path_result = transport.execute_script(
            "\n".join(
                [
                    "set -e",
                    "python3 - <<'PY_AR24_LAYOUT'",
                    layout_source,
                    "PY_AR24_LAYOUT",
                    f"probe_file=$(mktemp {quoted_root}/.ar24-write-test.XXXXXX)",
                    "printf 'ar24-cci-path-probe\\n' > \"$probe_file\"",
                    "test -s \"$probe_file\"",
                    "rm -f \"$probe_file\"",
                ]
            ),
            timeout=60,
        )
        path_policy = {
            "status": "ready" if path_result["returncode"] == 0 else "blocked",
            "repro_base_root": selection.get("repro_base_root"),
            "repro_root": repro_root,
            "writable": path_result["returncode"] == 0,
            "error": (
                path_result["stderr"] or path_result["stdout"]
                if path_result["returncode"] != 0
                else None
            ),
        }
    mismatches: list[str] = []
    if path_policy["status"] != "ready":
        mismatches.append("已确认 NAS 派生路径不可写")
    if role == "execution":
        expected = selection.get("execution_product") or {}
        expected_gpu_count = int(expected.get("gpu_count") or 0)
        actual_gpu_count = int(measured.get("gpu_count") or 0)
        if actual_gpu_count != expected_gpu_count:
            mismatches.append(
                f"实测 GPU 数量 {actual_gpu_count} 与确认规格 {expected_gpu_count} 不一致"
            )
        expected_vram = expected.get("vram_gb")
        actual_vram = min(
            (
                float(item.get("vram_gb"))
                for item in measured.get("gpus", [])
                if item.get("vram_gb") is not None
            ),
            default=None,
        )
        if expected_vram is not None and (
            actual_vram is None or actual_vram + 0.5 < float(expected_vram)
        ):
            mismatches.append(
                f"实测单卡显存 {actual_vram}GB 低于确认规格 {expected_vram}GB"
            )
        requirements = selection.get("plan_requirements") or {}
        minimum_count = requirements.get("minimum_gpu_count")
        minimum_vram = requirements.get("minimum_vram_gb")
        if minimum_count is not None and actual_gpu_count < int(minimum_count):
            mismatches.append(
                f"实测 GPU 数量低于计划最低要求 {minimum_count}"
            )
        if minimum_vram is not None and (
            actual_vram is None or actual_vram < float(minimum_vram)
        ):
            mismatches.append(
                f"实测单卡显存低于计划最低要求 {minimum_vram}GB"
            )
    measured.update(
        {
            "backend": "cci",
            "role": role,
            "status": (
                "resource_mismatch"
                if mismatches
                else "ready"
                if measured.get("ready")
                else "blocked"
            ),
            "ready": bool(measured.get("ready")) and not mismatches,
            "resource_evidence": "cci_instance_measured",
            "path_policy": path_policy,
            "resource_mismatches": mismatches,
            "step_7_allowed": role != "execution" or not mismatches,
        }
    )
    resource = state["resources"][role]
    resource["measured_probe"] = measured
    if mismatches:
        state["status"] = "execution_resource_confirmation_required"
        store.add_event(
            state,
            "execution_resource_mismatch",
            role=role,
            instance_id=resource.get("instance_id"),
            mismatches=mismatches,
        )
    else:
        store.add_event(
            state,
            "instance_probe_succeeded",
            role=role,
            instance_id=resource.get("instance_id"),
        )
    store.save(state)
    return measured


def sync_resource(
    store: CciStateStore,
    *,
    role: str,
    direction: str,
    remote_path: str,
    local_path: str,
    provider: CciCloudProvider | None = None,
) -> dict[str, Any]:
    state = store.load()
    selection = state.get("selection") or {}
    confirmed_root = PurePosixPath(str(selection.get("repro_root") or ""))
    requested_remote = PurePosixPath(str(remote_path or ""))
    if (
        not requested_remote.is_absolute()
        or ".." in requested_remote.parts
        or (
            requested_remote != confirmed_root
            and confirmed_root not in requested_remote.parents
        )
    ):
        raise CciStateError("同步远端路径必须位于用户确认的精确复现根内")
    transport = CciTransport.from_state(
        store.path,
        role=role,
        provider=provider,
    )
    result = (
        transport.pull(remote_path, local_path)
        if direction == "pull"
        else transport.push(local_path, remote_path)
    )
    exact_root_sync = requested_remote == confirmed_root
    if exact_root_sync and direction == "push":
        local_root = str(Path(local_path).expanduser().resolve())
        remap_result = transport.execute_script(
            _remote_remap_script(
                str(confirmed_root),
                old_root=local_root,
                new_root=str(confirmed_root),
            ),
            timeout=60,
        )
        if remap_result.get("returncode") != 0:
            raise CciTransportError(
                "CCI 根控制文件路径切换失败："
                + str(remap_result.get("stderr") or remap_result.get("stdout") or "")
            )
    elif exact_root_sync and direction == "pull":
        _remap_v3_control_files(
            local_path,
            old_root=str(confirmed_root),
            new_root=str(Path(local_path).expanduser().resolve()),
        )
    resource = state["resources"][role]
    record = {
        "direction": direction,
        "remote_path": remote_path,
        "local_path": str(Path(local_path).expanduser().resolve()),
        "completed_at": utc_now(),
        "file_count": len(result.get("files") or []),
    }
    resource.setdefault("sync_records", []).append(record)
    store.add_event(
        state,
        "sync_succeeded",
        role=role,
        direction=direction,
        remote_path=remote_path,
        local_path=record["local_path"],
        file_count=record["file_count"],
    )
    store.save(state)
    return result


def cloud_resource_evidence(store: CciStateStore) -> dict[str, Any]:
    state = store.load()
    selection = state.get("selection") or {}
    safe_selection = {
        key: selection.get(key)
        for key in (
            "repo_name",
            "aidc_id",
            "build_product",
            "execution_product",
            "image",
            "nas",
            "nas_mount_path",
            "repro_base_root",
            "repro_root",
            "local_staging_root",
            "plan_requirements",
        )
        if selection.get(key) is not None
    }
    return redact_mapping(
        {
            "provider": "cci",
            "state_path": str(store.path.resolve()),
            "session_id": state.get("session_id"),
            "run_id": state.get("run_id"),
            "status": state.get("status"),
            "selection": safe_selection,
            "resources": state.get("resources", {}),
            "cleanup_required": state.get("cleanup_required", []),
            "exported_at": utc_now(),
        }
    )


def _valid_selection(state: dict[str, Any]) -> dict[str, Any]:
    selection = state.get("selection")
    if not isinstance(selection, dict):
        raise CciStateError("没有用户明确确认的 CCI 资源选择")
    confirmation = selection.get("confirmation") or {}
    if confirmation.get("decision_source") != "explicit_user_reply":
        raise CciStateError("CCI 资源选择不是用户明确回复，禁止创建")
    expires_at = confirmation.get("expires_at")
    if not expires_at:
        raise CciStateError("CCI 资源确认缺少有效期，禁止创建")
    try:
        expires = datetime.fromisoformat(str(expires_at))
    except ValueError as exc:
        raise CciStateError("CCI 资源确认有效期格式错误") from exc
    if expires <= datetime.now(timezone.utc):
        raise CciStateError("CCI 资源确认已过期；请重新 preflight 并明确确认")
    return selection


def _find(items: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    matches = [item for item in items if str(item.get(key)) == value]
    if len(matches) != 1:
        raise CciStateError(f"选择项 {value!r} 不在最新预检结果中或不唯一")
    return dict(matches[0])


def _already_released(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("404", "not found", "does not exist", "already released")
    )


def _safe_instance_summary(detail: Any) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {"status": str(detail)[:100]}
    resource = detail.get("resource")
    resource = resource if isinstance(resource, dict) else {}
    return {
        key: value
        for key, value in {
            "instance_id": detail.get("instanceId") or detail.get("id"),
            "status": detail.get("status") or detail.get("state"),
            "sub_status": detail.get("subStatus"),
            "aidc_id": detail.get("aidcId"),
            "created_at": detail.get("createTime") or detail.get("createdAt"),
            "started_at": detail.get("startTime") or detail.get("startedAt"),
            "updated_at": detail.get("updateTime") or detail.get("updatedAt"),
            "released_at": detail.get("releaseTime") or detail.get("releasedAt"),
            "product_code": detail.get("productCode")
            or resource.get("productCode"),
            "cpu_cores": detail.get("cpuCores") or resource.get("cpuCores"),
            "memory_gb": detail.get("memoryGB") or resource.get("memoryGB"),
            "gpu_name": detail.get("gpuName") or resource.get("gpuName"),
            "gpu_count": detail.get("gpuCount") or resource.get("gpuCount"),
        }.items()
        if value not in (None, "")
    }


def _cost_evidence(resource: dict[str, Any]) -> dict[str, Any]:
    started = resource.get("created_at")
    ended = resource.get("released_at")
    evidence: dict[str, Any] = {
        "duration_hours": None,
        "actual_cost": None,
        "cost_calculation": "unavailable",
    }
    try:
        duration = (
            datetime.fromisoformat(str(ended))
            - datetime.fromisoformat(str(started))
        ).total_seconds() / 3600
        evidence["duration_hours"] = round(max(duration, 0.0), 6)
    except (TypeError, ValueError):
        return evidence
    unit = str(resource.get("price_unit") or "").strip().casefold()
    hourly = any(
        marker in unit
        for marker in ("hour", "hourly", "/h", "per h", "小时", "时")
    )
    if not hourly:
        evidence["cost_calculation"] = "price_unit_not_hourly"
        return evidence
    try:
        price = Decimal(str(resource.get("price")))
        duration_value = Decimal(str(evidence["duration_hours"]))
    except (InvalidOperation, TypeError, ValueError):
        evidence["cost_calculation"] = "invalid_price"
        return evidence
    evidence["actual_cost"] = format(price * duration_value, ".6f")
    evidence["cost_calculation"] = "price_times_duration_hours"
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AR24 CCI 控制面")
    parser.add_argument("--state", help="cci_state.json 或其所在目录")
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="只读查询规格、价格、镜像和已有 NAS")
    pre.add_argument("--session-id", required=True)
    pre.add_argument("--run-id")
    pre.add_argument("--aidc-id", type=int, default=5)

    confirm = sub.add_parser("confirm-selection", help="保存用户明确资源选择")
    confirm.add_argument("--repo-name", required=True)
    confirm.add_argument("--run-id")
    confirm.add_argument(
        "--base-root",
        required=True,
        help="用户明确确认的 NAS 内项目专用精确目录；不会追加 ar24 或项目名",
    )
    confirm.add_argument("--build-product", required=True)
    confirm.add_argument("--execution-product", required=True)
    confirm.add_argument("--image-key", required=True)
    confirm.add_argument("--nas-id", required=True)
    confirm.add_argument("--confirmation-id", required=True)
    confirm.add_argument("--user-response", required=True)
    confirm.add_argument(
        "--ttl-seconds",
        type=int,
        default=int(
            os.environ.get(
                "AR24_CCI_CONFIRMATION_TTL_SECONDS",
                DEFAULT_CONFIRMATION_TTL_SECONDS,
            )
        ),
    )

    measured = sub.add_parser(
        "confirm-measured",
        help="明确确认 GPU 实测资源差异并重新绑定计划最低要求",
    )
    measured.add_argument("--confirmation-id", required=True)
    measured.add_argument("--user-response", required=True)
    measured.add_argument("--minimum-gpu-count", type=int)
    measured.add_argument("--minimum-vram-gb", type=float)

    create = sub.add_parser("create", help="创建已确认的构建、执行或恢复实例")
    create.add_argument("--role", choices=("build", "execution", "recovery"), required=True)
    create.add_argument("--run-id")
    create.add_argument("--minimum-gpu-count", type=int)
    create.add_argument("--minimum-vram-gb", type=float)

    probe = sub.add_parser("probe", help="通过临时 SSH 实测实例资源")
    probe.add_argument("--role", choices=("build", "execution", "recovery"), required=True)

    execute = sub.add_parser("exec", help="通过 Paramiko 在 CCI 实例运行脚本")
    execute.add_argument("--role", choices=("build", "execution", "recovery"), required=True)
    execute.add_argument("--script-file")
    execute.add_argument("--timeout", type=float)

    sync = sub.add_parser("sync", help="通过 SFTP 同步文件或目录")
    sync.add_argument("--role", choices=("build", "execution", "recovery"), required=True)
    sync.add_argument("--direction", choices=("pull", "push"), required=True)
    sync.add_argument("--remote-path", required=True)
    sync.add_argument("--local-path", required=True)

    sub.add_parser("status", help="查询当前 run 的 CCI 资源")
    sub.add_parser("evidence", help="输出可写入执行台账的脱敏云资源证据")
    release = sub.add_parser("release", help="幂等释放单个 CCI 资源")
    release.add_argument("--role", choices=("build", "execution", "recovery"), required=True)
    sub.add_parser("cleanup", help="恢复并释放 cleanup_required/活跃资源")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            store = (
                CciStateStore(args.state)
                if args.state
                else CciStateStore.for_session(args.session_id)
            )
            result = preflight(
                store,
                session_id=args.session_id,
                run_id=args.run_id,
                aidc_id=args.aidc_id,
            )
        else:
            if not args.state:
                parser.error("--state 对此命令是必需的")
            store = CciStateStore(args.state)
            if args.command == "confirm-selection":
                result = confirm_selection(
                    store,
                    repo_name=args.repo_name,
                    run_id=args.run_id,
                    exact_base_root=args.base_root,
                    build_product_code=args.build_product,
                    execution_product_code=args.execution_product,
                    image_key=args.image_key,
                    nas_id=args.nas_id,
                    confirmation_id=args.confirmation_id,
                    user_response=args.user_response,
                    ttl_seconds=args.ttl_seconds,
                )
            elif args.command == "confirm-measured":
                result = confirm_measured_resources(
                    store,
                    confirmation_id=args.confirmation_id,
                    user_response=args.user_response,
                    minimum_gpu_count=args.minimum_gpu_count,
                    minimum_vram_gb=args.minimum_vram_gb,
                )
            elif args.command == "create":
                result = create_resource(
                    store,
                    role=args.role,
                    plan_requirements={
                        "minimum_gpu_count": args.minimum_gpu_count,
                        "minimum_vram_gb": args.minimum_vram_gb,
                    },
                    run_id=args.run_id,
                )
            elif args.command == "probe":
                result = probe_resource(store, role=args.role)
            elif args.command == "exec":
                if args.role == "execution":
                    ensure_execution_ready(store)
                script = (
                    Path(args.script_file).read_text(encoding="utf-8")
                    if args.script_file
                    else sys.stdin.read()
                )
                if not script.strip():
                    raise CciStateError("exec 需要通过 stdin 或 --script-file 提供脚本")
                result = CciTransport.from_state(
                    store.path, role=args.role
                ).execute_script(script, timeout=args.timeout)
            elif args.command == "sync":
                result = sync_resource(
                    store,
                    role=args.role,
                    direction=args.direction,
                    remote_path=args.remote_path,
                    local_path=args.local_path,
                )
            elif args.command == "status":
                result = status_resources(store)
            elif args.command == "evidence":
                result = cloud_resource_evidence(store)
            elif args.command == "release":
                result = release_resource(store, role=args.role)
            else:
                result = cleanup_resources(store)
        print(json.dumps(redact_mapping(result), ensure_ascii=False, indent=2))
        if result.get("status") in {"cleanup_required", "error"}:
            raise SystemExit(2)
    except SystemExit:
        raise
    except (MissingCciCredentials, MissingCciDependency, CciTransportError) as exc:
        print(
            json.dumps(
                {
                    "status": "paused",
                    "pause_reason": (
                        "missing_cci_credentials"
                        if isinstance(exc, MissingCciCredentials)
                        else "missing_cci_dependency_or_transport"
                    ),
                    "error": redact_text(exc),
                    "next_action": (
                        "从安全环境注入 CCI 凭证，或安装 requirements-cci.txt；"
                        "CCI 分支不会回退到 local/ssh"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(3) from exc
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "error": redact_text(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
