---
name: ar24-data-download
description: 在 AR24 复现流水线 step_5 使用；依赖准备成功后，根据 README 或审计报告投影下载、准备、跳过数据集。
---

# AR24 数据下载 Skill

本 skill 只负责 step_5：读取本轮 step_4 数据投影，使用同一个 `environment_bundle`，并把实验、合成、DNS/LES 数据、网格、坐标、初边值条件和求解器 case 放到共享 `$DATASET_DIR`。下载日志、资产清单和结构化回执写入 `$RUN_ROOT/step_5/`。

执行任何数据命令前，必须先激活 step_4 创建的同一个环境；不得裸用 python、pip 执行项目命令。

## README 投影规则

本 skill 已包含以下规则：
- HuggingFace dataset 链接由 `ar24-readme-parser` 派生为 `readme_parse.step_projection.data_cmds`。
- `readme_parse.step_projection.restricted_datasets` 记录需要人工确认的数据集。
- Baidu、Google Drive、申请制数据集、需要账号或提取码的数据源必须暂停确认；用户选择跳过或使用示例数据时，要写入最终报告。
- 当 `readme_parse.step_projection.data_cmds` 为空，且没有必须下载的数据时，本步骤记录跳过并视为成功。

必须补齐并核对以下投影：

- 如果 step_1 审计报告或 README 标记了数据集下载链接，必须组装到 `prep_plan.json.readme_parse.step_projection.data_cmds` 中；不得因为链接需要整理、命令较长或数据集较大而跳过。受限数据集按下节暂停确认。
- 常见数据源下载方式如下；需要的工具必须先由 step_4 `dependency_cmds` 安装，step_5 不得把 `pip install ...` 放入 `data_cmds`：

| 数据源 | `dependency_cmds` 先准备 | `data_cmds` 下载或处理 |
| --- | --- | --- |
| Google Drive | `pip install gdown` | `gdown "<file_id>" -O <output>` |
| 百度网盘 | 无自动安装；记录链接和提取码 | 提示用户手动下载，不得伪造自动下载命令 |
| HuggingFace datasets | `pip install datasets` | `python -c "from datasets import load_dataset; ..."` |
| wget/curl 直链 | 通常无需额外依赖 | `wget "<url>" -O <output>` 或 `curl -L "<url>" -o <output>` |

- 如果下载后需要解压，`data_cmds` 必须包含对应的 `tar` 或 `unzip` 命令，并放在下载命令之后。
- 如果原始示例写成 `pip install gdown && gdown ...`，必须拆分为 step_4 `dependency_cmds` 的安装命令和 step_5 `data_cmds` 的下载命令；不得在 `data_cmds` 中使用 `&&` 串联安装与下载。

## 🚨 受限数据集识别与用户提示

若审计报告或 README 中发现以下任一特征，判定为受限数据集：

- 需要填写申请表或签署 `license agreement`。
- 需要机构邮箱、学术身份验证、账号登录或人工审批。
- 下载链接非公开，需要邮件获取或联系作者。
- 出现关键词：`request access`、`apply`、`agreement`、`contact authors`。

对于受限数据集，Agent 必须：

1. 在看板中标注「⚠️ 受限数据集: <数据集名称>」。
2. 向用户提示该数据集需要人工申请权限，并提供申请链接或联系方式。
3. 询问用户是否跳过该数据集，改用项目自带示例数据进行 Smoke Test。
4. 等待用户确认后再继续执行；确认前不得下载、伪造数据或进入依赖后续步骤。
5. 若用户选择跳过，在最终报告中注明「定量评估未执行（数据集需申请）」。

## 常见错误

- 审计报告已有数据集链接，但 `data_cmds` 为空或只写“用户自行下载”。
- 把下载工具安装命令放进 step_5 `data_cmds`，而不是 step_4 `dependency_cmds`。
- 下载压缩包后没有把解压命令写入 `data_cmds`。
- 遇到申请制、邮件获取或账号门槛的数据集时继续执行，没有先等待用户确认。

## 执行命令

本地后端：

```bash
python <SKILL_DIR(ar24-data-download)>/data_runner.py --backend local --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

SSH 后端：

```bash
python <SKILL_DIR(ar24-data-download)>/data_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
REPRO_SSH_PASSWORD='<password>' python <SKILL_DIR(ar24-data-download)>/data_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

CCI 构建实例：

```bash
python <SKILL_DIR(ar24-data-download)>/data_runner.py --backend cci --cci-state <cci_state.json> --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

## 成功标准

- `$DATASET_DIR` 已准备完成，或空投影在本轮 step_5 被明确记录为跳过。
- 受限数据集的处理选择已记录。
- 未执行任何模型权重下载命令。
- `artifact_manifest.json` 存在且所有已下载资产都有校验和。
