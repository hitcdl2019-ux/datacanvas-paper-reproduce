---
name: "ar24-repro-report"
description: "将复现过程中收集到的项目档案、排坑记录、本机实际测试结果、学术对齐结论、超参数和未完成项根因进行提炼，自动排版并生成工业级 Word (.docx) 技术规格书报告。当用户要求『生成复现报告』『导出 Word 文档』『完成复现结项』，或复现流水线进入收尾出报告阶段时使用。"
---

# 工业级复现报告生成器 (DOCX Engine)

## 技能定位

本 skill 只负责 AR24 流水线的 step_8：在 step_7.5 已获得用户明确输出授权后，先复核 step_1 已生成的 `<repo_name>_Audit_Report.md`，再把 step_0 到一个或多个 step_7 执行轮次中收集到的审计结论、论文基准、路径与环境信息、数据和权重处理状态、实际执行结果、未完成项根因与后续建议整理成唯一的 Word `.docx` 复现报告。最终产物集必须同时交付 DOCX 和该 Markdown 审计报告。DOCX 固定采用模板化报告的封面、目录、十章正文与三个附录结构。

报告内容必须来自当前项目的真实上下文，不得复用示例项目、固定硬件型号、固定论文指标或固定失败案例。算力描述必须来自 step_0 的实测后端；指标名称和值必须来自 step_1 的论文/官方基准与 step_7 的实际结果。

## 调用准备

调用 `generate_report` 前，先整理以下结构化信息：

- `project_profile`：项目名称、仓库地址、论文链接、领域、审计分数、执行后端、实测算力、输出根路径。
- `scientific_context`：研究背景、核心创新点、复现目标和官方/论文基准摘要。
- `readiness_matrix`：每个复现点或任务的代码入口、数据/权重状态、阻碍因素、推荐算力、预计成本或耗时。
- `actual_test_outcomes`：step_7 实际执行的命令、状态、输出物、耗时、显存/内存占用和关键日志摘要。
- `execution_plan_selection`：step_2.5 中用户确认的完整执行计划。第三、第四和第七部分的“复现点/案例”必须严格按计划中的复现点和阶段顺序罗列。
- `execution_run_history`：必填的 `execution_run_history.json` 路径或已解析对象。必须通过 step_7.5 报告授权校验，并作为实际执行计划、核心验证、实验产物、算力与模型来源对比的唯一事实源；缺失、为空或无法验证时禁止生成 DOCX。
- `experiment_details`：可选的逐实验动态详情。每项使用 `case_name/status/sections`，section 支持 `text`、`list`、`key_value`、`flow`、`table`、`artifacts`；显式 section 用于覆盖或补充自动汇总结果。
- `compute_usage`：论文披露与本次复现的实验范围、墙钟时间、分阶段 CPU/GPU 设备小时、原文证据和可比性。
- `scientific_repro_contract`、`artifact_manifest`、`validation_result`：物理工况/数值方法、资产来源/校验和、step_7 指标与 claim gate。缺少验证结果时禁止把软件可运行写成物理复现成功。
- `repro_conclusions`：每个指标或任务的官方基准、本次实测值、偏差说明和是否满足 smoke test 目标。
- `se_audit_results`：安全审计、路径污染、依赖/编译/运行排坑记录和已执行的修复动作。
- `repro_recommendations`：未完成项的真实原因、继续复现需要的资源、后续训练/评估/数据补齐建议。

## 执行红线

1. 最终复现报告只生成 DOCX：生成器必须从 `report_gate.run_id` 定位获授权运行，并只写入该轮 `step_8/<repo_name>_final_reproduce_report.docx`。最终产物集还必须包含同轮 `step_1/<repo_name>_Audit_Report.md`，并把审计正文嵌入 DOCX 的附录 C。调用方传入其他 `output_dir` 也不得改变这一归档位置。
2. 不重新审计、不下载数据、不下载权重、不安装依赖、不运行推理或训练。缺少输入证据时在报告中标注缺口，不得编造。
3. `backend=ssh` 时，DOCX 和 `<repo_name>_Audit_Report.md` 必须一起同步回本地工作区后再交付，同时分别记录两份文件的远端原始路径与本地路径。
4. 报告中的硬件、指标、数据集和失败原因必须按当前项目动态填写，不能固定写某个具体项目、硬件型号或论文领域。
5. 论文没有披露墙钟时间时，在报告中标记 `论文未披露`，将 `paper.disclosed` 设为 `false`，仅令未知的时间/设备小时为空；论文已披露的设备类型、型号、数量、原文证据和页码必须保留，不得根据其他信息推断时间或设备小时。
6. `paper.scope_detail` 与 `reproduction.scope_detail` 必须使用同一 schema。使用 ML v1 时要求七字段完整；使用 v2 时要求相同的适用类别及类别内字段完整。无论哪种 schema，每个规范字段都必须非空；真正不适用时可写 `not_applicable`；缺失、`null`、空字符串、`unknown` 或 `undisclosed` 均阻断比较。训练类别的 `epochs 或 steps` 至少一项必须是实质值。
7. 任何墙钟或设备小时比例都要求论文提供非空 `evidence` 与非空 `source_page`。论文与复现实验范围不一致、完整结构化细节缺失/不匹配或 provenance 不足时，必须将 `comparison.comparable` 设为 `false`，在原因中标记 `不可直接比较`，且不得生成任何比例或节省结论。
8. 必须跟踪所有 step_7 记录的阶段完整性。无效/被拒绝阶段或 CPU 阶段缺少 `device_count_evidence` 时，不得静默只累计剩余阶段；必须渲染数据校验说明并阻断设备小时比例和节省结论。若论文 provenance、范围与双方墙钟时间独立完整，墙钟比较可独立保留。
9. 设备小时比例必须有论文正整数 `device_count`，即使论文提供显式 `device_hours` 也不例外。论文同时给出墙钟时间、设备数量和显式 `device_hours` 时，必须校验后者等于 `墙钟时间 × 设备数量`；不一致时拒绝该总数并渲染校验说明。
10. CPU 阶段的 `device_count` 表示显式分配的逻辑核，必须提供命令、配置或环境变量形式的 `device_count_evidence`；无法提供分配证据时，不得计算 CPU-hours。六列表格保持不变，并在设备数量单元格内追加 `device_count_evidence` 以便审计。
11. 所有报告都必须从 `execution_run_history` 中读取用户确认的完整执行计划。计划包含多个复现点时，使用 `execution_items`、`reproduction_points`、`cases`、`tasks`、`steps` 或 `plan_items` 数组；不得让外部传入的 `readiness_matrix`、`actual_test_outcomes` 或 `experiment_details` 改写其顺序或引入未选择候选。
12. `scope_detail` 接受 v2 的 `training`、`inference`、`numerical_simulation`、`experimental_fitting`、`coupled_simulation` 分类对象，同时继续兼容现有 ML v1 七字段对象。不同工况、网格、时间步、边界条件或 ensemble 数量不得标为可直接比较。
13. 第七部分“执行详情与产物”必须保留选定计划中的成功、失败、部分完成和跳过项。没有参数、数据流或产物证据时明确标注缺口，不得删掉实验或编造内容。
14. 自动汇总可以读取 `scientific_repro_contract`、`compute_usage.scope_detail`、`artifact_manifest` 和 `validation_result`。单实验时可接收未标注案例的公共证据；多实验时只按 `case_name` 或 `physical_metadata.case` 精确归属，未标注内容放入“公共/未归属证据”，标注了未选择候选的内容不进入报告。
15. 整篇正文跨所有实际终态轮次最多精选 8 张有效实验图片，可以少于 8 张但不得超过；优先选择 `role=paper_result_figure` 放入第六章，再按显式重点产物、验证图、运行与实验顺序稳定选择。图片缺失、损坏或不可读取时不占额度，并继续选择后续有效图片。PNG/JPEG/GIF/BMP/TIFF 与 SVG 可内嵌；CSV/TSV/JSON 表格最多预览 20 行、10 列，且不占图片额度。
16. 完整产物清单必须记录名称、角色、路径、来源、大小、SHA-256 和可用状态。文件缺失、不可读或不支持预览时只记录缺口，不得阻断 DOCX 生成。JSON 文件中的相对路径以该 JSON 所在目录为基准；直接传入对象时以报告输出目录为基准。
17. 必须先从获授权运行记录的 `audit_report` 与 `audit_score` 路径校验证据存在、非空且 UTF-8 可读，并确认评分对象有效。只接受 `schema_version=2.0`、`layout_version=3`，对终态轮次按实际到达阶段执行条件化校验；完整执行轮次仍必须通过 preparation、案例、算力、科学契约、资产清单、七阶段验证和 `terminal_summary` 门禁。旧布局或不兼容清单必须阻断，不得自动推测、移动或覆盖。
18. 多轮报告只包含 `passed`、`partial`、`failed`、`skipped` 轮次；`planned` 表示尚未实际开始，必须排除。同一案例重复运行时按 `(run_id, case_name)` 独立展示，禁止合并或以最新结果覆盖。
19. 第三、第六、第七、第九和第十章按 `sequence` 和每轮计划内案例顺序组织。算力章节先给出各轮累计运行成本，再逐轮判断论文可比性；不同实验范围不得合并计算论文效率比例。
20. 每轮未归属证据只能留在该轮的“公共/未归属证据”中。跨轮完整产物清单按路径或 SHA-256 去重，并在同一条记录中保留全部关联 `run_id`。
21. `execution_run_history` 中的相对证据路径以台账文件所在目录为基准。对象直传时以报告输出目录为基准；不得跨轮猜测验证结果、工况或产物归属。
22. 报告字体为不可覆盖的交付标准：所有中文字符使用宋体（Word 字体名 `SimSun`），所有英文、数字及拉丁字符使用 Times New Roman。标题、正文、表格、题注、页眉页脚和附录均适用；调用方传入其他字体参数时生成器必须忽略并强制恢复该组合。
23. 动态参数表、数据流表、普通结果表和可选环境配置表必须在创建前检查有效信息密度。空值、空集合、`-`、`—`、`未提供`、`未记录`、`not_recorded`、`N/A`、`null`、`none`、`unknown`、`未知`，以及以“未提供”“未记录”开头的通用占位文本均计为缺失；“论文未披露”“本步骤不需要”“未进入 step_X”“不可比较”“文件未同步”等明确状态不计为缺失。缺失率严格超过 50% 或没有数据时不创建表格、不生成题注或占用表格编号，而在原小节下显示“有效信息不足，已省略该表格（缺失 X/Y，比例 Z%）”；恰好 50% 时仍渲染。键值表只统计值列，数据流表不统计阶段标签列。执行状态、审计评分、步骤证据、CCI 资源、完整产物清单与附录 B 输出索引等关键证据表不应用该省略规则，必填证据缺失仍由门禁阻断。

## 固定报告结构

DOCX 必须依次包含：封面、目录、一“执行摘要”、二“复现环境与算力配置”、三“论文模型与实验概述”、四“代码审计与可行性评估”、五“依赖环境准备”、六“数据与模型权重”、七“执行详情与产物”、八“代码缺陷与排坑自愈指南”、九“论文复现结论”、十“后续建议”、附录 A“全部实验产出图片”、附录 B“输出文件索引”、附录 C“完整项目审计报告”。第一章必须保留“执行状态摘要”；第二章包含算力统计和 CCI 实例、价格、时长证据；第九章包含核心验证及自训练模型与预训练模型对比，并且只有在相同案例、数据集、评估协议和指标下计算差值，否则明确说明不可直接比较。附录 A 按 SHA-256 或路径去重后嵌入全部有效实验图片，正文已展示的图片仍须重复进入附录 A；无法嵌入的图片必须列出路径和失败原因。合规 `passed` 轮次的第一至第十章禁止出现“未记录”或 `not_recorded`。非适用显示“本步骤不需要”，论文没有提供的信息显示“论文未披露”，未进入某步骤显示“未进入 step_X：具体原因”，文件未同步、格式不支持、无校验和分别显示准确状态。

第七章多轮模式先按“第 N 次执行｜计划编号｜计划名称”分组，再展示轮次状态、后端、复用判定、耗时和验证等级；每个实验继续动态渲染参数、数据流、验证表、图表或项目自定义 section。可选表格若因缺失率严格超过 50% 被省略，小节标题仍须保留，并且后续表格编号必须连续。

## 调用示例

```json
{
  "repo_name": "<repo_name>",
  "project_profile": {
    "项目名称": "<repo_name>",
    "执行后端": "local | ssh | cci",
    "实测算力": "<来自 step_0 的 CPU/GPU 型号与数量>",
    "复现可行性打分": "<来自 audit_score.json>",
    "报告输出根": "${RUN_ROOT}/step_8"
  },
  "scientific_context": {
    "background": "<从论文或 README 提炼的背景>",
    "innovations": ["<创新点1>", "<创新点2>"]
  },
  "readiness_matrix": [
    {
      "case_name": "<复现点或任务名>",
      "config_path": "<代码入口或配置路径>",
      "status": "<ready | blocked | partial | skipped>",
      "barrier": "<数据、权重、依赖、算力或权限阻碍>",
      "gpu_spec": "<推荐或实际算力>",
      "est_time": "<预计或实测耗时>"
    }
  ],
  "actual_test_outcomes": [
    {
      "case_name": "<实际执行任务>",
      "status": "<passed | failed | partial | skipped>",
      "output_detail": "<输出物、日志或错误摘要>",
      "actual_time": "<实测耗时>"
    }
  ],
  "execution_plan_selection": {
    "selected_option_id": "<A/B/C/...>",
    "selected_execution_plan": {
      "name": "<用户确认的计划名称>",
      "execution_items": [
        {"name": "<复现点或阶段 1>"},
        {"name": "<复现点或阶段 2>"}
      ]
    }
  },
  "execution_run_history": "${WORKSPACE_ROOT}/execution_run_history.json",
  "experiment_details": [
    {
      "case_name": "<必须与选定计划中的实验名一致>",
      "status": "<passed | failed | partial | skipped>",
      "sections": [
        {
          "title": "仿真参数",
          "kind": "key_value",
          "content": {"<参数名>": "<参数值或证据>"}
        },
        {
          "title": "数据流",
          "kind": "flow",
          "content": [
            {"stage": "<阶段>", "input": "<输入/来源>", "operation": "<处理过程>", "output": "<输出/去向>"}
          ]
        },
        {
          "title": "实验产物",
          "kind": "artifacts",
          "content": [
            {
              "name": "<图、曲线、表格或数据文件名>",
              "role": "<validation_plot | metric_table | prediction | ...>",
              "path": "<本地或相对路径>",
              "caption": "<图表题注>",
              "sha256": "<可选校验和>",
              "featured": true
            }
          ]
        },
        {
          "title": "<项目自定义小节>",
          "kind": "text | list | key_value | flow | table | artifacts",
          "content": "<与 kind 匹配的内容>"
        }
      ]
    }
  ],
  "compute_usage": {
    "paper": {
      "disclosed": true,
      "scope": "<full_training | fine_tuning | inference | evaluation>",
      "scope_detail": {
        "dataset": "<数据集名称>",
        "data_scale": "<数据规模>",
        "epochs": "<轮数；真正不适用时为 not_applicable>",
        "steps": "<步数；真正不适用时为 not_applicable>",
        "sample_count": "<样本数；真正不适用时为 not_applicable>",
        "checkpoint": "<检查点；真正不适用时为 not_applicable>",
        "evaluation_protocol": "<评估协议；真正不适用时为 not_applicable>"
      },
      "wall_clock_hours": 24.0,
      "resource_type": "gpu",
      "device_model": "<论文披露的设备型号>",
      "device_count": 8,
      "device_hours": 192.0,
      "evidence": "<论文原文依据>",
      "source_page": "<页码>"
    },
    "reproduction": {
      "scope": "<与本次实际执行一致的范围>",
      "scope_detail": {
        "dataset": "<实际数据集名称>",
        "data_scale": "<实际数据规模>",
        "epochs": "<实际轮数；真正不适用时为 not_applicable>",
        "steps": "<实际步数；真正不适用时为 not_applicable>",
        "sample_count": "<实际样本数；真正不适用时为 not_applicable>",
        "checkpoint": "<实际检查点；真正不适用时为 not_applicable>",
        "evaluation_protocol": "<实际评估协议；真正不适用时为 not_applicable>"
      },
      "end_to_end_hours": 3.5,
      "step_7_hours": 1.25,
      "phases": [
        {
          "name": "<阶段名>",
          "status": "<completed | partial | failed>",
          "started_at": "<ISO-8601 时间>",
          "ended_at": "<ISO-8601 时间>",
          "duration_hours": 1.0,
          "resource_type": "gpu | cpu",
          "device_model": "<step_0 实测型号>",
          "device_count": 2,
          "device_count_evidence": "<CPU 必填：如 OMP_NUM_THREADS=2；GPU 可为 null>",
          "device_hours": 2.0
        }
      ]
    },
    "comparison": {
      "comparable": false,
      "reason": "<不可比原因；可比时可留空>"
    }
  },
  "repro_conclusions": [
    {
      "case_name": "<指标或任务>",
      "paper_data": "<论文/官方基准>",
      "repro_data": "<本次实测>",
      "status": "<对齐结论>"
    }
  ],
  "se_audit_results": {
    "secrets_leak": "<安全审计结论>",
    "path_pollution": "<路径污染结论>",
    "pitfalls": [
      {"id": "<编号>", "problem": "<问题>", "solution": "<处理动作>"}
    ],
    "healing_actions": ["<后续建议或已执行动作>"]
  },
  "repro_recommendations": {
    "unfinished_reasons": ["<未完成项根因>"],
    "oom_fix": "<如发生内存/显存不足时的项目特定建议；否则写 N/A>",
    "metrics_completion": "<后续指标补齐建议>",
    "training_advices": "<后续训练或推理优化建议>"
  }
}
```
