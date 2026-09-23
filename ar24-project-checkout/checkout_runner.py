import argparse
import os
from pathlib import Path
import shlex
import sys
from urllib.parse import urlparse
import re

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ar24-auto-reproduct" / "scripts"))

import path_context
import preparation_recording
from backend_executor import execute_script


GITHUB_REPOSITORY_PATH = re.compile(
    r"^/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?P<git>\.git)?/?$"
)
GITHUB_SCP_URL = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?P<git>\.git)?$"
)


def _valid_repository_part(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and ".." not in value


def normalize_github_https_url(github_url: str) -> str:
    """Return an auditable GitHub HTTPS URL; never return an SSH transport URL."""
    value = str(github_url or "").strip()
    scp_match = GITHUB_SCP_URL.fullmatch(value)
    if scp_match:
        owner = scp_match.group("owner")
        repo = scp_match.group("repo")
        suffix = ".git" if scp_match.group("git") else ""
        if _valid_repository_part(owner) and _valid_repository_part(repo):
            return f"https://github.com/{owner}/{repo}{suffix}"

    parsed = urlparse(value)
    if parsed.query or parsed.fragment or parsed.params:
        raise ValueError("github_url must not contain query parameters or fragments")

    if parsed.scheme == "https":
        if parsed.netloc != "github.com":
            raise ValueError("github_url HTTPS host must be github.com")
    elif parsed.scheme == "ssh":
        if parsed.hostname != "github.com" or parsed.username != "git" or parsed.port is not None:
            raise ValueError("github_url SSH form must be ssh://git@github.com/<owner>/<repo>")
    else:
        raise ValueError("github_url must use GitHub HTTPS or a supported GitHub SSH input form")

    match = GITHUB_REPOSITORY_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("github_url must identify exactly one GitHub owner/repository")
    owner = match.group("owner")
    repo = match.group("repo")
    if not (_valid_repository_part(owner) and _valid_repository_part(repo)):
        raise ValueError("github_url contains an invalid owner or repository name")
    suffix = ".git" if match.group("git") else ""
    return f"https://github.com/{owner}/{repo}{suffix}"


def _https_git_prefix() -> list[str]:
    """Rewrite GitHub SSH submodule URLs without changing global Git config."""
    return [
        "git",
        "-c",
        "url.https://github.com/.insteadOf=git@github.com:",
        "-c",
        "url.https://github.com/.insteadOf=ssh://git@github.com/",
    ]


def _shell_command(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def build_checkout_script(
    github_url: str,
    repo_name: str,
    run_id: str,
    workspace_root: str | None = None,
    envs_root: str | None = None,
    reuse_source_code: str | None = None,
) -> str:
    clone_url = normalize_github_https_url(github_url)
    paths = path_context.project_paths(
        repo_name,
        workspace_root=workspace_root,
        envs_root=envs_root,
        run_id=run_id,
    )
    step_dir = paths["step_dirs"]["step_3"]
    git_prefix = _https_git_prefix()
    fetch_command = _shell_command([*git_prefix, "-C", paths["code_dir"], "fetch", "--all", "--tags"])
    clone_command = _shell_command([*git_prefix, "clone", "--recursive", clone_url, paths["code_dir"]])
    submodule_command = _shell_command([*git_prefix, "-C", paths["code_dir"], "submodule", "update", "--init", "--recursive"])
    if reuse_source_code:
        source_code = str(Path(reuse_source_code).expanduser())
        if source_code == paths["code_dir"]:
            raise ValueError("reuse_source_code must point to a different run")
        reuse_clone_command = _shell_command(
            [
                *git_prefix,
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                source_code,
                paths["code_dir"],
            ]
        )
        materialize_lines = [
            f"  test -d {shlex.quote(source_code)}/.git",
            f"  SOURCE_COMMIT=$(git -C {shlex.quote(source_code)} rev-parse HEAD)",
            f"  {reuse_clone_command}",
            f"  git -C {shlex.quote(paths['code_dir'])} checkout --detach \"$SOURCE_COMMIT\"",
            f"  git -C {shlex.quote(paths['code_dir'])} remote set-url origin {shlex.quote(clone_url)}",
            f"  git -C {shlex.quote(paths['code_dir'])} submodule sync --recursive",
            f"  {submodule_command}",
            f"  test \"$(git -C {shlex.quote(paths['code_dir'])} rev-parse HEAD)\" = \"$SOURCE_COMMIT\"",
            "  MATERIALIZATION_MODE=reused_independent_clone",
            f"  MATERIALIZATION_SOURCE={shlex.quote(source_code)}",
        ]
        existing_materialization_lines = [
            f"  test -d {shlex.quote(source_code)}/.git",
            f"  SOURCE_COMMIT=$(git -C {shlex.quote(source_code)} rev-parse HEAD)",
            f"  test \"$(git -C {shlex.quote(paths['code_dir'])} rev-parse HEAD)\" = \"$SOURCE_COMMIT\"",
            "  MATERIALIZATION_MODE=existing_reused_checkout",
            f"  MATERIALIZATION_SOURCE={shlex.quote(source_code)}",
        ]
    else:
        materialize_lines = [
            f"  {clone_command}",
            "  MATERIALIZATION_MODE=fresh_clone",
            "  MATERIALIZATION_SOURCE=",
        ]
        existing_materialization_lines = [
            "  MATERIALIZATION_MODE=existing_run_checkout",
            "  MATERIALIZATION_SOURCE=",
        ]
    return "\n".join(
        [
            "#!/bin/bash",
            "set -e",
            "echo 'start project checkout'",
            *path_context.export_lines(
                paths["workspace_root"], paths["envs_root"], repo_name, run_id
            ),
            f"mkdir -p {shlex.quote(paths['base_dir'])}",
            f"mkdir -p {shlex.quote(paths['dataset_dir'])} {shlex.quote(paths['model_dir'])}",
            f"if [ -d {shlex.quote(paths['code_dir'])}/.git ]; then",
            f"  echo 'code already cloned: {paths['code_dir']}'",
            f"  git -C {shlex.quote(paths['code_dir'])} remote set-url origin {shlex.quote(clone_url)}",
            f"  {fetch_command}",
            f"  git -C {shlex.quote(paths['code_dir'])} submodule sync --recursive",
            f"  {submodule_command}",
            *existing_materialization_lines,
            "else",
            f"  test ! -e {shlex.quote(paths['code_dir'])}",
            *materialize_lines,
            "fi",
            f"test -d {shlex.quote(paths['code_dir'])}/.git",
            f"test -d {shlex.quote(paths['dataset_dir'])}",
            f"test -d {shlex.quote(paths['model_dir'])}",
            "echo '[checkout evidence] record immutable source state'",
            f"EVIDENCE_DIR={shlex.quote(step_dir + '/checkout_evidence')}",
            "mkdir -p \"$EVIDENCE_DIR\"",
            f"git -C {shlex.quote(paths['code_dir'])} rev-parse HEAD > \"$EVIDENCE_DIR/commit.txt\"",
            f"git -C {shlex.quote(paths['code_dir'])} describe --tags --exact-match > \"$EVIDENCE_DIR/exact_tag.txt\" 2>/dev/null || true",
            f"git -C {shlex.quote(paths['code_dir'])} submodule status --recursive > \"$EVIDENCE_DIR/submodules.txt\"",
            "if command -v git-lfs >/dev/null 2>&1 || git lfs version >/dev/null 2>&1; then",
            "  LFS_STATUS=available",
            f"  git -C {shlex.quote(paths['code_dir'])} lfs ls-files > \"$EVIDENCE_DIR/git_lfs_files.txt\" 2>/dev/null || true",
            "else",
            "  LFS_STATUS=unavailable",
            "  : > \"$EVIDENCE_DIR/git_lfs_files.txt\"",
            "fi",
            f"cat > {shlex.quote(step_dir + '/checkout_state.json')} <<JSON",
            "{",
            '  "schema_version": "1.0",',
            f'  "repository": "{clone_url}",',
            f'  "input_repository": "{github_url}",',
            f'  "clone_repository": "{clone_url}",',
            '  "materialization_mode": "$MATERIALIZATION_MODE",',
            '  "materialization_source": "$MATERIALIZATION_SOURCE",',
            f'  "commit": "$(git -C {shlex.quote(paths["code_dir"])} rev-parse HEAD)",',
            f'  "exact_tag": "$(git -C {shlex.quote(paths["code_dir"])} describe --tags --exact-match 2>/dev/null || true)",',
            '  "submodules_evidence": "checkout_evidence/submodules.txt",',
            '  "git_lfs_status": "$LFS_STATUS",',
            '  "git_lfs_evidence": "checkout_evidence/git_lfs_files.txt"',
            "}",
            "JSON",
            f"test -s {shlex.quote(step_dir + '/checkout_state.json')}",
            "echo 'project checkout complete'",
        ]
    )


def run_local_checkout(**kwargs) -> str:
    return execute_script(build_checkout_script(**kwargs), backend="local", success_label="代码拉取")


def run_ssh_checkout(ssh_command: str, ssh_password: str | None = None, **kwargs) -> str:
    return execute_script(
        build_checkout_script(**kwargs),
        backend="ssh",
        ssh_command=ssh_command,
        ssh_password=ssh_password,
        success_label="代码拉取",
    )


def run_cci_checkout(cci_state: str, **kwargs) -> str:
    return execute_script(
        build_checkout_script(**kwargs),
        backend="cci",
        cci_state=cci_state,
        cci_role="build",
        success_label="代码拉取",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 3 project checkout runner")
    parser.add_argument("--backend", default="local", choices=["local", "ssh", "cci"])
    parser.add_argument("--ssh", "--ssh-command", dest="ssh_command", default=None)
    parser.add_argument("--ssh-password", dest="ssh_password", default=os.environ.get("REPRO_SSH_PASSWORD"))
    parser.add_argument("--cci-state", default=None)
    parser.add_argument("--github_url", required=True)
    parser.add_argument("--repo_name", required=True)
    parser.add_argument("--history")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--reuse-source-code",
        help="上一终态运行的 step_3/code；将按其 HEAD 物化独立 clone 并校验 commit",
    )
    parser.add_argument("--standalone", action="store_true")
    args = parser.parse_args()
    preparation_recording.validate_tracking_args(args.history, args.run_id, args.standalone)
    kwargs = {
        "github_url": args.github_url,
        "repo_name": args.repo_name,
        "run_id": args.run_id,
        "reuse_source_code": args.reuse_source_code,
    }
    started_at = preparation_recording.now_iso()
    if args.backend == "ssh":
        result = run_ssh_checkout(args.ssh_command, ssh_password=args.ssh_password, **kwargs)
    elif args.backend == "cci":
        result = run_cci_checkout(args.cci_state, **kwargs)
    else:
        result = run_local_checkout(**kwargs)
    if not args.standalone:
        project_root = Path(args.history).expanduser().parent
        result = preparation_recording.record_runner_result(
            history=args.history,
            run_id=args.run_id,
            step="step_3",
            backend=args.backend,
            result_text=result,
            started_at=started_at,
            fingerprint_payload={
                "github_url": normalize_github_https_url(args.github_url),
                "reuse_source_code": args.reuse_source_code,
            },
            evidence_paths=[
                str(
                    project_root
                    / args.run_id
                    / "step_3"
                    / "checkout_state.json"
                )
            ],
            ssh_command=args.ssh_command,
            ssh_password=args.ssh_password,
            cci_state=args.cci_state,
        )
    print(result)


if __name__ == "__main__":
    main()
