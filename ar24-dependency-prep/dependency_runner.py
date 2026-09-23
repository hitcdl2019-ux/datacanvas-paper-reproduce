import argparse
import json
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ar24-auto-reproduct" / "scripts"))

import command_safety
import env_backend
import path_context
import preparation_recording
from backend_executor import execute_script
from retry_policy import FALLBACK_PROXY, TSINGHUA_PIP_INDEX, TSINGHUA_TRUSTED_HOST, retry_with_proxy


def build_dependency_script(
    repo_name: str,
    run_id: str,
    python_version: str = "3.10",
    dependency_cmds=None,
    prep_plan: str | None = None,
    vcpkg_json_path: str | None = None,
    enable_elephant_detect: bool = True,
    mode: str | None = None,
    workspace_root: str | None = None,
    envs_root: str | None = None,
    env_backend_info: dict | None = None,
) -> str:
    plan_cmds = _load_projection_commands(prep_plan, "dependency_cmds")
    runtime_dependency_cmds = _load_projection_commands(prep_plan, "runtime_dependency_cmds") or []
    environment_bundle = _load_environment_bundle(prep_plan)
    dependency_cmds = dependency_cmds if dependency_cmds is not None else plan_cmds
    dependency_cmds = dependency_cmds or []
    mode = (mode or os.environ.get("REPRO_MODE") or "unknown").lower()

    paths = path_context.project_paths(
        repo_name,
        workspace_root=workspace_root,
        envs_root=envs_root,
        run_id=run_id,
    )
    env_info = env_backend_info or env_backend.select_env_backend()
    runtime_names = {item.get("name") for item in environment_bundle.get("runtimes", []) if isinstance(item, dict)}
    needs_python = "python" in runtime_names or not runtime_names
    if needs_python:
        command_safety.validate_dependency_command_list(dependency_cmds, "dependency_cmds", env_info)
    elif dependency_cmds:
        raise ValueError("non-Python environment_bundle cannot contain Python dependency_cmds")
    _validate_runtime_dependency_commands(runtime_dependency_cmds)
    proxy = os.environ.get("REPRO_HTTP_PROXY", "")

    script_lines = [
        "#!/bin/bash",
        "set -e",
        "echo 'start dependency preparation'",
        f"PROXY='{proxy}'",
        f"PIP_INDEX_URL='{TSINGHUA_PIP_INDEX}'",
        f"PIP_TRUSTED_HOST='{TSINGHUA_TRUSTED_HOST}'",
        f"FALLBACK_PROXY='{FALLBACK_PROXY}'",
        "with_proxy()    { if [ -n \"$PROXY\" ]; then http_proxy=\"$PROXY\" https_proxy=\"$PROXY\" \"$@\"; else \"$@\"; fi; }",
        "without_proxy() { env -u http_proxy -u https_proxy \"$@\"; }",
        *path_context.export_lines(
            paths["workspace_root"], paths["envs_root"], repo_name, run_id
        ),
        "export CUDA_HOME=${CUDA_HOME:-/usr/local/cuda}",
        "export PATH=$CUDA_HOME/bin:$PATH",
    ]

    if needs_python:
        script_lines.extend([
            "echo '[Python 环境] create or reuse virtual environment'",
            *env_backend.build_env_setup_lines(env_info, paths["env_path"], paths["envs_root"], python_version),
            "echo '[Python 工具] install gdown'",
        ])
        if env_info["backend"] == "uv":
            uv_bin = env_info.get("uv", {}).get("path") or "uv"
            gdown_cmd = f"\"{uv_bin}\" pip install --python '{paths['env_path']}/bin/python' gdown -q"
        else:
            gdown_cmd = "python -m pip install gdown -q"
        script_lines.append(retry_with_proxy(command_safety.patch_pip_command_for_tsinghua(gdown_cmd)))
    else:
        script_lines.append("echo '[Python 环境] selected plan does not require Python; skip uv/conda' ")
    script_lines.extend(
        [
            f"cd {shlex.quote(paths['code_dir'])}",
            f"mkdir -p {shlex.quote(paths['dataset_dir'])} {shlex.quote(paths['model_dir'])}",
        ]
    )

    if vcpkg_json_path and enable_elephant_detect:
        script_lines.append(f"echo '[依赖分析] vcpkg_json_path={vcpkg_json_path}, mode={mode}'")
    else:
        script_lines.append("echo '[依赖分析] no vcpkg_json_path, skip native dependency precheck'")

    if dependency_cmds:
        script_lines.append("echo '[依赖安装] start'")
        for index, cmd in enumerate(dependency_cmds, start=1):
            normalized = command_safety.normalize_python_cmd(cmd, env_info, paths["env_path"])
            patched = command_safety.patch_pip_command_for_tsinghua(normalized)
            script_lines.append(f"echo '[依赖 {index}/{len(dependency_cmds)}] {patched}'")
            script_lines.append(retry_with_proxy(patched, delay=5))
    else:
        script_lines.append("echo '[依赖安装] no dependency command, skip'")

    if runtime_dependency_cmds:
        script_lines.append("echo '[多运行时依赖] start' ")
        for index, cmd in enumerate(runtime_dependency_cmds, start=1):
            script_lines.append(f"echo '[运行时依赖 {index}/{len(runtime_dependency_cmds)}]' ")
            script_lines.append(cmd)

    script_lines.append("echo '[依赖校验] runtime smoke test'")
    if "julia" in runtime_names:
        script_lines.extend(["julia --version", "julia --project -e 'using Pkg; Pkg.status()'"])
    if needs_python:
        script_lines.extend([
            "echo '[依赖校验] import-level smoke test'",
            f"if [ -f '{paths['code_dir']}/requirements.txt' ]; then",
            f"  for PKG in $(cat '{paths['code_dir']}/requirements.txt' | grep -v '^#' | grep -v '^$' | sed 's/[>=<!].*//' | sed 's/\\[.*//' | tr '-' '_' | head -30); do",
            "    python -c \"import $PKG\" 2>/dev/null || echo \"warn import failed: $PKG\"",
            "  done",
            "else",
            "  echo 'requirements.txt not found, skip import smoke test'",
            "fi",
        ])
    script_lines.append("echo 'dependency preparation complete'")
    return "\n".join(script_lines)


def _load_projection_commands(prep_plan: str | None, key: str):
    if not prep_plan:
        return None
    payload = json.loads(Path(prep_plan).read_text(encoding="utf-8"))
    return payload.get("readme_parse", {}).get("step_projection", {}).get(key, [])


def _load_environment_bundle(prep_plan: str | None) -> dict:
    if not prep_plan:
        return {"schema_version": "1.0", "runtimes": [{"name": "python", "required": True}]}
    payload = json.loads(Path(prep_plan).read_text(encoding="utf-8"))
    return payload.get("environment_bundle") or payload.get("readme_parse", {}).get("environment_bundle") or {
        "schema_version": "1.0", "runtimes": [{"name": "python", "required": True}]
    }


def _validate_runtime_dependency_commands(commands) -> None:
    for command in commands:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"invalid runtime dependency command: {command!r}") from exc
        if not parts or parts[0] != "julia" or "-e" not in parts:
            raise ValueError(f"unsupported runtime dependency command: {command!r}")
        expression = parts[parts.index("-e") + 1] if parts.index("-e") + 1 < len(parts) else ""
        normalized = "".join(expression.split())
        if normalized not in {"usingPkg;Pkg.instantiate()", "importPkg;Pkg.instantiate()"}:
            raise ValueError("Julia step_4 commands are restricted to Pkg.instantiate()")


def run_local_dependency_prep(**kwargs) -> str:
    return execute_script(build_dependency_script(**kwargs), backend="local", success_label="依赖准备")


def run_ssh_dependency_prep(ssh_command: str, ssh_password: str | None = None, **kwargs) -> str:
    return execute_script(
        build_dependency_script(**kwargs),
        backend="ssh",
        ssh_command=ssh_command,
        ssh_password=ssh_password,
        success_label="依赖准备",
    )


def run_cci_dependency_prep(cci_state: str, **kwargs) -> str:
    return execute_script(
        build_dependency_script(**kwargs),
        backend="cci",
        cci_state=cci_state,
        cci_role="build",
        success_label="依赖准备",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 4 dependency preparation runner")
    parser.add_argument("--backend", default="local", choices=["local", "ssh", "cci"])
    parser.add_argument("--ssh", "--ssh-command", dest="ssh_command", default=None)
    parser.add_argument("--ssh-password", dest="ssh_password", default=os.environ.get("REPRO_SSH_PASSWORD"))
    parser.add_argument("--cci-state", default=None)
    parser.add_argument("--repo_name", required=True)
    parser.add_argument("--python_version", default="3.10")
    parser.add_argument("--dependency_cmds", default=None)
    parser.add_argument("--prep-plan", dest="prep_plan", default=None)
    parser.add_argument("--vcpkg_json_path", default=None)
    parser.add_argument("--enable_elephant_detect", default=True, type=lambda value: value.lower() != "false")
    parser.add_argument("--mode", default=os.environ.get("REPRO_MODE", "unknown"))
    parser.add_argument("--history")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--standalone", action="store_true")
    args = parser.parse_args()
    preparation_recording.validate_tracking_args(args.history, args.run_id, args.standalone)

    kwargs = {
        "repo_name": args.repo_name,
        "run_id": args.run_id,
        "python_version": args.python_version,
        "dependency_cmds": json.loads(args.dependency_cmds) if args.dependency_cmds is not None else None,
        "prep_plan": args.prep_plan,
        "vcpkg_json_path": args.vcpkg_json_path,
        "enable_elephant_detect": args.enable_elephant_detect,
        "mode": args.mode,
    }
    started_at = preparation_recording.now_iso()
    if args.backend == "ssh":
        result = run_ssh_dependency_prep(args.ssh_command, ssh_password=args.ssh_password, **kwargs)
    elif args.backend == "cci":
        result = run_cci_dependency_prep(args.cci_state, **kwargs)
    else:
        result = run_local_dependency_prep(**kwargs)
    if not args.standalone:
        plan_payload = (
            json.loads(Path(args.prep_plan).read_text(encoding="utf-8"))
            if args.prep_plan and Path(args.prep_plan).is_file()
            else {}
        )
        projection = plan_payload.get("readme_parse", {}).get("step_projection", {})
        dependency_commands = (
            json.loads(args.dependency_cmds)
            if args.dependency_cmds is not None
            else projection.get("dependency_cmds", [])
        )
        runtime_commands = projection.get("runtime_dependency_cmds", [])
        evidence_paths = [args.prep_plan] if args.prep_plan else []
        result = preparation_recording.record_runner_result(
            history=args.history,
            run_id=args.run_id,
            step="step_4",
            backend=args.backend,
            result_text=result,
            started_at=started_at,
            fingerprint_payload={
                "python_version": args.python_version,
                "mode": args.mode,
                "dependency_cmds": dependency_commands,
                "runtime_dependency_cmds": runtime_commands,
                "environment_bundle": plan_payload.get("environment_bundle")
                or plan_payload.get("readme_parse", {}).get("environment_bundle"),
            },
            evidence_paths=evidence_paths,
            ssh_command=args.ssh_command,
            ssh_password=args.ssh_password,
            cci_state=args.cci_state,
        )
    print(result)


if __name__ == "__main__":
    main()
