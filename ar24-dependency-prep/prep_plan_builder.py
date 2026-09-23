import argparse
import json
from pathlib import Path
import os
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_executor import execute_script
import path_context
import project_plan_inferer


def build_prep_plan(
    repo_name: str,
    run_id: str,
    python_version: str = "3.10",
    dependency_cmds=None,
    data_cmds=None,
    weight_cmds=None,
    deferred_step7_cmds=None,
    restricted_datasets=None,
    analysis_sources=None,
    warnings=None,
    readme_parse=None,
    vcpkg_json_path: str | None = None,
    workspace_root: str | None = None,
    envs_root: str | None = None,
) -> dict:
    if not str(run_id or "").strip():
        raise ValueError("run_id is required for the v2 step layout")
    paths = path_context.project_paths(
        repo_name,
        workspace_root=workspace_root,
        envs_root=envs_root,
        run_id=run_id,
    )
    readme_payload = _readme_parse_with_projection(
        readme_parse,
        dependency_cmds=dependency_cmds,
        data_cmds=data_cmds,
        weight_cmds=weight_cmds,
        deferred_step7_cmds=deferred_step7_cmds,
        restricted_datasets=restricted_datasets,
    )
    return {
        "schema_version": "2.0",
        "repo_name": repo_name,
        "run_id": run_id,
        "python_version": python_version,
        "vcpkg_json_path": vcpkg_json_path,
        "analysis_sources": analysis_sources or [],
        "warnings": warnings or [],
        "readme_parse": readme_payload,
        "environment_bundle": readme_payload.get("environment_bundle") or {
            "schema_version": "1.0",
            "runtimes": [{"name": "python", "required": True, "evidence": ["legacy_default"]}],
            "system_capabilities": [],
        },
        "paths": {
            "base_dir": paths["base_dir"],
            "code_dir": paths["code_dir"],
            "dataset_dir": paths["dataset_dir"],
            "model_dir": paths["model_dir"],
            "env_path": paths["env_path"],
        },
    }


def write_prep_plan(**kwargs) -> str:
    plan = build_prep_plan(**kwargs)
    plan_path = Path(
        path_context.project_paths(
            kwargs["repo_name"],
            workspace_root=kwargs.get("workspace_root"),
            envs_root=kwargs.get("envs_root"),
            run_id=kwargs["run_id"],
        )["prep_plan"]
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle_path = plan_path.with_name(f"{kwargs['run_id']}_environment_bundle.json")
    bundle_path.write_text(
        json.dumps(plan["environment_bundle"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(plan_path)


def _readme_parse_with_projection(
    readme_parse=None,
    dependency_cmds=None,
    data_cmds=None,
    weight_cmds=None,
    deferred_step7_cmds=None,
    restricted_datasets=None,
) -> dict:
    payload = dict(readme_parse or {})
    projection = dict(payload.get("step_projection") or {})
    projection.setdefault("dependency_cmds", [])
    projection.setdefault("runtime_dependency_cmds", [])
    projection.setdefault("data_cmds", [])
    projection.setdefault("weight_cmds", [])
    projection.setdefault("deferred_step7_cmds", [])
    projection.setdefault("restricted_datasets", [])
    projection.setdefault("physics_asset_links", [])

    for key, values in (
        ("dependency_cmds", dependency_cmds),
        ("data_cmds", data_cmds),
        ("weight_cmds", weight_cmds),
        ("deferred_step7_cmds", deferred_step7_cmds),
    ):
        for value in values or []:
            if value not in projection[key]:
                projection[key].append(value)
    for value in restricted_datasets or []:
        if value not in projection["restricted_datasets"]:
            projection["restricted_datasets"].append(value)
    payload["step_projection"] = projection
    return payload


def build_prep_plan_write_script(**kwargs) -> str:
    plan = build_prep_plan(**kwargs)
    paths = path_context.project_paths(
        kwargs["repo_name"],
        workspace_root=kwargs.get("workspace_root"),
        envs_root=kwargs.get("envs_root"),
        run_id=kwargs["run_id"],
    )
    payload = json.dumps(plan, ensure_ascii=False, indent=2)
    bundle_payload = json.dumps(plan["environment_bundle"], ensure_ascii=False, indent=2)
    bundle_path = f"{paths['step_dirs']['step_4']}/{kwargs['run_id']}_environment_bundle.json"
    return "\n".join(
        [
            "#!/bin/bash",
            "set -e",
            f"mkdir -p {shlex.quote(paths['base_dir'])}",
            f"cat > {shlex.quote(paths['prep_plan'])} <<'JSON'",
            payload,
            "JSON",
            f"cat > {shlex.quote(bundle_path)} <<'JSON_BUNDLE'",
            bundle_payload,
            "JSON_BUNDLE",
            f"test -s {shlex.quote(paths['prep_plan'])}",
            f"test -s {shlex.quote(bundle_path)}",
            f"echo 'prep_plan written: {paths['prep_plan']}'",
        ]
    )


def run_local_prep_plan(**kwargs) -> str:
    path = write_prep_plan(**kwargs)
    return f"本地准备计划写入成功：{path}"


def run_ssh_prep_plan(ssh_command: str, ssh_password: str | None = None, **kwargs) -> str:
    return execute_script(
        build_prep_plan_write_script(**kwargs),
        backend="ssh",
        ssh_command=ssh_command,
        ssh_password=ssh_password,
        success_label="准备计划写入",
    )


def run_cci_prep_plan(cci_state: str, **kwargs) -> str:
    return execute_script(
        build_prep_plan_write_script(**kwargs),
        backend="cci",
        cci_state=cci_state,
        cci_role="build",
        success_label="准备计划写入",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build step_4 prep_plan.json")
    parser.add_argument("--backend", default="local", choices=["local", "ssh", "cci"])
    parser.add_argument("--ssh", "--ssh-command", dest="ssh_command", default=None)
    parser.add_argument("--ssh-password", dest="ssh_password", default=os.environ.get("REPRO_SSH_PASSWORD"))
    parser.add_argument("--cci-state", default=None)
    parser.add_argument("--repo_name", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--python_version", default="3.10")
    parser.add_argument("--dependency_cmds", default="[]")
    parser.add_argument("--data_cmds", default="[]")
    parser.add_argument("--weight_cmds", default="[]")
    parser.add_argument("--deferred_step7_cmds", default="[]")
    parser.add_argument("--analysis_sources", default="[]")
    parser.add_argument("--restricted_datasets", default="[]")
    parser.add_argument("--warnings", default="[]")
    parser.add_argument("--readme_parse", default="{}")
    parser.add_argument("--vcpkg_json_path", default=None)
    parser.add_argument("--auto-detect", action="store_true")
    parser.add_argument("--code-dir", default=None)
    parser.add_argument("--audit-report", default=None)
    args = parser.parse_args()

    inferred = {}
    if args.auto_detect:
        paths = path_context.project_paths(args.repo_name, run_id=args.run_id)
        inferred = project_plan_inferer.infer_prep_plan_inputs(
            args.code_dir or paths["code_dir"],
            audit_report=args.audit_report,
        )

    dependency_cmds = json.loads(args.dependency_cmds)
    data_cmds = json.loads(args.data_cmds)
    weight_cmds = json.loads(args.weight_cmds)
    deferred_step7_cmds = json.loads(args.deferred_step7_cmds)
    analysis_sources = json.loads(args.analysis_sources)
    restricted_datasets = json.loads(args.restricted_datasets)
    warnings = json.loads(args.warnings)
    readme_parse = json.loads(args.readme_parse)

    kwargs = {
        "repo_name": args.repo_name,
        "run_id": args.run_id,
        "python_version": inferred.get("python_version", args.python_version)
        if args.python_version == "3.10"
        else args.python_version,
        "dependency_cmds": dependency_cmds,
        "data_cmds": data_cmds,
        "weight_cmds": weight_cmds,
        "deferred_step7_cmds": deferred_step7_cmds,
        "analysis_sources": analysis_sources or inferred.get("analysis_sources", []),
        "restricted_datasets": restricted_datasets,
        "warnings": warnings or inferred.get("warnings", []),
        "readme_parse": readme_parse or inferred.get("readme_parse", {}),
        "vcpkg_json_path": args.vcpkg_json_path or inferred.get("vcpkg_json_path"),
    }
    if args.backend == "ssh":
        print(run_ssh_prep_plan(args.ssh_command, ssh_password=args.ssh_password, **kwargs))
    elif args.backend == "cci":
        print(run_cci_prep_plan(args.cci_state, **kwargs))
    else:
        print(run_local_prep_plan(**kwargs))


if __name__ == "__main__":
    main()
