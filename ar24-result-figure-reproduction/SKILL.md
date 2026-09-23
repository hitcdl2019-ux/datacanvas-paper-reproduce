---
name: ar24-result-figure-reproduction
description: 使用论文中有页码证据的绘图规范和复现实验产生的原始数值数据，重绘曲线、散点、柱状、分布、热图、等值线、二维场和多面板实验结果图。用于 AR24 step_7 已完成推理、训练或数值计算及坐标/单位对齐之后；缺少坐标、单位、数据绑定、绘制方法或来源页码时必须逐图阻断，禁止目测猜图。
---

# AR24 论文结果图复现

仅在 step_7 已产生原始数值结果后使用本技能。图片是证据产物，不能作为指标计算输入。

## 执行顺序

1. 读取 step_1 生成并经 Agent 补全的 `paper_result_figure_specs.json`。
2. 激活 step_4 创建的同一个环境；不得在 step_7 安装依赖。
3. 核对每张图的论文页码、复现方法证据、变量、坐标、单位、采样范围、数据绑定和布局。
4. 优先运行已审计仓库内的官方绘图脚本；否则使用声明式 generic renderer。
5. 把输出写入 `$RUN_OUTPUT_ROOT/figures/`，生成 `figure_reproduction_result.json`。
6. 将成功图片合并到 `artifact_manifest.json`、对应案例的 `experiment_details` 和 `validation_result.validation_artifacts`。

## 严格纪律

- 只处理实验结果图，排除网络结构图、方法流程图和系统架构图。
- 只使用 `.npy/.npz/.csv/.tsv/.json` 原始数据；不得从论文截图反推数值。
- 只执行规范声明且带论文证据的变换。禁止 `eval`、动态代码和 shell 字符串。
- 坐标、单位、线性/对数尺度、范围、刻度、图例、分面和色条必须与规范一致。
- 关键字段缺失时将单图标为 `blocked`，保留缺口并继续其他图；不得生成近似图片。
- 选定计划要求的目标图未全部 `passed` 时，执行轮次不得标记为 `passed`。
- 官方脚本必须位于已审计的 `code/` 根目录内，以参数数组执行并记录脚本、参数、commit 和日志。
- SSH 模式在远端同一环境执行，并在报告前同步图片、规范和结果 JSON。

## 运行

```bash
python <SKILL_DIR(ar24-result-figure-reproduction)>/scripts/figure_runner.py \
  --spec "$RUN_ROOT/step_1/paper_result_figure_specs.json" \
  --run-output-root "$RUN_OUTPUT_ROOT" \
  --code-root "$RUN_ROOT/step_3/code" \
  --repo-commit "<checkout_state.commit>" \
  --artifact-manifest "$RUN_ROOT/step_6/artifact_manifest.json" \
  --experiment-details "$RUN_OUTPUT_ROOT/experiment_details.json" \
  --validation-result "$RUN_OUTPUT_ROOT/validation_result.json"
```

输入输出字段详见 [SCHEMA.md](references/SCHEMA.md)。运行器需要 NumPy、Matplotlib 和 Pillow；这些依赖必须由 step_4 写入同一 `environment_bundle`。

## 成功标准

- 每张目标图都有 `passed|blocked|failed|skipped_not_applicable` 状态。
- `passed` 图片的数据输入与输出文件都有大小和 SHA-256。
- `passed` 图片的结构一致性检查全部通过。
- 合并后的三类证据文件仍是合法 JSON，且保留原有内容。
