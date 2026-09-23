import os
import shutil


SYSTEM_DEV_PACKAGES = (
    "libopengl-dev libegl1-mesa-dev libglu1-mesa-dev libxt-dev "
    "libxext-dev libxrender-dev libxrandr-dev libxi-dev "
    "libxcursor-dev libxinerama-dev libxcomposite-dev libxdamage-dev "
    "libxfixes-dev libxss-dev libxft-dev libxmu-dev "
    "zip autoconf autoconf-archive pkg-config"
)


def select_env_backend():
    uv_bin = shutil.which("uv")
    conda_bin = shutil.which("conda")
    if not conda_bin:
        for candidate in (
            "/opt/conda/bin/conda",
            os.path.expanduser("~/miniconda3/bin/conda"),
            os.path.expanduser("~/anaconda3/bin/conda"),
        ):
            if os.path.exists(candidate):
                conda_bin = candidate
                break
    if uv_bin:
        backend = "uv"
    elif conda_bin:
        backend = "conda"
    else:
        backend = None
    return {
        "backend": backend,
        "uv": {"available": bool(uv_bin), "path": uv_bin},
        "conda": {"available": bool(conda_bin), "path": conda_bin},
    }


def build_env_setup_lines(env_backend_info: dict, env_path: str, envs_root: str, python_version: str) -> list[str]:
    if env_backend_info["backend"] == "uv":
        uv_bin = env_backend_info["uv"]["path"] or "uv"
        return [
            "# ====== uv virtual environment backend ======",
            f"UV_BIN='{uv_bin}'",
            "if [ ! -x \"$UV_BIN\" ] && ! command -v \"$UV_BIN\" >/dev/null 2>&1; then echo 'missing uv'; exit 1; fi",
            f"if [ -d '{env_path}' ] && [ -f '{env_path}/bin/python' ]; then",
            f"  echo 'uv environment already exists: {env_path}'",
            "else",
            f"  echo 'create uv environment: python={python_version}'",
            f"  without_proxy \"$UV_BIN\" venv --python {python_version} {env_path}",
            "fi",
            f"source '{env_path}/bin/activate'",
            "PYTHON_PATH=$(which python 2>/dev/null || echo '')",
            f"if [[ ! \"$PYTHON_PATH\" =~ ^{envs_root}/ ]]; then echo 'environment path error: '$PYTHON_PATH; exit 1; fi",
        ]
    if env_backend_info["backend"] == "conda":
        conda_bin = env_backend_info["conda"]["path"] or "conda"
        return [
            "# ====== conda virtual environment backend ======",
            f"CONDA_BIN='{conda_bin}'",
            "CONDA_BASE=$(\"$CONDA_BIN\" info --base 2>/dev/null || echo /opt/conda)",
            "if [ ! -x \"$CONDA_BIN\" ]; then echo 'missing conda'; exit 1; fi",
            f"if [ -d '{env_path}' ] && [ -f '{env_path}/bin/python' ]; then",
            f"  echo 'conda environment already exists: {env_path}'",
            "else",
            f"  echo 'create conda environment: python={python_version}'",
            f"  without_proxy \"$CONDA_BIN\" create --prefix {env_path} python={python_version} -y > /dev/null",
            "fi",
            f"source \"$CONDA_BASE/bin/activate\" {env_path}",
            "PYTHON_PATH=$(which python 2>/dev/null || echo '')",
            f"if [[ ! \"$PYTHON_PATH\" =~ ^{envs_root}/ ]]; then echo 'environment path error: '$PYTHON_PATH; exit 1; fi",
        ]
    return ["echo 'missing uv or conda'; exit 1"]


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_activate_existing_env_lines(env_path: str, envs_root: str | None = None) -> list[str]:
    env_activate = _shell_quote(f"{env_path}/bin/activate")
    env_python = _shell_quote(f"{env_path}/bin/python")
    env_path_quoted = _shell_quote(env_path)
    lines = [
        "# ====== activate step_4 virtual environment ======",
        f"if [ -f {env_activate} ]; then",
        f"  source {env_activate}",
        f"elif [ -f {env_python} ]; then",
        "  CONDA_BIN=${CONDA_BIN:-$(command -v conda 2>/dev/null || true)}",
        "  if [ -z \"$CONDA_BIN\" ]; then",
        "    for CONDA_CANDIDATE in /opt/conda/bin/conda \"$HOME/miniconda3/bin/conda\" \"$HOME/anaconda3/bin/conda\"; do",
        "      if [ -x \"$CONDA_CANDIDATE\" ]; then CONDA_BIN=\"$CONDA_CANDIDATE\"; break; fi",
        "    done",
        "  fi",
        f"  if [ -z \"$CONDA_BIN\" ]; then echo 'missing conda for prefix environment: {env_path}'; exit 1; fi",
        "  CONDA_BASE=$(\"$CONDA_BIN\" info --base 2>/dev/null || echo /opt/conda)",
        f"  source \"$CONDA_BASE/bin/activate\" {env_path_quoted}",
        "else",
        f"  echo 'missing environment: {env_path}'; exit 1",
        "fi",
        "PYTHON_PATH=$(python -c 'import sys; print(sys.executable)' 2>/dev/null || which python 2>/dev/null || echo '')",
    ]
    if envs_root:
        lines.append(
            f"if [[ ! \"$PYTHON_PATH\" =~ ^{envs_root}/ ]]; then echo 'environment path error: '$PYTHON_PATH; exit 1; fi"
        )
    else:
        lines.append("if [ -z \"$PYTHON_PATH\" ]; then echo 'environment activation did not expose python'; exit 1; fi")
    return lines
