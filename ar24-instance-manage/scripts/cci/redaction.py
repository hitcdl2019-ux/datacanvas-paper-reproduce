from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any


REDACTION_TOKEN = "[REDACTED]"

_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|password|passwd|ssh[_-]?(?:pwd|password)|token|secret|"
    r"(?:secret|access|api|private)[_-]?key|credential|cookie)",
    re.IGNORECASE,
)
_SECRET_LIKE_VALUE_RE = re.compile(
    r"^(?:ak_|sk_|tok_|token_|bearer\s+|alayanew-HMAC-SHA256\s+)",
    re.IGNORECASE,
)
_SECRET_LIKE_SUBSTRING_RE = re.compile(
    r"\b(?:ak|sk|tok|token|ssh_password)_[A-Za-z0-9_:-]+\b",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?im)\b(authorization|password|passwd|token|secretKey|secret_key|"
    r"access_key|ALAYANEW_ACCESS_KEY|ALAYANEW_SECRET_KEY|"
    r"ALAYANEW_ACCESS_TOKEN)\b(\s*[:=]\s*)([^\r\n\s,;]+)"
)


def is_sensitive_key(key: Any) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(key)))


def is_secret_like_value(value: Any) -> bool:
    return isinstance(value, str) and bool(_SECRET_LIKE_VALUE_RE.search(value.strip()))


def redact_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (
                _redact_sensitive_value(item)
                if is_sensitive_key(key)
                else redact_mapping(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, str):
        return REDACTION_TOKEN if is_secret_like_value(value) else redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact_mapping(item) for item in value]
    return value


def redact_text(text: Any, secrets: Sequence[str] | None = None) -> str:
    redacted = str(text)
    runtime_secrets = [
        os.environ.get(name, "")
        for name in (
            "ALAYANEW_ACCESS_KEY",
            "ALAYANEW_SECRET_KEY",
            "ALAYANEW_ACCESS_TOKEN",
        )
    ]
    for secret in [*(secrets or ()), *runtime_secrets]:
        if secret:
            redacted = redacted.replace(secret, REDACTION_TOKEN)
    redacted = _SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTION_TOKEN}",
        redacted,
    )
    return _SECRET_LIKE_SUBSTRING_RE.sub(REDACTION_TOKEN, redacted)


def assert_no_sensitive_values(
    value: Any,
    secrets: Sequence[str] | None = None,
) -> None:
    text = str(value)
    if redact_text(text, secrets=secrets) != text:
        raise ValueError("CCI 状态中检测到敏感凭证")


def _redact_sensitive_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _redact_sensitive_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_sensitive_value(item) for item in value]
    return REDACTION_TOKEN
