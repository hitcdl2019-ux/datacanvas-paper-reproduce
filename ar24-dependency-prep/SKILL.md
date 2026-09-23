---
name: ar24-dependency-prep
description: 在 AR24 复现流水线 step_4 使用；基于通用 README 解析结果写入 prep_plan.json，并只准备依赖环境。
---

# AR24 依赖准备 Skill

本 skill 只负责 step_4：生成 `prep_plan.json.readme_parse` 和统一 `environment_bundle`。Python 候选创建或复用 `$REPRO_ENVS_ROOT/<repo_name>`；Julia 候选可执行严格白名单内的 `Pkg.instantiate()`；OpenFOAM、MPI、系统求解器与容器只校验既有能力，不自动安装需管理员权限的软件。

step_4 是唯一创建/复用 uv 或 conda 环境的步骤。step_5、step_6、step_7 必须激活这个同一个环境后再执行。

执行前必须已完成 step_0.5 精确根路径确认与 `reserve-run`，并显式导出 `REPRO_BASE_ROOT`、`REPRO_ROOT`、`WORKSPACE_ROOT=$REPRO_ROOT`、`REPRO_OUTPUT_ROOT=$REPRO_ROOT`、`REPRO_ENVS_ROOT=$REPRO_ROOT/envs`、`CONDA_ENVS_PATH=$REPRO_ENVS_ROOT${CONDA_ENVS_PATH:+:$CONDA_ENVS_PATH}`、`RUN_ID`、`RUN_ROOT`、`DATASET_DIR`、`MODEL_DIR`。

## README 解析契约

`prep_plan_builder.py --auto-detect` 必须调用 `ar24-readme-parser/readme_parser.py`。README 的结构化结果写入：

- `readme_parse.documents`：带行号、原文、标题路径和覆盖率的 README AST。
- `readme_parse.commands`：README 中识别出的命令及来源 block。
- `readme_parse.links`：README 中识别出的链接及类型。
- `readme_parse.issues`：人工下载、占位符、不可执行上下文等问题。
- `readme_parse.unprojected_blocks`：已解析但不进入执行动作的内容及原因。
- `readme_parse.step_projection`：各 step 可以消费的派生结果。

step_4 只消费 `readme_parse.step_projection.dependency_cmds`。数据、权重、推理、评测、训练和编译命令只允许留在对应 step 的 projection 中。

## 依赖安装规则

- `pip install -r requirements.txt`、普通包安装等可以进入 `readme_parse.step_projection.dependency_cmds`。
- `pip install -e .`、`python setup.py`、`cmake`、`make`、`ninja`、编译型 wheel 和 CUDA/C++ 扩展必须进入 `readme_parse.step_projection.deferred_step7_cmds`。
- 若 step_0 输出 `env_backend=uv`，依赖安装最终必须统一为 `uv pip install --python "$REPRO_ENVS_ROOT/<repo_name>/bin/python" ...`。
- uv 后端下不得裸用 `pip`、`python -m pip`、`conda install`、`mamba install` 或 `micromamba install` 安装依赖。

## 执行命令

写入 README 解析计划：

```bash
python <SKILL_DIR(ar24-dependency-prep)>/prep_plan_builder.py --backend local --repo_name <repo_name> --run-id "$RUN_ID" --auto-detect
python <SKILL_DIR(ar24-dependency-prep)>/prep_plan_builder.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --run-id "$RUN_ID" --auto-detect
python <SKILL_DIR(ar24-dependency-prep)>/prep_plan_builder.py --backend cci --cci-state <cci_state.json> --repo_name <repo_name> --run-id "$RUN_ID"
```

安装 step_4 依赖：

```bash
python <SKILL_DIR(ar24-dependency-prep)>/dependency_runner.py --backend local --repo_name <repo_name> --python_version <v> --mode <step_0.mode> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
python <SKILL_DIR(ar24-dependency-prep)>/dependency_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --python_version <v> --mode <step_0.mode> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
REPRO_SSH_PASSWORD='<password>' python <SKILL_DIR(ar24-dependency-prep)>/dependency_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --python_version <v> --mode <step_0.mode> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
python <SKILL_DIR(ar24-dependency-prep)>/dependency_runner.py --backend cci --cci-state <cci_state.json> --repo_name <repo_name> --python_version <v> --mode <build_probe.mode> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

CCI 命令只允许在 CPU 构建实例上执行；该实例必须挂载用户确认的 NAS。step_4 完成后环境保存在 NAS 的 `$REPRO_ENVS_ROOT/<repo_name>`，供后续 GPU 执行实例复用。

## 成功标准

- `$RUN_ROOT/step_4/prep_plan.json` 存在，并包含 `readme_parse.step_projection`；依赖日志和环境描述也位于本轮 `step_4/`。
- `readme_parse.documents[*].coverage.uncovered_lines` 为空，或未覆盖原因已记录。
- `environment_bundle` 中选定候选所需的运行时已有版本证据；Python 候选还要求 `$REPRO_ENVS_ROOT/<repo_name>` 存在。
- step_5、step_6、step_7 的派生命令只被记录，尚未执行。
