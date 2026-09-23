import os
import re
import shlex

from retry_policy import TSINGHUA_PIP_INDEX, TSINGHUA_TRUSTED_HOST


SHELL_META_PATTERN = re.compile(r"[;&|`$<>]")
SAFE_COMMAND_PREFIXES = (
    ("pip", "install"),
    ("pip3", "install"),
    ("python", "-m", "pip", "install"),
    ("python3", "-m", "pip", "install"),
    ("uv", "pip", "install"),
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

CONDA_INSTALL_PREFIXES = (
    ("conda", "install"),
    ("mamba", "install"),
    ("micromamba", "install"),
)

BUILD_OR_EDITABLE_PATTERNS = (
    ("pip", "install", "-e"),
    ("pip3", "install", "-e"),
    ("python", "-m", "pip", "install", "-e"),
    ("python3", "-m", "pip", "install", "-e"),
    ("uv", "pip", "install", "-e"),
)

BUILD_COMMAND_PREFIXES = (
    ("python", "setup.py"),
    ("python3", "setup.py"),
    ("cmake",),
    ("make",),
    ("ninja",),
)
COMPILED_PACKAGE_HINTS = {
    "apex",
    "flash-attn",
    "kaolin",
    "nvdiffrast",
    "pytorch3d",
    "spconv",
}


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


def _starts_with(parts, prefix) -> bool:
    return tuple(parts[: len(prefix)]) == prefix


def _is_conda_install(parts) -> bool:
    return any(_starts_with(parts, prefix) for prefix in CONDA_INSTALL_PREFIXES)


def _is_build_or_editable_command(parts) -> bool:
    if any(_starts_with(parts, prefix) for prefix in BUILD_OR_EDITABLE_PATTERNS + BUILD_COMMAND_PREFIXES):
        return True
    pip_args = _pip_install_args(parts)
    if pip_args is None:
        return False
    for arg in pip_args:
        lowered = arg.lower()
        if lowered in {"-e", "--editable"} or lowered.startswith("--editable="):
            return True
        if lowered.startswith("git+"):
            return True
        package = re.split(r"[@=<>~!;\[]", lowered, maxsplit=1)[0]
        if package in COMPILED_PACKAGE_HINTS:
            return True
    return False


def validate_dependency_command_list(cmds, label, env_backend_info: dict | None = None):
    validate_command_list(cmds, label)
    backend = (env_backend_info or {}).get("backend")
    for cmd in cmds:
        parts = shlex.split(cmd)
        if _is_build_or_editable_command(parts):
            raise ValueError(
                f"{label} contains a build or editable install command for step_7: {cmd!r}"
            )
        if backend == "uv":
            if not is_pip_install(parts):
                raise ValueError(
                    f"{label} contains a non-pip installer under uv backend: {cmd!r}. "
                    "The uv backend only installs dependencies with uv pip inside the step_4 environment."
                )
            if _is_conda_install(parts):
                raise ValueError(
                    f"{label} contains a conda-style installer under uv backend: {cmd!r}"
                )
        elif not (is_pip_install(parts) or _is_conda_install(parts)):
            raise ValueError(
                f"{label} contains a non-install command: {cmd!r}. "
                "Use dependency_cmds only for package installation; move downloads to step_5 or step_6."
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


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _quote_executable(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _drop_uv_python_target(args: list[str]) -> list[str]:
    cleaned = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--python":
            skip_next = True
            continue
        if arg.startswith("--python="):
            continue
        cleaned.append(arg)
    return cleaned


def _pip_install_args(parts: list[str]) -> list[str] | None:
    if len(parts) >= 2 and parts[:2] in (["pip", "install"], ["pip3", "install"]):
        return parts[2:]
    if len(parts) >= 4 and parts[:4] in (
        ["python", "-m", "pip", "install"],
        ["python3", "-m", "pip", "install"],
    ):
        return parts[4:]
    if len(parts) >= 3 and os.path.basename(parts[0]) == "uv" and parts[1:3] == ["pip", "install"]:
        return _drop_uv_python_target(parts[3:])
    return None


def normalize_python_cmd(cmd: str, env_backend_info: dict | None = None, env_path: str | None = None) -> str:
    stripped = cmd.lstrip()
    leading = cmd[: len(cmd) - len(stripped)]
    try:
        parts = shlex.split(stripped)
    except ValueError:
        return cmd

    if env_backend_info and env_backend_info.get("backend") == "uv" and env_path:
        pip_args = _pip_install_args(parts)
        if pip_args is not None:
            uv_bin = env_backend_info.get("uv", {}).get("path") or "uv"
            args = " ".join(shlex.quote(arg) for arg in pip_args)
            return (
                f"{leading}{_quote_executable(uv_bin)} pip install "
                f"--python {_shell_quote(f'{env_path}/bin/python')} {args}"
            ).rstrip()

    if len(parts) >= 1 and parts[0] == "pip":
        return f"{leading}python -m pip {' '.join(shlex.quote(arg) for arg in parts[1:])}"
    if len(parts) >= 1 and parts[0] == "pip3":
        return f"{leading}python -m pip {' '.join(shlex.quote(arg) for arg in parts[1:])}"
    return cmd
