#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地算力探测 + 环境自检 (Local Compute Probe)

替代原 AR24 云端 instance_create / instance_stop。
本地化流水线无需"创建/释放实例"，改为探测本机算力并做环境体检，
输出统一 JSON 供后续流水线决策（单卡 / 多卡 / 纯 CPU 降级）。

用法:
    python probe.py            # 输出完整探测 + 自检 JSON
    python probe.py --brief    # 只输出 mode 与 gpu_count

输出关键字段:
    mode        : cpu | single_gpu | multi_gpu      —— 决定 step_4 执行分支
    cpu_model   : 实测 CPU 型号；无法读取时为 null
    gpu_count   : 本机可见 GPU 数量
    gpus        : [{index, name, vram_gb, capability}]
    arch_list   : TORCH_CUDA_ARCH_LIST 动态值（如 "8.6;8.9"），供 CUDA 扩展编译
    workspace_root   : 解析后的项目工作根（env > /workspace可写 > $HOME/auto-reproduction）
    envs_root        : 解析后的虚拟环境根（同上规则）
    env_backend      : uv | conda
    conda_envs_root  : envs_root 的兼容别名
    checks      : 环境体检结果（uv / conda / nvcc / 磁盘 / 内存）
    ready       : 是否满足最低运行条件
    warnings    : 不致命但需提示用户的问题
    blockers    : 仅连接/探测本身的致命问题；缺少某个运行时只限制对应候选
"""

import os
import sys
import json
import platform
import shutil
import shlex
import subprocess


def _writable(path: str) -> bool:
    """目录可写判定：存在且可写，或其最近的已存在父级可写（即能在其下创建）。"""
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return bool(p) and os.access(p, os.W_OK)


def resolve_workspace_root() -> str:
    """
    Return explicit WORKSPACE_ROOT after path confirmation.
    Missing paths are reported as pending user input, not guessed here.
    """
    return (os.environ.get("WORKSPACE_ROOT") or "").strip() or None


def resolve_envs_root() -> str:
    """
    Return explicit REPRO_ENVS_ROOT/CONDA_ENVS_ROOT after path confirmation.
    """
    return (
        os.environ.get("REPRO_ENVS_ROOT")
        or os.environ.get("CONDA_ENVS_ROOT")
        or ""
    ).strip() or None


def resolve_conda_envs_root() -> str:
    """兼容旧调用方：返回通用虚拟环境根目录。"""
    return resolve_envs_root()


def _run(cmd, timeout=15):
    """执行命令，返回 (returncode, stdout)。失败返回 (-1, '')。"""
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return -1, ""


def probe_cpu_model() -> str | None:
    """Return a measured CPU model without inventing a fallback label."""
    system = (platform.system() or "").strip().casefold()
    if system == "windows":
        identifier = (os.environ.get("PROCESSOR_IDENTIFIER") or "").strip()
        if identifier:
            return identifier
        model = (platform.processor() or "").strip()
        return model or None

    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as cpuinfo:
            for line in cpuinfo:
                if line.lower().startswith("model name") and ":" in line:
                    model = line.split(":", 1)[1].strip()
                    if model:
                        return model
    except OSError:
        pass

    code, output = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if code == 0 and output.strip():
        return output.strip()
    model = (platform.processor() or "").strip()
    return model or None


def parse_ssh_connection(command: str) -> dict:
    """
    Parse a user-provided SSH command into structured connection fields.

    Supported examples:
      ssh -p 40072 shancong@60.171.65.231
      ssh shancong@gpu-box
      ssh -p 2222 gpu-alias

    The raw command is never executed directly by the pipeline.
    """
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"SSH 连接信息解析失败: {exc}") from exc

    if not parts or parts[0] != "ssh":
        raise ValueError("SSH 连接信息必须以 ssh 开头，例如: ssh -p 40072 user@host")

    port = 22
    target = None
    i = 1
    while i < len(parts):
        token = parts[i]
        if token in ("-p", "-l", "-i", "-J", "-F", "-o"):
            if i + 1 >= len(parts):
                raise ValueError(f"SSH 参数 {token} 缺少取值")
            if token == "-p":
                try:
                    port = int(parts[i + 1])
                except ValueError as exc:
                    raise ValueError("SSH 端口必须是数字") from exc
            i += 2
            continue
        if token.startswith("-p") and len(token) > 2:
            try:
                port = int(token[2:])
            except ValueError as exc:
                raise ValueError("SSH 端口必须是数字") from exc
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        target = token
        i += 1

    if not target:
        raise ValueError("SSH 连接信息缺少 user@host 或 host")

    if "@" in target:
        user, host = target.rsplit("@", 1)
    else:
        user, host = None, target

    if not host:
        raise ValueError("SSH 连接信息缺少 host")

    return {
        "user": user,
        "host": host,
        "port": port,
        "target": target,
    }


def paused_for_missing_ssh() -> dict:
    return {
        "backend": "ssh",
        "status": "paused",
        "ready": False,
        "pause_reason": "missing_ssh_connection",
        "next_action": "请提供 SSH 连接命令，例如: ssh -p 40072 shancong@60.171.65.231",
        "mode": None,
        "gpu_count": 0,
        "gpus": [],
        "warnings": [],
        "blockers": ["缺少 SSH 连接信息，等待用户输入后继续 step_0。"],
    }

def paused_for_missing_ssh_password(ssh: dict) -> dict:
    return {
        "backend": "ssh",
        "status": "paused",
        "ready": False,
        "pause_reason": "missing_ssh_password",
        "next_action": "SSH password is required. Ask the user for the password, then resume step_0 with REPRO_SSH_PASSWORD or --ssh-password.",
        "ssh": ssh,
        "mode": None,
        "gpu_count": 0,
        "gpus": [],
        "warnings": [],
        "blockers": ["SSH authentication failed; waiting for password to continue with sshpass."],
    }


def looks_like_ssh_auth_failure(output: str) -> bool:
    text = (output or "").lower()
    markers = (
        "permission denied",
        "authentication failed",
        "publickey",
        "keyboard-interactive",
        "password",
    )
    return any(marker in text for marker in markers)


def build_remote_probe_script() -> str:
    """Small self-contained Python probe copied to the remote shell over SSH."""
    return r"""python3 - <<'PY_REMOTE_PROBE'
import json
import os
import platform
import shutil
import subprocess


def writable(path):
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return bool(p) and os.access(p, os.W_OK)


def workspace_root():
    return (
        os.environ.get("WORKSPACE_ROOT")
        or ""
    ).strip() or None


def envs_root():
    return (
        os.environ.get("REPRO_ENVS_ROOT")
        or os.environ.get("CONDA_ENVS_ROOT")
        or ""
    ).strip() or None


def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return -1, ""


def probe_cpu_model():
    system = (platform.system() or "").strip().casefold()
    if system == "windows":
        identifier = (os.environ.get("PROCESSOR_IDENTIFIER") or "").strip()
        if identifier:
            return identifier
        model = (platform.processor() or "").strip()
        return model or None
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as cpuinfo:
            for line in cpuinfo:
                if line.lower().startswith("model name") and ":" in line:
                    model = line.split(":", 1)[1].strip()
                    if model:
                        return model
    except OSError:
        pass
    code, output = run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if code == 0 and output.strip():
        return output.strip()
    model = (platform.processor() or "").strip()
    return model or None


def probe_gpus():
    code, out = run([
        "nvidia-smi",
        "--query-gpu=index,name,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if code != 0 or not out.strip():
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "vram_gb": round(float(parts[2]) / 1024, 1),
                "capability": None,
            })
        except ValueError:
            pass
    return gpus


def check_uv():
    path = shutil.which("uv")
    return {"available": bool(path), "path": path}


def check_conda():
    path = shutil.which("conda")
    if not path:
        for cand in ("/opt/conda/bin/conda", os.path.expanduser("~/miniconda3/bin/conda"), os.path.expanduser("~/anaconda3/bin/conda")):
            if os.path.exists(cand):
                path = cand
                break
    return {"available": bool(path), "path": path}


def check_nvcc():
    path = shutil.which("nvcc")
    cuda_home = os.environ.get("CUDA_HOME") or "/usr/local/cuda"
    if not path and os.path.exists(f"{cuda_home}/bin/nvcc"):
        path = f"{cuda_home}/bin/nvcc"
    return {"available": bool(path), "path": path, "cuda_home": cuda_home if os.path.exists(cuda_home) else None}


def check_disk(root):
    target = root
    while target and not os.path.exists(target):
        target = os.path.dirname(target)
    target = target or "/"
    free_gb = None
    try:
        free_gb = round(shutil.disk_usage(target).free / (1024 ** 3), 1)
    except Exception:
        pass
    return {"path": target, "free_gb": free_gb}


def check_memory():
    total = None
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        code, out = run(["sysctl", "-n", "hw.memsize"])
        if code == 0 and out.strip().isdigit():
            total = int(out.strip())
    return {"total_gb": round(total / (1024 ** 3), 1) if total else None}


def check_tool(candidates, version_args=("--version",)):
    for name in candidates:
        path = shutil.which(name)
        if path:
            code, out = run([path, *version_args], timeout=5)
            return {"available": True, "path": path, "version": out.strip().splitlines()[0][:200] if code == 0 and out.strip() else None}
    return {"available": False, "path": None, "version": None}


def capability_checks():
    return {
        "memory": check_memory(),
        "compiler": check_tool(("gcc", "clang", "cc")),
        "cpp_compiler": check_tool(("g++", "clang++", "c++")),
        "cmake": check_tool(("cmake",)),
        "mpi": check_tool(("mpirun", "mpiexec")),
        "julia": check_tool(("julia",), ("--version",)),
        "openfoam": check_tool(("foamVersion", "foamRun", "simpleFoam")),
        "container": check_tool(("docker", "podman", "apptainer", "singularity")),
    }


gpus = probe_gpus()
gpu_count = len(gpus)
mode = "cpu" if gpu_count == 0 else "single_gpu" if gpu_count == 1 else "multi_gpu"
cpu_model = probe_cpu_model()
uv = check_uv()
conda = check_conda()
env_backend = "uv" if uv["available"] else "conda" if conda["available"] else None
root = workspace_root()
env_root = envs_root()
nvcc = check_nvcc()
disk = check_disk(root)
scientific = capability_checks()
path_policy = {
    "status": "ready" if root and env_root else "pending_user_input",
    "next_action": "Ask the user for a storage base root, validate it with path_policy.py, then export WORKSPACE_ROOT/REPRO_ENVS_ROOT.",
}

warnings = []
blockers = []
if not env_backend:
    warnings.append("远端未检测到 uv 或 conda；Python 候选不可执行，但 Julia/OpenFOAM/容器候选不因此全局熔断。")
if mode == "cpu":
    warnings.append("远端未探测到 GPU（nvidia-smi 不可用）。将以纯 CPU 模式运行。")
if mode != "cpu" and not nvcc["available"]:
    warnings.append("远端检测到 GPU 但未找到 nvcc（CUDA Toolkit）。源码编译 CUDA 扩展的项目可能失败。")
if disk["free_gb"] is not None and disk["free_gb"] < 50:
    warnings.append(f"远端工作盘可用空间仅 {disk['free_gb']}GB（{disk['path']}）。")

result = {
    "mode": mode,
    "gpu_count": gpu_count,
    "gpus": gpus,
    "arch_list": "",
    "cpu_model": cpu_model,
    "cpu_cores": os.cpu_count() or 1,
    "workspace_root": root,
    "envs_root": env_root,
    "env_backend": env_backend,
    "venv_path_example": os.path.join(env_root, "<repo_name>") if env_root else None,
    "conda_envs_root": env_root,
    "path_policy": path_policy,
    "checks": {"uv": uv, "conda": conda, "nvcc": nvcc, "disk": disk, **scientific},
    "ready": not blockers,
    "warnings": warnings,
    "blockers": blockers,
}
print(json.dumps(result, ensure_ascii=False))
PY_REMOTE_PROBE"""


def run_remote_probe(ssh_connection: str, ssh_password: str | None = None, timeout=60) -> dict:
    ssh = parse_ssh_connection(ssh_connection)
    password = ssh_password or ""
    if password:
        cmd = [
            "sshpass",
            "-e",
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-p",
            str(ssh["port"]),
            ssh["target"],
            build_remote_probe_script(),
        ]
        env = os.environ.copy()
        env["SSHPASS"] = password
    else:
        cmd = ["ssh", "-p", str(ssh["port"]), ssh["target"], build_remote_probe_script()]
        env = None
    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        **({"env": env} if env is not None else {}),
    )
    if completed.returncode != 0:
        combined_output = f"{completed.stderr or ''}\n{completed.stdout or ''}"
        if not password and looks_like_ssh_auth_failure(combined_output):
            return paused_for_missing_ssh_password(ssh)
        return {
            "backend": "ssh",
            "status": "error",
            "ready": False,
            "ssh": ssh,
            "mode": None,
            "gpu_count": 0,
            "gpus": [],
            "warnings": [],
            "blockers": [f"SSH 远端探测失败: {completed.stderr or completed.stdout}".strip()],
        }

    try:
        remote = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        return {
            "backend": "ssh",
            "status": "error",
            "ready": False,
            "ssh": ssh,
            "mode": None,
            "gpu_count": 0,
            "gpus": [],
            "warnings": [],
            "blockers": [f"SSH 远端探测输出不是合法 JSON: {exc}"],
            "raw_output": completed.stdout,
        }

    remote["backend"] = "ssh"
    remote["status"] = "ready" if remote.get("ready") else "blocked"
    remote["ssh"] = ssh
    return remote


def probe_gpus() -> list:
    """
    通过 nvidia-smi 探测 GPU。探测不到（无驱动/无卡/CPU-only 沙箱）返回 []。
    """
    code, out = _run([
        "nvidia-smi",
        "--query-gpu=index,name,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if code != 0 or not out.strip():
        return []

    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0])
            vram_gb = round(float(parts[2]) / 1024, 1)
        except ValueError:
            continue
        gpus.append({
            "index": idx,
            "name": parts[1],
            "vram_gb": vram_gb,
            "capability": None,  # 由 torch 补充（若可用）
        })
    return gpus


def enrich_capability(gpus: list):
    """
    用 torch.cuda.get_device_capability 补充计算能力（sm 架构）。
    torch 不可用时静默跳过——capability 留空，step_4 再降级处理。
    """
    if not gpus:
        return
    try:
        import torch
        if not torch.cuda.is_available():
            return
        for g in gpus:
            try:
                major, minor = torch.cuda.get_device_capability(g["index"])
                g["capability"] = f"{major}.{minor}"
            except Exception:
                pass
    except Exception:
        # 探测阶段 torch 通常还没装，正常现象
        pass


def build_arch_list(gpus: list) -> str:
    """
    根据各 GPU 的 capability 生成 TORCH_CUDA_ARCH_LIST。
    去重、排序，例 [8.6, 8.9] -> "8.6;8.9"。
    全部探测不到 capability 时返回空串（交由 PyTorch 自行决定）。
    """
    caps = sorted({g["capability"] for g in gpus if g.get("capability")})
    return ";".join(caps)


def check_conda() -> dict:
    """检测 conda 是否可用。uv 不可用时回退 conda。"""
    conda_bin = shutil.which("conda")
    if not conda_bin:
        # 兜底探测常见安装位置
        for cand in ("/opt/conda/bin/conda", os.path.expanduser("~/miniconda3/bin/conda"),
                     os.path.expanduser("~/anaconda3/bin/conda")):
            if os.path.exists(cand):
                conda_bin = cand
                break
    return {"available": bool(conda_bin), "path": conda_bin}


def check_uv() -> dict:
    """检测 uv 是否可用。流水线优先用 uv venv 创建隔离环境。"""
    uv_bin = shutil.which("uv")
    return {"available": bool(uv_bin), "path": uv_bin}


def select_env_backend(uv: dict, conda: dict) -> str | None:
    """优先 uv，缺 uv 时回退 conda；两者都缺失则返回 None。"""
    if uv.get("available"):
        return "uv"
    if conda.get("available"):
        return "conda"
    return None


def check_nvcc() -> dict:
    """检测 nvcc（CUDA Toolkit）。编译 flash-attn/nvdiffrast 等扩展需要，缺失非致命。"""
    nvcc = shutil.which("nvcc")
    cuda_home = os.environ.get("CUDA_HOME") or "/usr/local/cuda"
    if not nvcc and os.path.exists(f"{cuda_home}/bin/nvcc"):
        nvcc = f"{cuda_home}/bin/nvcc"
    return {
        "available": bool(nvcc),
        "path": nvcc,
        "cuda_home": cuda_home if os.path.exists(cuda_home) else None,
    }


def check_disk(workspace_root: str) -> dict:
    """检测工作目录所在盘可用空间。权重/数据动辄数十 GB，<50GB 给出告警。"""
    target = workspace_root
    while target and not os.path.exists(target):
        target = os.path.dirname(target)
    target = target or "/"
    try:
        usage = shutil.disk_usage(target)
        free_gb = round(usage.free / (1024 ** 3), 1)
    except Exception:
        free_gb = None
    return {"path": target, "free_gb": free_gb}


def check_memory() -> dict:
    """Return measured physical RAM without requiring psutil."""
    total = None
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        code, output = _run(["sysctl", "-n", "hw.memsize"])
        if code == 0 and output.strip().isdigit():
            total = int(output.strip())
    return {"total_gb": round(total / (1024 ** 3), 1) if total else None}


def check_tool(candidates, version_args=("--version",)) -> dict:
    """Probe one capability family and retain exact executable/version evidence."""
    for name in candidates:
        path = shutil.which(name)
        if not path:
            continue
        code, output = _run([path, *version_args], timeout=5)
        return {
            "available": True,
            "path": path,
            "version": output.strip().splitlines()[0][:200] if code == 0 and output.strip() else None,
        }
    return {"available": False, "path": None, "version": None}


def scientific_capability_checks() -> dict:
    return {
        "memory": check_memory(),
        "compiler": check_tool(("gcc", "clang", "cc")),
        "cpp_compiler": check_tool(("g++", "clang++", "c++")),
        "cmake": check_tool(("cmake",)),
        "mpi": check_tool(("mpirun", "mpiexec")),
        "julia": check_tool(("julia",), ("--version",)),
        "openfoam": check_tool(("foamVersion", "foamRun", "simpleFoam")),
        "container": check_tool(("docker", "podman", "apptainer", "singularity")),
    }


def main():
    brief = "--brief" in sys.argv
    backend = "local"
    ssh_connection = os.environ.get("REPRO_SSH_COMMAND") or ""
    ssh_password = os.environ.get("REPRO_SSH_PASSWORD") or ""
    cci_state = os.environ.get("AR24_CCI_STATE") or ""
    cci_role = "execution"

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--backend" and i + 1 < len(args):
            backend = args[i + 1]
            i += 2
            continue
        if args[i] in ("--ssh", "--ssh-command") and i + 1 < len(args):
            ssh_connection = args[i + 1]
            i += 2
            continue
        if args[i] in ("--ssh-password", "--password") and i + 1 < len(args):
            ssh_password = args[i + 1]
            i += 2
            continue
        if args[i] == "--cci-state" and i + 1 < len(args):
            cci_state = args[i + 1]
            i += 2
            continue
        if args[i] == "--cci-role" and i + 1 < len(args):
            cci_role = args[i + 1]
            i += 2
            continue
        i += 1

    if backend not in ("local", "ssh", "cci"):
        print(json.dumps({
            "backend": backend,
            "status": "error",
            "ready": False,
            "blockers": [f"不支持的 backend: {backend}"],
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    if backend == "ssh":
        if not ssh_connection:
            print(json.dumps(paused_for_missing_ssh(), ensure_ascii=False, indent=2))
            sys.exit(3)
        try:
            result = run_remote_probe(ssh_connection, ssh_password=ssh_password)
        except ValueError as exc:
            result = {
                "backend": "ssh",
                "status": "error",
                "ready": False,
                "blockers": [str(exc)],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") == "paused":
            sys.exit(3)
        sys.exit(0 if result.get("ready") else 2)

    if backend == "cci":
        if not cci_state:
            print(json.dumps({
                "backend": "cci",
                "status": "paused",
                "ready": False,
                "blockers": ["缺少 --cci-state；CCI 分支不会回退到本地或普通 SSH"],
            }, ensure_ascii=False, indent=2))
            sys.exit(3)
        try:
            from cci.client import MissingCciCredentials, MissingCciDependency
            from cci.state import CciStateStore
            from cci.transport import CciTransportError
            from cci_manager import probe_resource

            result = probe_resource(CciStateStore(cci_state), role=cci_role)
        except (MissingCciCredentials, MissingCciDependency, CciTransportError) as exc:
            result = {
                "backend": "cci",
                "role": cci_role,
                "status": "paused",
                "ready": False,
                "blockers": [str(exc)],
            }
        except Exception as exc:
            result = {
                "backend": "cci",
                "role": cci_role,
                "status": "error",
                "ready": False,
                "blockers": [str(exc)],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") == "paused":
            sys.exit(3)
        sys.exit(0 if result.get("ready") else 2)

    workspace_root = resolve_workspace_root()
    envs_root = resolve_envs_root()
    path_policy = {
        "status": "ready" if workspace_root and envs_root else "pending_user_input",
        "next_action": (
            "Ask the user for a storage base root, validate it with "
            "path_policy.py, then export WORKSPACE_ROOT/REPRO_ENVS_ROOT."
        ),
    }

    # ---- 1. GPU 探测 ----
    gpus = probe_gpus()
    enrich_capability(gpus)
    gpu_count = len(gpus)

    if gpu_count == 0:
        mode = "cpu"
    elif gpu_count == 1:
        mode = "single_gpu"
    else:
        mode = "multi_gpu"

    arch_list = build_arch_list(gpus)

    # ---- 2. 环境体检 ----
    uv = check_uv()
    conda = check_conda()
    env_backend = select_env_backend(uv, conda)
    nvcc = check_nvcc()
    disk = check_disk(workspace_root or os.getcwd())
    scientific_checks = scientific_capability_checks()
    cpu_model = probe_cpu_model()
    cpu_cores = os.cpu_count() or 1

    warnings = []
    blockers = []

    if not env_backend:
        warnings.append(
            "未检测到 uv 或 conda；Python 候选不可执行，但 Julia、OpenFOAM 或容器候选不因此全局熔断。"
        )
    if mode == "cpu":
        warnings.append(
            "未探测到 GPU（nvidia-smi 不可用）。将以纯 CPU 模式运行：跳过 CUDA 扩展编译，"
            "仅做 CPU 推理验证，GPU 性能指标不可得。"
        )
    if mode != "cpu" and not nvcc["available"]:
        warnings.append(
            "检测到 GPU 但未找到 nvcc（CUDA Toolkit）。需源码编译 CUDA 扩展"
            "（flash-attn/nvdiffrast 等）的项目可能失败；纯预编译 wheel 项目不受影响。"
        )
    if disk["free_gb"] is not None and disk["free_gb"] < 50:
        warnings.append(
            f"工作盘可用空间仅 {disk['free_gb']}GB（{disk['path']}）。"
            "权重/数据下载可能不足，建议清理或挂载更大磁盘。"
        )

    ready = len(blockers) == 0

    result = {
        "backend": "local",
        "status": "ready" if ready else "blocked",
        "mode": mode,
        "gpu_count": gpu_count,
        "gpus": gpus,
        "arch_list": arch_list,
        "cpu_model": cpu_model,
        "cpu_cores": cpu_cores,
        "workspace_root": workspace_root,
        "envs_root": envs_root,
        "env_backend": env_backend,
        "venv_path_example": os.path.join(envs_root, "<repo_name>") if envs_root else None,
        "conda_envs_root": envs_root,
        "path_policy": path_policy,
        "checks": {
            "uv": uv,
            "conda": conda,
            "nvcc": nvcc,
            "disk": disk,
            **scientific_checks,
        },
        "ready": ready,
        "warnings": warnings,
        "blockers": blockers,
    }

    if brief:
        result = {
            "mode": mode,
            "gpu_count": gpu_count,
            "cpu_model": cpu_model,
            "ready": ready,
            "env_backend": env_backend,
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    # 单一运行时缺失只限制对应计划，不改变全局 ready；连接/探测失败才熔断。
    sys.exit(0 if ready else 2)


if __name__ == "__main__":
    main()
