from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from .redaction import redact_mapping, redact_text


@dataclass(frozen=True)
class CciResponse:
    code: str
    message: str
    data: Any
    raw: dict[str, Any]


class CciApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int | None,
        details: Any | None = None,
    ) -> None:
        self.code = str(code)
        self.status_code = status_code
        self.details = redact_mapping(details)
        safe_message = redact_text(message, secrets=_collect_string_values(details))
        super().__init__(
            f"CCI API 调用失败 code={self.code} status={self.status_code}: "
            f"{safe_message}; 详情={self.details}"
        )


class MissingCciCredentials(RuntimeError):
    pass


class MissingCciDependency(RuntimeError):
    pass


class AlayaNewCciClient:
    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        access_token: str | None = None,
        base_url: str = "https://api.alayanew.com",
        timeout: float = 30.0,
        retries: int = 2,
        timestamp_fn: Callable[[], str | int] | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.timestamp_fn = timestamp_fn or (lambda: int(time.time() * 1000))
        if http_client is None:
            try:
                import requests
            except ImportError as exc:
                raise MissingCciDependency(
                    "CCI 分支缺少 requests；请安装 ar24-instance-manage/requirements-cci.txt"
                ) from exc
            http_client = requests.Session()
        self.http_client = http_client

    @classmethod
    def from_env(cls, **kwargs: Any) -> "AlayaNewCciClient":
        access_key = os.environ.get("ALAYANEW_ACCESS_KEY", "")
        secret_key = os.environ.get("ALAYANEW_SECRET_KEY", "")
        if not access_key or not secret_key:
            raise MissingCciCredentials(
                "CCI 凭证缺失：请在环境变量中设置 ALAYANEW_ACCESS_KEY 和 "
                "ALAYANEW_SECRET_KEY；不要在命令参数或对话中提供凭证"
            )
        return cls(
            access_key=access_key,
            secret_key=secret_key,
            access_token=os.environ.get("ALAYANEW_ACCESS_TOKEN") or None,
            base_url=os.environ.get("ALAYANEW_BASE_URL", "https://api.alayanew.com"),
            **kwargs,
        )

    def sign(self, method: str, uri: str, timestamp: str | int | None = None) -> str:
        ts = str(timestamp if timestamp is not None else self._timestamp())
        path = normalize_uri_path(uri)
        string_to_sign = f"{ts}|{method.upper()}|{path}"
        return hmac.new(
            self.secret_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def build_headers(self, method: str, uri: str) -> dict[str, str]:
        timestamp = self._timestamp()
        signature = self.sign(method, uri, timestamp=timestamp)
        headers = {
            "Authorization": (
                f"alayanew-HMAC-SHA256 {self.access_key}:{timestamp}:{signature}"
            ),
            "Content-Type": "application/json",
        }
        if self.access_token:
            headers["X-Access-Token"] = self.access_token
        return headers

    def get(self, uri: str, **kwargs: Any) -> CciResponse:
        return self.request("GET", uri, **kwargs)

    def post(self, uri: str, **kwargs: Any) -> CciResponse:
        return self.request("POST", uri, **kwargs)

    def request(self, method: str, uri: str, **kwargs: Any) -> CciResponse:
        method = method.upper()
        path = normalize_uri_path(uri)
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.http_client.request(
                    method=method,
                    url=url,
                    headers=self.build_headers(method, path),
                    timeout=self.timeout,
                    **kwargs,
                )
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    continue
                raise CciApiError(
                    str(exc),
                    code="transport_error",
                    status_code=None,
                    details={"error": str(exc)},
                ) from exc
            if (
                response.status_code in {408, 429}
                or response.status_code >= 500
            ) and attempt < self.retries:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                continue
            return self._normalize_response(response)
        raise CciApiError(
            str(last_error),
            code="transport_error",
            status_code=None,
            details={"error": str(last_error)},
        )

    def _normalize_response(self, response: Any) -> CciResponse:
        try:
            payload = response.json()
        except Exception as exc:
            raise CciApiError(
                getattr(response, "text", str(exc)),
                code="invalid_json",
                status_code=getattr(response, "status_code", None),
                details={"body": getattr(response, "text", "")},
            ) from exc
        if not isinstance(payload, dict):
            payload = {"code": 0, "data": payload}
        code = str(
            payload["code"]
            if "code" in payload
            else payload.get("status", 0)
        )
        message = str(payload.get("message") or payload.get("msg") or "")
        data = payload.get("data")
        if response.status_code >= 400 or code not in {"0", "200"}:
            raise CciApiError(
                message or getattr(response, "text", ""),
                code=code,
                status_code=response.status_code,
                details=data if data is not None else payload,
            )
        return CciResponse(code=code, message=message, data=data, raw=payload)

    def _timestamp(self) -> str:
        return str(self.timestamp_fn())


def normalize_uri_path(uri: str) -> str:
    parsed = urlsplit(uri)
    path = parsed.path if parsed.scheme or parsed.netloc else uri.split("?", 1)[0]
    path = path or "/"
    return path if path.startswith("/") else f"/{path}"


def _collect_string_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [
            item
            for nested in value.values()
            for item in _collect_string_values(nested)
        ]
    if isinstance(value, list):
        return [item for nested in value for item in _collect_string_values(nested)]
    return [value] if isinstance(value, str) else []
