---
name: ar24-project-checkout
description: 在 AR24 复现流水线 step_3 使用；当 resource_gate 通过后，在选定的本地、SSH 或 CCI 构建实例拉取 GitHub 仓库代码。
---

# AR24 项目代码拉取 Skill

本 skill 只负责 step_3：把仓库克隆到 `$RUN_ROOT/step_3/code/`，写入本轮 checkout 证据，并确认共享的 `$DATASET_DIR` 与 `$MODEL_DIR` 可用。复用代码时必须使用 `--reuse-source-code <source_run>/step_3/code` 物化无硬链接的独立 clone，并校验来源 HEAD。

必须在 step_2 的 `resource_gate` 通过后才能使用。若代码拉取失败，必须停止复现流水线，不得进入依赖准备、数据下载、权重下载或执行阶段。

## 输入参数

- `github_url`：GitHub HTTPS 或 SSH 仓库地址。
- `repo_name`：根路径清单中绑定的仓库名，不用作包装目录。
- `run_id`：路径确认后已预留的 `run-NNN`。
- `backend`：`local`、`ssh` 或 `cci`。
- `ssh`：`backend=ssh` 时必填，格式示例为 `ssh -p 40072 user@host`。
- `cci_state`：`backend=cci` 时必填，指向用户已确认且构建实例正在运行的本地安全状态文件。
- `REPRO_SSH_PASSWORD` 或 `--ssh-password`：可选的 SSH 密码认证入口。runner 使用 `sshpass -e`，不得记录或输出密码。

`WORKSPACE_ROOT` 和 `REPRO_ENVS_ROOT` 必须来自 step_0.5 的路径确认。缺少路径变量时必须硬失败。

## 执行命令

本地后端：

```bash
python <SKILL_DIR(ar24-project-checkout)>/checkout_runner.py --backend local --github_url <github_url> --repo_name <repo_name> --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

SSH 后端：

```bash
python <SKILL_DIR(ar24-project-checkout)>/checkout_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --github_url <github_url> --repo_name <repo_name> --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
REPRO_SSH_PASSWORD='<password>' python <SKILL_DIR(ar24-project-checkout)>/checkout_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --github_url <github_url> --repo_name <repo_name> --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

CCI 构建实例：

```bash
python <SKILL_DIR(ar24-project-checkout)>/checkout_runner.py --backend cci --cci-state <cci_state.json> --github_url <github_url> --repo_name <repo_name> --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

## 成功标准

- `$RUN_ROOT/step_3/code/.git` 存在，`step_3/checkout_state.json` 记录准确 commit。
- `$DATASET_DIR` 与 `$MODEL_DIR` 存在。
- 新运行不得直接修改旧运行的代码目录。
- `backend=ssh` 时不得在本机创建项目目录。
- `backend=cci` 时只在已确认的 CPU 构建实例和同一 NAS 上创建目录，并在台账中记录 `backend=cci`。
