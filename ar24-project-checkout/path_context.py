import os
import re
import shlex


STEP_NAMES = (
    "step_precheck",
    "step_0",
    "step_1",
    "step_2",
    "step_2_5",
    "step_3",
    "step_4",
    "step_5",
    "step_6",
    "step_7",
    "step_7_5",
    "step_8",
)
RUN_ID_RE = re.compile(r"^run-\d{3,}$")


def resolve_workspace_root():
    env = (os.environ.get("WORKSPACE_ROOT") or "").strip()
    if env:
        return env
    raise RuntimeError(
        "WORKSPACE_ROOT is required. Ask the user for the exact dedicated "
        "reproduction root, validate it with path_policy.py, then export it."
    )


def resolve_envs_root(workspace_root: str | None = None):
    env = (
        os.environ.get("REPRO_ENVS_ROOT")
        or os.environ.get("CONDA_ENVS_ROOT")
        or ""
    ).strip()
    if env:
        return env
    workspace = workspace_root or resolve_workspace_root()
    return os.path.join(workspace, "envs")


def resolve_conda_envs_root():
    return resolve_envs_root()


def project_paths(
    repo_name: str,
    workspace_root: str | None = None,
    envs_root: str | None = None,
    run_id: str | None = None,
) -> dict:
    run = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(run):
        raise RuntimeError("run_id is required and must use the form run-NNN")
    workspace = os.path.abspath(
        os.path.expanduser(workspace_root or resolve_workspace_root())
    )
    envs = os.path.abspath(
        os.path.expanduser(envs_root or resolve_envs_root(workspace))
    )
    run_root = os.path.join(workspace, run)
    step_dirs = {name: os.path.join(run_root, name) for name in STEP_NAMES}
    return {
        "workspace_root": workspace,
        "project_root": workspace,
        "envs_root": envs,
        "base_dir": run_root,
        "run_id": run,
        "run_root": run_root,
        "code_dir": os.path.join(step_dirs["step_3"], "code"),
        "dataset_dir": os.path.join(workspace, "dataset"),
        "model_dir": os.path.join(workspace, "model"),
        "env_path": os.path.join(envs, repo_name),
        "history_path": os.path.join(workspace, "execution_run_history.json"),
        "prep_plan": os.path.join(step_dirs["step_4"], "prep_plan.json"),
        "audit_report": os.path.join(
            step_dirs["step_1"], f"{repo_name}_Audit_Report.md"
        ),
        "audit_score": os.path.join(step_dirs["step_1"], "audit_score.json"),
        "report_path": os.path.join(
            step_dirs["step_8"], f"{repo_name}_final_reproduce_report.docx"
        ),
        "run_output_root": step_dirs["step_7"],
        "step_dirs": step_dirs,
    }


def export_lines(
    workspace_root: str,
    envs_root: str,
    repo_name: str,
    run_id: str,
) -> list[str]:
    paths = project_paths(
        repo_name,
        workspace_root=workspace_root,
        envs_root=envs_root,
        run_id=run_id,
    )
    return [
        f"export REPRO_BASE_ROOT={shlex.quote(paths['workspace_root'])}",
        'export REPRO_ROOT="$REPRO_BASE_ROOT"',
        'export WORKSPACE_ROOT="$REPRO_ROOT"',
        'export REPRO_OUTPUT_ROOT="$REPRO_ROOT"',
        f"export REPRO_ENVS_ROOT={shlex.quote(paths['envs_root'])}",
        'export CONDA_ENVS_ROOT="$REPRO_ENVS_ROOT"',
        f"export CONDA_ENVS_PATH={shlex.quote(paths['envs_root'])}${{CONDA_ENVS_PATH:+:$CONDA_ENVS_PATH}}",
        f"export RUN_ID={shlex.quote(paths['run_id'])}",
        f"export RUN_ROOT={shlex.quote(paths['run_root'])}",
        f"export RUN_OUTPUT_ROOT={shlex.quote(paths['run_output_root'])}",
        f"export DATASET_DIR={shlex.quote(paths['dataset_dir'])}",
        f"export MODEL_DIR={shlex.quote(paths['model_dir'])}",
    ]
