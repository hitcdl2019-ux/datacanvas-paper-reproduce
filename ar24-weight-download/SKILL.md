---
name: ar24-weight-download
description: 在 AR24 复现流水线 step_6 使用；根据 README 解析投影下载模型权重并在执行前完成权重文件校验。
---

# AR24 权重下载 Skill

本 skill 只负责 step_6：读取本轮 step_4 权重投影，使用同一个 `environment_bundle`，把神经网络权重、BSON/JLD2 参数、normalizer、scaler、坐标尺度和本构参数放到共享 `$MODEL_DIR`；下载日志、资产清单和结构化回执写入 `$RUN_ROOT/step_6/`。

执行任何权重命令前，必须先激活 step_4 创建的同一个环境；不得裸用 python、pip 执行项目命令。

## README 投影规则

- HuggingFace model 链接由 `ar24-readme-parser` 派生为 `readme_parse.step_projection.weight_cmds`。
- Model Zoo 表格、demo/inference 中出现的模型路径提示，可以进入权重语义分析；只有明确可下载的模型资产才进入 step_6 投影。
- 默认使用 `HF_ENDPOINT=https://hf-mirror.com`。
- 下载后必须校验 `.pt`、`.pth`、`.safetensors`、`.pkl`、`.bin`、`.npz`、`.bson`、`.jld2`、`.ckpt`、`.onnx` 文件不是零字节。
- 当 `readme_parse.step_projection.weight_cmds` 为空，且项目没有必须下载的权重时，本步骤记录跳过并视为成功。

## 执行命令

本地后端：

```bash
python <SKILL_DIR(ar24-weight-download)>/weight_runner.py --backend local --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

SSH 后端：

```bash
python <SKILL_DIR(ar24-weight-download)>/weight_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
REPRO_SSH_PASSWORD='<password>' python <SKILL_DIR(ar24-weight-download)>/weight_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

CCI 构建实例：

```bash
python <SKILL_DIR(ar24-weight-download)>/weight_runner.py --backend cci --cci-state <cci_state.json> --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

## 成功标准

- `$MODEL_DIR` 已准备完成，或空投影在本轮 step_6 被明确记录为跳过。
- 权重下载后的零字节文件校验通过。
- 未执行任何数据下载或推理评测命令。
