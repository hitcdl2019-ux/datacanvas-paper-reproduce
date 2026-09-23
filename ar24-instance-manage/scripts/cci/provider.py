from __future__ import annotations

import hashlib
import re
import time
from dataclasses import replace
from typing import Any, Callable

from .models import (
    DEFAULT_AIDC_ID,
    CciImage,
    CciProduct,
    CciResourceSpec,
    NasStorage,
)


class CciProvisioningError(RuntimeError):
    def __init__(self, result_key: str, result: dict[str, Any]) -> None:
        self.result_key = result_key
        self.result = result
        try:
            self.instance_id = extract_instance_id(result)
        except ValueError:
            self.instance_id = None
        super().__init__(f"CCI 创建结果 {result_key} 失败")


class CciCloudProvider:
    PRODUCT_LIST_PATH = "/api/osm/v1/product/list-for-cci"
    IMAGE_LIST_PATH = "/api/osm/v1/cci/public-image/list/page"
    NAS_TENANT_PAGE_PATH = "/api/osm/v1/nasStorage/instance/tenant/page"
    CCI_CREATE_PATH = "/api/osm/v1/subscription/CCI/create"

    def __init__(
        self,
        client: Any,
        *,
        poll_interval: float = 2.0,
        max_polls: int = 90,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self.sleeper = sleeper

    def list_products(self, aidc_id: int | None = None) -> list[CciProduct]:
        kwargs = {"params": {"aidcId": aidc_id}} if aidc_id is not None else {}
        return [
            CciProduct.from_api(item)
            for item in _items(self.client.get(self.PRODUCT_LIST_PATH, **kwargs).data)
        ]

    def list_public_images(
        self,
        page: int = 1,
        page_size: int = 100,
        aidc_id: int = DEFAULT_AIDC_ID,
    ) -> list[CciImage]:
        data = self.client.post(
            self.IMAGE_LIST_PATH,
            json={"pageNo": page, "pageSize": page_size, "aidcId": aidc_id},
        ).data
        images = []
        for item in _image_items(data):
            image = CciImage.from_api(item)
            if item.get("aidcId") is None and item.get("aidc_id") is None:
                image = replace(image, aidc_id=aidc_id)
            images.append(image)
        return images

    def list_nas(self, page: int = 1, page_size: int = 100) -> list[NasStorage]:
        data = self.client.get(
            self.NAS_TENANT_PAGE_PATH,
            params={"page": page, "pageSize": page_size},
        ).data
        return [NasStorage.from_api(item) for item in _items(data)]

    @staticmethod
    def build_cci_create_payload(spec: CciResourceSpec) -> dict[str, Any]:
        return {
            "productCode": spec.product.product_code,
            "cciParams": {
                "name": _cci_instance_name(spec),
                "image": spec.image.image_url,
                # AR24 owns cleanup. The provider must not silently stop or release
                # a billable instance while the report confirmation gate is open.
                "autoStopEnable": False,
                "autoReleaseEnable": False,
                "storageConfigEnable": True,
                "storageConfigs": [
                    {
                        "storageId": spec.nas.storage_id,
                        "storageType": spec.nas.storage_type,
                        "fileDirectory": spec.nas.name or "",
                        "mountPath": spec.nas.mount_path,
                        "onlyRead": False,
                    }
                ],
            },
        }

    def create_cci_instance(self, spec: CciResourceSpec) -> str:
        data = self.client.post(
            self.CCI_CREATE_PATH,
            json=self.build_cci_create_payload(spec),
        ).data
        return _result_key(data)

    def poll_cci_result(self, result_key: str) -> dict[str, Any]:
        path = f"/api/osm/v1/subscription/CCI/result/{result_key}"
        for attempt in range(self.max_polls):
            data = self.client.get(path).data
            if not isinstance(data, dict):
                data = {"result": data}
            status = str(
                data.get("status") or data.get("state") or data.get("result") or ""
            ).lower()
            if status in {
                "success",
                "succeeded",
                "completed",
                "available",
                "running",
            }:
                return data
            if status in {"failed", "error", "cancelled", "canceled"}:
                raise CciProvisioningError(result_key, data)
            if attempt < self.max_polls - 1:
                self.sleeper(self.poll_interval)
        raise TimeoutError(
            f"CCI 创建结果 {result_key} 在 {self.max_polls} 次轮询后仍未完成"
        )

    def instance_detail(self, instance_id: str) -> dict[str, Any]:
        data = self.client.get(f"/api/osm/v1/cci/instance/{instance_id}").data
        return data if isinstance(data, dict) else {"data": data}

    def instance_events(self, instance_id: str) -> dict[str, Any]:
        data = self.client.get(
            f"/api/osm/v1/cci/instance/{instance_id}/get-event"
        ).data
        return data if isinstance(data, dict) else {"data": data}

    def instance_error_details(self, instance_id: str) -> dict[str, Any]:
        data = self.client.get(
            f"/api/osm/v1/cci/instance/{instance_id}/error-details"
        ).data
        return data if isinstance(data, dict) else {"data": data}

    def release_instance(self, instance_id: str) -> dict[str, Any]:
        data = self.client.post(
            f"/api/osm/v1/cci/instance/{instance_id}/release"
        ).data
        return data if isinstance(data, dict) else {"data": data}

    def get_ssh_connection(self, instance_id: str) -> "SshConnectionInfo":
        return parse_ssh_connection(
            self.client.post(
                f"/api/osm/v1/cci/instance/{instance_id}/get-ssh"
            ).data
        )


class SshConnectionInfo:
    def __init__(
        self,
        host: str,
        *,
        port: int = 22,
        user: str = "root",
        password: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    def to_safe_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "user": self.user}


def parse_ssh_connection(data: Any) -> SshConnectionInfo:
    import shlex
    from urllib.parse import urlparse

    if not isinstance(data, dict):
        raise ValueError("CCI SSH 响应格式无效")
    payload = dict(data)
    for key in ("data", "result", "ssh", "connection"):
        if isinstance(payload.get(key), dict):
            payload = dict(payload[key])

    password = _pick(
        payload,
        "sshPwd",
        "sshPassword",
        "ssh_password",
        "password",
        "passwd",
    )
    host = _pick(
        payload,
        "sshHost",
        "ssh_host",
        "host",
        "ip",
        "publicIp",
        "public_ip",
        "externalIp",
        "external_ip",
    )
    if host:
        return SshConnectionInfo(
            str(host),
            port=_safe_int(_pick(payload, "sshPort", "ssh_port", "port"), 22),
            user=str(
                _pick(payload, "sshUser", "ssh_user", "user", "username") or "root"
            ),
            password=str(password) if password else None,
        )

    url_value = _pick(payload, "sshUrl", "ssh_url", "url", "connectionUrl")
    if url_value:
        parsed = urlparse(str(url_value))
        if parsed.hostname:
            return SshConnectionInfo(
                parsed.hostname,
                port=parsed.port or 22,
                user=parsed.username or "root",
                password=str(password) if password else None,
            )
        match = re.fullmatch(r"(?P<host>[^:]+):(?P<port>\d+)", str(url_value))
        if match:
            return SshConnectionInfo(
                match.group("host"),
                port=int(match.group("port")),
                password=str(password) if password else None,
            )

    command = _pick(payload, "sshCommand", "ssh_command", "command", "cmd")
    if command:
        try:
            parts = shlex.split(str(command))
        except ValueError:
            parts = str(command).split()
        target: str | None = None
        port = 22
        for index, part in enumerate(parts):
            if part in {"-p", "-P"} and index + 1 < len(parts):
                port = _safe_int(parts[index + 1], 22)
            elif part.startswith("-p") and part[2:].isdigit():
                port = int(part[2:])
            elif "@" in part and not part.startswith("-"):
                target = part
            elif target is None and index > 0 and not part.startswith("-"):
                target = part
        if target:
            user, host = (
                target.rsplit("@", 1) if "@" in target else ("root", target)
            )
            return SshConnectionInfo(
                host,
                port=port,
                user=user or "root",
                password=str(password) if password else None,
            )
    raise ValueError("CCI SSH 响应中缺少可识别的主机信息")


def extract_instance_id(result: dict[str, Any]) -> str:
    for key in ("instanceId", "instance_id", "id", "resourceId", "resource_id"):
        if result.get(key):
            return str(result[key])
    for key in ("data", "result", "instance"):
        nested = result.get(key)
        if isinstance(nested, dict):
            try:
                return extract_instance_id(nested)
            except ValueError:
                pass
    raise ValueError(f"CCI 创建成功响应缺少实例 ID: {result}")


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("list", "records", "rows", "items", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _image_items(data: Any) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for item in _items(data):
        tags = item.get("tagsList")
        if not isinstance(tags, list) or not tags:
            images.append(item)
            continue
        for tag in tags:
            if isinstance(tag, dict):
                images.append(
                    {
                        **item,
                        **tag,
                        "imageUrl": tag.get("imageUrl") or item.get("imageUrl"),
                        "tag": tag.get("tagName")
                        or tag.get("tag")
                        or item.get("tag"),
                    }
                )
    return images


def _cci_instance_name(spec: CciResourceSpec, max_length: int = 20) -> str:
    full_name = f"{spec.run_id}-{spec.role}"
    if len(full_name) <= max_length:
        return full_name
    prefix = re.sub(r"[^a-zA-Z0-9-]+", "-", spec.run_id).strip("-")
    prefix = (prefix.split("-")[0] or "repro")[:7].lower()
    digest = hashlib.sha1(spec.run_id.encode("utf-8")).hexdigest()[:6]
    role = {"build": "bld", "execution": "exe", "recovery": "rcv"}.get(
        spec.role, spec.role[:3]
    )
    return f"{prefix}-{digest}-{role}"[:max_length]


def _result_key(data: Any) -> str:
    if isinstance(data, str) and data:
        return data
    if isinstance(data, dict):
        for key in ("resultKey", "result-key", "result_key", "key"):
            if data.get(key):
                return str(data[key])
    raise ValueError(f"CCI 创建响应缺少 result key: {data}")


def _pick(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload[key]
    return None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
