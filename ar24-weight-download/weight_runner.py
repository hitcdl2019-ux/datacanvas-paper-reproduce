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


def _manifest_lines(roots: list[str], output: str, project_root: str) -> list[str]:
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
        "        lower = path.name.casefold(); suffix = path.suffix.casefold()",
        "        role = 'model_weight' if suffix in {'.pt','.pth','.ckpt','.safetensors','.onnx','.bson','.jld2','.bin'} else 'normalizer' if any(token in lower for token in ('normalizer','scaler','scale','mean','std')) else 'field_data' if suffix in {'.npy','.npz','.h5','.hdf5','.nc','.csv','.mat'} else 'supporting_asset'",
        "        items.append({'role':role,'path':str(path.relative_to(project_root)),'size_bytes':path.stat().st_size,'sha256':digest.hexdigest(),'origin':'step_6','physical_metadata':{}})",
        "payload={'schema_version':'2.0','created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'roots':[str(p.relative_to(project_root)) for p in roots],'artifacts':items}",
        f"Path({output!r}).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')",
        "PY_AR24_MANIFEST",
    ]


def _patch_hf_download_command(cmd: str) -> str:
    cmd = str(cmd).replace("../model", '"$MODEL_DIR"')
    if "huggingface-cli download" in cmd:
        hf_cmd = cmd.replace("huggingface-cli download", "hf download")
        return f"{{ {hf_cmd}; }} || {{ echo 'hf failed, fallback to huggingface-cli'; {cmd}; }}"
    return cmd


def build_weight_script(
    repo_name: str,
    run_id: str,
    weight_cmds=None,
    prep_plan: str | None = None,
    workspace_root: str | None = None,
    envs_root: str | None = None,
) -> str:
    plan_cmds = _load_plan_commands(prep_plan, "weight_cmds")
    weight_cmds = plan_cmds if plan_cmds is not None else (weight_cmds or [])
    command_safety.validate_command_list(weight_cmds, "weight_cmds")

    if not str(run_id or "").strip():
        raise ValueError("run_id is required for step_6 outputs")
    paths = path_context.project_paths(
        repo_name, workspace_root=workspace_root, envs_root=envs_root, run_id=run_id
    )
    needs_python = _plan_requires_python(prep_plan)
    proxy = os.environ.get("REPRO_HTTP_PROXY", "")
    script_lines = [
        "#!/bin/bash",
        "set -e",
        "echo 'start weight download'",
        f"PROXY='{proxy}'",
        f"FALLBACK_PROXY='{FALLBACK_PROXY}'",
        "with_proxy()    { if [ -n \"$PROXY\" ]; then http_proxy=\"$PROXY\" https_proxy=\"$PROXY\" \"$@\"; else \"$@\"; fi; }",
        "export HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com}",
        *path_context.export_lines(
            paths["workspace_root"], paths["envs_root"], repo_name, run_id
        ),
        *(env_backend.build_activate_existing_env_lines(paths["env_path"], paths["envs_root"]) if needs_python else ["echo '[权重] non-Python plan; skip virtualenv activation'"]),
        f"mkdir -p {shlex.quote(paths['model_dir'])}",
        f"cd {shlex.quote(paths['code_dir'])}",
    ]
    if weight_cmds:
        for index, cmd in enumerate(weight_cmds, start=1):
            patched = _patch_hf_download_command(cmd)
            script_lines.append(f"echo '[权重 {index}/{len(weight_cmds)}] {cmd}'")
            script_lines.append(retry_with_proxy(patched, delay=10))
    else:
        script_lines.append("echo '[权重] no weight command, skip'")

    script_lines.extend(
        [
            f"EMPTY_FILES=$(find {paths['model_dir']} -type f \\( -name '*.pt' -o -name '*.pth' -o -name '*.safetensors' -o -name '*.pkl' -o -name '*.bin' -o -name '*.npz' -o -name '*.bson' -o -name '*.jld2' -o -name '*.ckpt' -o -name '*.onnx' \\) -size 0 2>/dev/null)",
            "if [ -n \"$EMPTY_FILES\" ]; then echo 'zero-byte weight files found'; echo \"$EMPTY_FILES\"; exit 1; fi",
        ]
    )
    manifest_path = f"{paths['step_dirs']['step_6']}/{run_id}_data_model_artifact_manifest.json"
    script_lines.extend(
        _manifest_lines(
            [paths["dataset_dir"], paths["model_dir"]],
            manifest_path,
            paths["workspace_root"],
        )
    )
    script_lines.append("echo 'weight download complete; associated asset bundle recorded'")
    return "\n".join(script_lines)


def run_local_weight_download(**kwargs) -> str:
    return execute_script(build_weight_script(**kwargs), backend="local", success_label="模型权重下载")


def run_ssh_weight_download(ssh_command: str, ssh_password: str | None = None, **kwargs) -> str:
    return execute_script(
        build_weight_script(**kwargs),
        backend="ssh",
        ssh_command=ssh_command,
        ssh_password=ssh_password,
        success_label="模型权重下载",
    )


def run_cci_weight_download(cci_state: str, **kwargs) -> str:
    return execute_script(
        build_weight_script(**kwargs),
        backend="cci",
        cci_state=cci_state,
        cci_role="build",
        success_label="模型权重下载",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 6 model weight download runner")
    parser.add_argument("--backend", default="local", choices=["local", "ssh", "cci"])
    parser.add_argument("--ssh", "--ssh-command", dest="ssh_command", default=None)
    parser.add_argument("--ssh-password", dest="ssh_password", default=os.environ.get("REPRO_SSH_PASSWORD"))
    parser.add_argument("--cci-state", default=None)
    parser.add_argument("--repo_name", required=True)
    parser.add_argument("--weight_cmds", default="[]")
    parser.add_argument("--prep-plan", dest="prep_plan", default=None)
    parser.add_argument("--history")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--standalone", action="store_true")
    args = parser.parse_args()
    preparation_recording.validate_tracking_args(args.history, args.run_id, args.standalone)
    kwargs = {
        "repo_name": args.repo_name,
        "run_id": args.run_id,
        "weight_cmds": json.loads(args.weight_cmds),
        "prep_plan": args.prep_plan,
    }
    started_at = preparation_recording.now_iso()
    if args.backend == "ssh":
        result = run_ssh_weight_download(args.ssh_command, ssh_password=args.ssh_password, **kwargs)
    elif args.backend == "cci":
        result = run_cci_weight_download(args.cci_state, **kwargs)
    else:
        result = run_local_weight_download(**kwargs)
    if not args.standalone:
        plan_payload = (
            json.loads(Path(args.prep_plan).read_text(encoding="utf-8"))
            if args.prep_plan and Path(args.prep_plan).is_file()
            else {}
        )
        projected = plan_payload.get("readme_parse", {}).get("step_projection", {}).get("weight_cmds")
        weight_commands = projected if projected is not None else json.loads(args.weight_cmds)
        project_root = Path(args.history).expanduser().parent
        manifest_path = (
            project_root
            / args.run_id
            / "step_6"
            / f"{args.run_id}_data_model_artifact_manifest.json"
        )
        result = preparation_recording.record_runner_result(
            history=args.history,
            run_id=args.run_id,
            step="step_6",
            backend=args.backend,
            result_text=result,
            started_at=started_at,
            fingerprint_payload={
                "weight_cmds": weight_commands,
                "declared_assets": plan_payload.get("weight_assets") or [],
                "zero_byte_check": True,
            },
            evidence_paths=[str(manifest_path)],
            skipped_not_required=not weight_commands,
            ssh_command=args.ssh_command,
            ssh_password=args.ssh_password,
            cci_state=args.cci_state,
        )
    print(result)


if __name__ == "__main__":
    main()
