from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


DEFAULT_AIDC_ID = 5


def _pick(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if raw.get(key) is not None:
            return raw[key]
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return _as_int(value)


def _as_price(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError):
        return str(value)


@dataclass(frozen=True)
class CciProduct:
    product_code: str
    aidc_id: int = DEFAULT_AIDC_ID
    cpu_cores: int = 0
    memory_gb: int = 0
    gpu_name: str | None = None
    gpu_count: int = 0
    vram_gb: int | None = None
    price: str | None = None
    price_unit: str | None = None
    remaining_count: int | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "CciProduct":
        remaining = _pick(
            raw,
            "remainingCount",
            "remaining_count",
            "stock",
            "inventory",
        )
        price_unit = _pick(raw, "priceUnit", "price_unit", "billingUnit")
        return cls(
            product_code=str(_pick(raw, "productCode", "product_code", "code", default="")),
            aidc_id=_as_int(_pick(raw, "aidcId", "aidc_id", default=DEFAULT_AIDC_ID), DEFAULT_AIDC_ID),
            cpu_cores=_as_int(_pick(raw, "cpuCores", "cpu_cores", "cpu", default=0)),
            memory_gb=_as_int(
                _pick(raw, "memoryGb", "memoryGB", "memory_gb", "memory", default=0)
            ),
            gpu_name=_pick(raw, "gpuName", "gpu_name", "gpuType", "gpu_type"),
            gpu_count=_as_int(_pick(raw, "gpuCount", "gpu_count", default=0)),
            vram_gb=_as_optional_int(_pick(raw, "vramGb", "vramGB", "vram_gb")),
            price=_as_price(_pick(raw, "price", "originalPrice", "unitPrice")),
            price_unit=str(price_unit) if price_unit not in (None, "") else None,
            remaining_count=_as_optional_int(remaining),
        )

    @property
    def available(self) -> bool:
        return self.remaining_count is None or self.remaining_count > 0

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "product_code": self.product_code,
            "aidc_id": self.aidc_id,
            "cpu_cores": self.cpu_cores,
            "memory_gb": self.memory_gb,
            "gpu_name": self.gpu_name,
            "gpu_count": self.gpu_count,
            "vram_gb": self.vram_gb,
            "price": self.price,
            "price_unit": self.price_unit,
            "remaining_count": self.remaining_count,
            "available": self.available,
        }


@dataclass(frozen=True)
class CciImage:
    repository_name: str
    image_url: str
    tag: str
    aidc_id: int = DEFAULT_AIDC_ID

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "CciImage":
        return cls(
            repository_name=str(
                _pick(raw, "repositoryName", "repository_name", "name", default="")
            ),
            image_url=str(_pick(raw, "imageUrl", "image_url", "url", default="")),
            tag=str(
                _pick(raw, "tag", "tagName", "imageTag", "image_tag", default="latest")
            ),
            aidc_id=_as_int(_pick(raw, "aidcId", "aidc_id", default=DEFAULT_AIDC_ID), DEFAULT_AIDC_ID),
        )

    @property
    def selection_key(self) -> str:
        return f"{self.repository_name}:{self.tag}|{self.image_url}"

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "repository_name": self.repository_name,
            "image_url": self.image_url,
            "tag": self.tag,
            "aidc_id": self.aidc_id,
            "selection_key": self.selection_key,
        }


@dataclass(frozen=True)
class NasStorage:
    storage_id: str
    mount_path: str
    name: str | None = None
    aidc_id: int = DEFAULT_AIDC_ID
    status: str | None = None
    storage_type: str = "nas-capacity"
    access_policy: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "NasStorage":
        shared_policy = raw.get("sharedPolicy")
        shared_policy = shared_policy if isinstance(shared_policy, dict) else {}
        return cls(
            storage_id=str(_pick(raw, "storageId", "storage_id", "id", default="")),
            mount_path=str(_pick(raw, "mountPath", "mount_path", default="")),
            name=_pick(raw, "name", "storageName", "storage_name"),
            aidc_id=_as_int(_pick(raw, "aidcId", "aidc_id", default=DEFAULT_AIDC_ID), DEFAULT_AIDC_ID),
            status=_pick(raw, "status", "state"),
            storage_type=str(
                _pick(raw, "storageType", "storage_type", default="nas-capacity")
            ),
            access_policy=_pick(
                raw,
                "policy",
                "accessPolicy",
                "access_policy",
                default=shared_policy.get("policy"),
            ),
        )

    @property
    def writable(self) -> bool:
        status = str(self.status or "").strip().casefold()
        if status in {
            "released",
            "deleted",
            "failed",
            "error",
            "disabled",
            "releasing",
        }:
            return False
        policy = str(self.access_policy or "").strip().casefold()
        return policy not in {"ro", "read_only", "readonly", "read-only"}

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "storage_id": self.storage_id,
            "mount_path": self.mount_path,
            "name": self.name,
            "aidc_id": self.aidc_id,
            "status": self.status,
            "storage_type": self.storage_type,
            "access_policy": self.access_policy,
            "writable": self.writable,
        }


@dataclass(frozen=True)
class CciResourceSpec:
    run_id: str
    role: str
    product: CciProduct
    image: CciImage
    nas: NasStorage
    aidc_id: int = DEFAULT_AIDC_ID
    auto_stop_enable: bool = False
    auto_release_enable: bool = False
    storage_config_enable: bool = True
