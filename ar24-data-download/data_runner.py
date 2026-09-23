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
from retry_policy import FALLBACK_PROXY, retry_with_proxy


def _load_plan_commands(prep_plan: str | None, key: str):
    if not prep_plan:
        return None
    payload = json.loads(Path(prep_plan).read_text(encoding="utf-8"))
    return payload.get("readme_parse", {}).get("step_projection", {}).get(key, [])


def _plan_requires_python(prep_plan: str | None) -> bool:
    if not prep_plan:
        return True
    payload = json.loads(Path(prep_plan).read_text(encoding="utf-8"))
    bundle = payload.get("environment_bundle") or payload.get("readme_parse", {}).get("environment_bundle") or {}
    runtimes = {item.get("name") for item in bundle.get("runtimes", []) if isinstance(item, dict)}
    return "python" in runtimes or not runtimes


def _shared_data_command(command: str) -> str:
    return str(command).replace("../dataset", '"$DATASET_DIR"')


def _manifest_lines(roots: list[str], output: str, origin: str, project_root: str) -> list[str]:
    roots_json = json.dumps(roots)
    return [
        "python3 - <<'PY_AR24_MANIFEST'",
        "import datetime, hashlib, json",
        "from pathlib import Path",
        f"roots = [Path(value) for value in {roots_json}]",
        f"project_root = Path({project_root!r})",
        "items = []",
        "for root in roots:",
        "    for path in sorted(p for p in root.rglob('*') if p.is_file()) if root.exists() else []:",
        "        digest = hashlib.sha256()",
        "        with path.open('rb') as handle:",
        "            for chunk in iter(lambda: handle.read(1048576), b''): digest.update(chunk)",
        "        suffix = path.suffix.casefold()",
        "        role = 'mesh' if suffix in {'.msh','.mesh','.foam','.vtk','.vtu','.stl'} else 'field_data' if suffix in {'.npy','.npz','.h5','.hdf5','.nc','.csv','.mat','.jld2'} else 'supporting_asset'",
        f"        items.append({{'role': role, 'path': str(path.relative_to(project_root)), 'size_bytes': path.stat().st_size, 'sha256': digest.hexdigest(), 'origin': {origin!r}, 'physical_metadata': {{}}}})",
        f"payload = {{'schema_version':'2.0','created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'roots':[str(p.relative_to(project_root)) for p in roots],'artifacts':items}}",
        f"Path({output!r}).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')",
        "PY_AR24_MANIFEST",
    ]


def build_data_script(
    repo_name: str,
    run_id: str,
    data_cmds=None,
    prep_plan: str | None = None,
    workspace_root: str | None = None,
    envs_root: str | None = None,
) -> str:
    plan_cmds = _load_plan_commands(prep_plan, "data_cmds")
    data_cmds = plan_cmds if plan_cmds is not None else (data_cmds or [])
    command_safety.validate_command_list(data_cmds, "data_cmds")

    if not str(run_id or "").strip():
        raise ValueError("run_id is required for step_5 outputs")
    paths = path_context.project_paths(
        repo_name, workspace_root=workspace_root, envs_root=envs_root, run_id=run_id
    )
    needs_python = _plan_requires_python(prep_plan)
    proxy = os.environ.get("REPRO_HTTP_PROXY", "")
    script_lines = [
        "#!/bin/bash",
        "set -e",
        "echo 'start data download'",
        f"PROXY='{proxy}'",
        f"FALLBACK_PROXY='{FALLBACK_PROXY}'",
        "with_proxy()    { if [ -n \"$PROXY\" ]; then http_proxy=\"$PROXY\" https_proxy=\"$PROXY\" \"$@\"; else \"$@\"; fi; }",
        *path_context.export_lines(
            paths["workspace_root"], paths["envs_root"], repo_name, run_id
        ),
        *(env_backend.build_activate_existing_env_lines(paths["env_path"], paths["envs_root"]) if needs_python else ["echo '[数据] non-Python plan; skip virtualenv activation'"]),
        f"mkdir -p {shlex.quote(paths['dataset_dir'])}",
        f"cd {shlex.quote(paths['code_dir'])}",
    ]
    if data_cmds:
        for index, cmd in enumerate(data_cmds, start=1):
            normalized = _shared_data_command(cmd)
            script_lines.append(
                f"echo '[数据 {index}/{len(data_cmds)}] {normalized}'"
            )
            script_lines.append(retry_with_proxy(normalized, delay=10))
    else:
        script_lines.append("echo '[数据] no data command, skip'")
    manifest_path = f"{paths['step_dirs']['step_5']}/{run_id}_dataset_artifact_manifest.json"
    script_lines.extend(
        _manifest_lines(
            [paths["dataset_dir"]],
            manifest_path,
            "step_5",
            paths["workspace_root"],
        )
    )
    script_lines.append("echo 'data download complete; dataset manifest snapshot written'")
    return "\n".join(script_lines)


def run_local_data_download(**kwargs) -> str:
    return execute_script(build_data_script(**kwargs), backend="local", success_label="数据下载")


def run_ssh_data_download(ssh_command: str, ssh_password: str | None = None, **kwargs) -> str:
    return execute_script(
        build_data_script(**kwargs),
        backend="ssh",
        ssh_command=ssh_command,
        ssh_password=ssh_password,
        success_label="数据下载",
    )


def run_cci_data_download(cci_state: str, **kwargs) -> str:
    return execute_script(
        build_data_script(**kwargs),
        backend="cci",
        cci_state=cci_state,
        cci_role="build",
        success_label="数据下载",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 5 data download runner")
    parser.add_argument("--backend", default="local", choices=["local", "ssh", "cci"])
    parser.add_argument("--ssh", "--ssh-command", dest="ssh_command", default=None)
    parser.add_argument("--ssh-password", dest="ssh_password", default=os.environ.get("REPRO_SSH_PASSWORD"))
    parser.add_argument("--cci-state", default=None)
    parser.add_argument("--repo_name", required=True)
    parser.add_argument("--data_cmds", default="[]")
    parser.add_argument("--prep-plan", dest="prep_plan", default=None)
    parser.add_argument("--history")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--standalone", action="store_true")
    args = parser.parse_args()
    preparation_recording.validate_tracking_args(args.history, args.run_id, args.standalone)
    kwargs = {
        "repo_name": args.repo_name,
        "run_id": args.run_id,
        "data_cmds": json.loads(args.data_cmds),
        "prep_plan": args.prep_plan,
    }
    started_at = preparation_recording.now_iso()
    if args.backend == "ssh":
        result = run_ssh_data_download(args.ssh_command, ssh_password=args.ssh_password, **kwargs)
    elif args.backend == "cci":
        result = run_cci_data_download(args.cci_state, **kwargs)
    else:
        result = run_local_data_download(**kwargs)
    if not args.standalone:
        plan_payload = (
            json.loads(Path(args.prep_plan).read_text(encoding="utf-8"))
            if args.prep_plan and Path(args.prep_plan).is_file()
            else {}
        )
        projected = plan_payload.get("readme_parse", {}).get("step_projection", {}).get("data_cmds")
        data_commands = projected if projected is not None else json.loads(args.data_cmds)
        project_root = Path(args.history).expanduser().parent
        manifest_path = (
            project_root
            / args.run_id
            / "step_5"
            / f"{args.run_id}_dataset_artifact_manifest.json"
        )
        result = preparation_recording.record_runner_result(
            history=args.history,
            run_id=args.run_id,
            step="step_5",
            backend=args.backend,
            result_text=result,
            started_at=started_at,
            fingerprint_payload={
                "data_cmds": data_commands,
                "declared_assets": plan_payload.get("data_assets") or [],
            },
            evidence_paths=[str(manifest_path)],
            skipped_not_required=not data_commands,
            ssh_command=args.ssh_command,
            ssh_password=args.ssh_password,
            cci_state=args.cci_state,
        )
    print(result)


if __name__ == "__main__":
    main()
