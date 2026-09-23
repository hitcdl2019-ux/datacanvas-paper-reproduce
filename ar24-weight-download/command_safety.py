import os
import re
import shlex

from retry_policy import TSINGHUA_PIP_INDEX, TSINGHUA_TRUSTED_HOST


SHELL_META_PATTERN = re.compile(r"[;&|`$<>]")
SAFE_COMMAND_PREFIXES = (
    ("pip", "install"),
    ("python", "-m", "pip", "install"),
    ("python3", "-m", "pip", "install"),
    ("conda", "install"),
    ("mamba", "install"),
    ("micromamba", "install"),
    ("gdown",),
    ("wget",),
    ("curl",),
    ("tar",),
    ("unzip",),
    ("python", "-c"),
    ("python3", "-c"),
    ("hf", "download"),
    ("huggingface-cli", "download"),
)


def is_safe_command(cmd: str) -> bool:
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if not parts or parts[0] in {"export", "cd", "source"}:
        return False
    if SHELL_META_PATTERN.search(cmd):
        if len(parts) == 3 and tuple(parts[:2]) in {("python", "-c"), ("python3", "-c")}:
            return True
        return False
    return any(tuple(parts[: len(prefix)]) == prefix for prefix in SAFE_COMMAND_PREFIXES)


def validate_command_list(cmds, label):
    for cmd in cmds:
        if not is_safe_command(cmd):
            raise ValueError(
                f"{label} contains an unsafe or unsupported command: {cmd!r}. "
                "Use one auditable pip/conda/gdown/wget/curl/hf/python -c command."
            )


def is_pip_install(parts):
    if len(parts) >= 2 and parts[:2] == ["pip", "install"]:
        return True
    if len(parts) >= 4 and parts[:4] in (
        ["python", "-m", "pip", "install"],
        ["python3", "-m", "pip", "install"],
    ):
        return True
    return len(parts) >= 3 and os.path.basename(parts[0]) == "uv" and parts[1:3] == ["pip", "install"]


def patch_pip_command_for_tsinghua(cmd: str) -> str:
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return cmd
    if not is_pip_install(parts):
        return cmd
    index_flags = {"-i", "--index-url", "--extra-index-url", "--find-links", "-f"}
    if any(
        p in index_flags or p.startswith("--index-url=") or p.startswith("--extra-index-url=")
        for p in parts
    ):
        return cmd
    return f"{cmd} -i {TSINGHUA_PIP_INDEX} --trusted-host {TSINGHUA_TRUSTED_HOST}"


def normalize_python_cmd(cmd: str, env_backend_info: dict | None = None, env_path: str | None = None) -> str:
    stripped = cmd.lstrip()
    leading = cmd[: len(cmd) - len(stripped)]
    if env_backend_info and env_backend_info.get("backend") == "uv" and env_path:
        uv_bin = env_backend_info.get("uv", {}).get("path") or "uv"
        if stripped.startswith("pip install "):
            return f"{leading}\"{uv_bin}\" pip install --python '{env_path}/bin/python' {stripped[12:]}"
        if stripped.startswith("pip3 install "):
            return f"{leading}\"{uv_bin}\" pip install --python '{env_path}/bin/python' {stripped[13:]}"
    if stripped.startswith("pip "):
        return f"{leading}python -m pip {stripped[4:]}"
    if stripped.startswith("pip3 "):
        return f"{leading}python -m pip {stripped[5:]}"
    return cmd
