---
name: ar24-physics-validation
description: 对 SciML 和计算物理论文复现输出执行条件预检、数值与统计指标验证、适用的物理规律检查和证据产物生成，并通过不可拔高的结论门禁限制复现声明。仅在 step_7 已产出原始数值结果后使用。
---

# AR24 物理验证

仅在 step_7 中使用本技能，且所选构建、训练、推理或求解器运行必须已经产出原始数值结果。
本技能不能替代项目运行器，也不得将进程成功退出视为科学验证通过。

1. 按照 `references/SCHEMAS.md` 创建 `validation_spec.json`。
2. 将参考数组和预测数组保留为原始 `.json`、`.csv`、`.npy` 或 `.npz` 文件。图像只能作为证据输出，绝不能作为指标输入。
3. 当所选计划包含 Python 时，在 step_4 创建的环境包内运行 `scripts/validate_results.py --spec ... --output-dir ...`。
4. 保留验证失败的结果。不得将结论级别提高到 `claim_gate.achieved_level` 所记录值以上。
5. 如果未披露权威容差，则仅执行冒烟检查或定性检查；不得自行编造数值通过阈值。

固定阶段依次为：`validation_preflight`、`reference_acquisition`、
`prediction_acquisition`、`field_alignment`、`quantitative_validation`、
`physics_validation`、`validation_artifacts` 和 `claim_gate`。
