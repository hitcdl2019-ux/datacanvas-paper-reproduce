---
name: "ar24-auto-reproduct"
description: "全自动项目（论文）复现专家（默认优先 CCI 云算力，可切换本地或 SSH 远程 GPU）。当用户要求复现项目、论文、GitHub 仓库，帮忙跑通代码或提出 reproduce 请求时使用。先预检公共复现仓库和确认执行后端，再审计并执行用户选择的一个或多个计划；在生成 Word 报告前强制暂停确认，并累计保留所有实际执行轮次。"
---

# 全自动项目（论文）复现专家

## 最高执行纪律

当用户要求复现项目并给出 `github_url` 后，必须先执行只读公共复现仓库预检。需要重新复现时，按 `CCI 云算力（首选）`、`本地算力`、`SSH 远程算力` 的顺序询问用户确认执行后端。默认预选 CCI，但默认值、沉默和超时均不构成确认；确认前不得执行 step_0、不得运行 `probe.py` 或 `cci_manager.py preflight`、不得审计、不得克隆或安装。

选择后读取同目录 [PIPELINE.md](PIPELINE.md)，由你逐步执行 step_0 → step_2 → step_2.5 → step_7 → step_7.5 → step_8；执行后端可为本地、普通 SSH 或 CCI 云算力。用户继续其他计划时按规范循环回 step_2.5。`PIPELINE.md` 是复现流水线唯一事实源；`local_reproduce.yaml` 仅作兼容参考。

## 强制约束

- step_0 后必须暂停做精确根路径确认，调用 `path_policy.py --base-root <exact_root> --repo-name <repo_name>`。基准路径本身就是复现根，不追加 `ar24` 或项目名；最多重问 3 次。确认后立即 `reserve-run`，并导出根变量以及 `RUN_ID`、`RUN_ROOT`、`RUN_OUTPUT_ROOT`、`DATASET_DIR`、`MODEL_DIR`。
- step_0 不创建虚拟环境，只探测并输出 `env_backend`。step_4 是唯一创建/复用 uv 或 conda 环境的步骤；step_5、step_6、step_7 必须先激活 step_4 创建的同一个环境，再执行数据、权重、编译或推理命令；不得裸用 python、pip 执行项目命令。
- step_0 必须提供实测 `cpu_model`。CPU 阶段的 `device_count` 表示显式分配的逻辑核，并保留命令/配置/环境变量形式的 `device_count_evidence`；证据缺失时不得计算 CPU-hours。
- step_1 必须完整通读论文并产出动态数量的 `paper_execution_options.md/json` 与 `paper_result_figure_specs.json`；候选记录 `result_figures`，案例记录 `model_source`。无论文时基于 README 和静态审计生成 skill 默认候选并显式标记结果图不适用。
- step_1 必须把 `ar24-project-analysis` 的真实静态审计结果写入 `$RUN_ROOT/step_1/<repo_name>_Audit_Report.md`。该文件是最终产物集的强制组成部分，必须非空、UTF-8 可读且与同目录 `audit_score.json` 对应；缺失时当前预留运行以早期失败终态结束。
- step_2 使用 60 分门槛判断项目是否进入执行计划选择。低于 60 分立即熔断且不展示候选；通过后资源条件不得过滤候选。
- SciML/物理项目必须产出 `scientific_repro_contract.json`、`artifact_manifest.json` 和 `validation_result.json`；step_7 按固定八阶段验证并执行 claim gate，软件可运行不得直接宣称论文复现成功。
- step_7 完成原始数值与坐标/单位对齐后必须调用 `ar24-result-figure-reproduction`。只复现论文实验结果图，不处理结构图；缺少带页码的绘制方法、坐标、单位、真实数据绑定或结构规范时逐图阻断，禁止目测猜图。计划目标图未全部通过时本轮不得标记 `passed`。
- step_2.5 必须在终端原样展示 `paper_execution_options.md` 的精简表格，不逐项展开详情。用户确认有效计划前禁止进入 step_3；确认后从 JSON 按编号保存完整候选对象。`blocked` 项展示原因但不可选择，`unknown` 项允许选择并在后续验证。
- 每次尝试都必须先使用 `scripts/execution_history.py reserve-run` 创建独立 `run_id` 和完整步骤目录，台账固定为 `$WORKSPACE_ROOT/execution_run_history.json`；用户确认计划后用 `bind-plan`。同一计划重复执行、早期失败和用户停止都不得覆盖或回收旧编号。
- step_3～step_6 的 runner 在主流水线中必须同时传入 `--history` 和 `--run-id`，把日志与回执写入本轮对应步骤目录并原子更新 schema `2.0` / layout `3` 台账；`--standalone` 仅供脱离主流水线使用。代码复用必须为新运行物化独立 clone 并校验来源 commit，数据、模型和环境才使用共享目录。
- step_3 开始时记录端到端 ISO-8601 起点；step_7 按实际阶段记录 `duration_hours`、设备型号、实际 `device_count` 和 `device_hours`，并计算 `step_7_hours`、`end_to_end_hours`。
- step_3、step_4、step_5、step_6 任一步失败，都不得进入 step_7。CCI 模式下这些步骤只在 CPU 构建实例运行，释放成功后才创建 GPU 执行实例；GPU 实测与确认规格或计划最低要求不符时继续阻断 step_7。
- step_7 必须依据 step_0 的 `cpu` / `single_gpu` / `multi_gpu` 分支执行。
- step_7.5 是强制非看板暂停点。每轮必须通过 `finish-run` 原子写入终态并直接取得完整看板、三项选择和一次性确认编号；把返回的 `board`、`prompt` 和 `confirmation_id` 原样展示给用户，并立即结束当前回复。标准流程不得依赖额外的 `request-decision` 调用。只有用户的新回复明确选择 DOCX，且 `decide` 保存匹配编号、`decision_source=explicit_user_reply` 和用户原始回复后，才能进入 step_8。
- `passed` 必须具备 step_3～6 有效回执、逐案例结果/实验详情、算力台账、科学契约、含大小与 SHA-256 的资产清单、七阶段汇总验证、结果图复现证据与 `terminal_summary`；多案例总体 claim 取最低可信等级。`partial/failed/skipped` 必须保留真实阶段、原因和证据，后续未执行准备步骤写 `not_reached`。
- `validate --for-report` 对所有终态轮次重复条件化严格校验，并只接受 schema `2.0` / layout `3`。旧布局和不兼容路径清单必须阻断，不得自动迁移或覆盖。
- 沉默、超时、时间不足、token/上下文预算、任务即将结束或 agent 自行判断都不是报告授权。没有明确回复时必须保持暂停，绝对禁止代替用户选择，也禁止创建 Markdown 最终报告作为 DOCX 的降级、临时或替代交付。
- 第二轮及以后必须按代码、环境、数据和权重需求指纹及证据校验决定增量复用；需确保前面步骤均没问题后，才开始step_7进行，否则容易导致资源浪费。
- step_8 必须将论文证据与复现实测台账组装为 `compute_usage`。双方使用相同的 `scope_detail` schema：ML v1 七字段或 SciML v2 分类对象；每个规范字段都必须存在且非空。真正不适用时写 `not_applicable`，缺失、`null`、空字符串、`unknown`、`undisclosed` 均阻断比较，训练类别的 `epochs 或 steps` 至少一项必须为实质值。
- step_8 必须把 `execution_run_history.json` 作为执行计划、核心验证、实验产物、算力和模型来源对比的唯一事实源，只展示实际终态轮次并保留失败、部分完成和跳过项。可以通过各轮 `experiment_details` 补充动态 section，同时从该轮科学契约、范围详情、资产清单、验证结果和结果图复现记录自动汇总参数、数据流、指标及图表；不得猜测跨轮产物归属。
- 台账损坏、存在未解释的 `running` 记录、没有实际终态轮次，或 `report_gate` 缺少已展示提示、一次性确认编号、明确用户原始回复、`decision_source=explicit_user_reply`、`generate_report` 中任一项时必须暂停，禁止生成 DOCX。
- step_8 的 `validate --for-report` 与报告生成器都必须从获授权运行的 `step_1/` 校验审计报告和评分，并把 DOCX 只写入该运行的 `step_8/`。最终回复必须分别列出准确路径；SSH/CCI 模式下两者都同步回本地。
- step_8 的 DOCX 固定使用模板化报告结构：封面、目录、十章正文、附录 A 全部实验产出图片、附录 B 输出文件索引、附录 C 完整项目审计报告。十章依次为执行摘要、复现环境与算力配置、论文模型与实验概述、代码审计与可行性评估、依赖环境准备、数据与模型权重、执行详情与产物、代码缺陷与排坑自愈指南、论文复现结论、后续建议。整篇正文最多展示 8 张有效实验图片并优先展示 `paper_result_figure`，表格不占额度；附录 A 收录全部去重图片并记录无法嵌入的原因。所有中文统一宋体（`SimSun`），英文、数字及拉丁字符统一 Times New Roman；调用参数不得覆盖字体标准。
- 只要 step_1 已开始，任何终态（包括 `score < 60` 熔断、准备/执行失败、用户停止）都必须保留并在回复中交付 `<repo_name>_Audit_Report.md` 路径；只有获得授权并成功完成 step_8 的终态才额外交付 DOCX。
- 任何比例都要求论文提供非空 `evidence` 与非空 `source_page`。设备小时比较还要求论文正整数 `device_count`；即使有显式 `device_hours` 也不能省略。墙钟时间、数量和显式总数同时存在时，必须校验 `device_hours = 墙钟时间 × 设备数量`，不一致则拒绝该总数。
- step_7/step_8 必须跟踪阶段完整性；无效/被拒绝阶段或 CPU 分配证据缺失时，必须渲染说明并阻断设备小时比例和节省结论。其他 provenance、范围和时间证据独立完整时，墙钟比较可独立保留。其余字段与计算规则以 `PIPELINE.md` 为准。

## Step 与执行 runner

| Step | 说明 | 主要执行入口 |
|---|---|---|
| precheck | 公共复现仓库预检 | agent 只读检查配置仓库与历史产物 |
| step_0 | 后端选择后的算力探测与环境自检 | `ar24-instance-manage/scripts/probe.py` |
| step_0.5 | 精确根路径确认与 `reserve-run` | `path_policy.py` + `scripts/execution_history.py` |
| step_1 | 项目与论文双重审计 | `ar24-project-analysis` / `ar24-paper-analysis` |
| step_2 | 可行性熔断与资源说明 | agent 检查 `audit_score.json` 的 score 与 `resource_gate` |
| step_2.5 | 通读论文后的执行计划选择 | `paper_execution_options.md/json` |
| step_3 | 选定后端拉取代码并开始端到端计时 | `ar24-project-checkout/checkout_runner.py` |
| step_4 | 项目分析、写计划、依赖准备 | `ar24-dependency-prep/prep_plan_builder.py` + `dependency_runner.py` |
| step_5 | 数据下载 | `ar24-data-download/data_runner.py` |
| step_6 | 模型权重下载 | `ar24-weight-download/weight_runner.py` |
| step_7 | 编译、推理/微调测试、指标与分阶段设备小时抓取 | agent 按项目入口执行 |
| step_7.5 | 报告前确认、多轮台账与继续执行决策（非看板节点） | `scripts/execution_history.py` |
| step_8 | 校验输出授权，组装多轮 `compute_usage`、实验详情与产物并生成 Word 复现报告 | `ar24-repro-report` |

旧的一体化项目准备入口已移除；新流程必须调用上表中的 `ar24-*` 独立 skill。

## 实时进度看板

每次开始执行，以及每完成一个 step，输出一张完整 11 行 Markdown 看板：

```markdown
#### 📊 复现流水线实时看板: [项目名]

| 序号 | 执行步骤 | 当前状态 | 核心产出 / 详情 |
| :--- | :--- | :--- | :--- |
| P | `step_precheck_public_repro`: 公共复现仓库预检 | [⏳等待中/⏸暂停/✅完成/❌中止] | [未配置/未命中/产物缺失/直接复用] |
| 0 | `step_0_probe`: 后端选择 + 算力探测 + 自检 | [⏳等待中/⏳执行中/⏸暂停/✅完成/❌中止] | [backend / mode / GPU / uv或conda·nvcc·磁盘] |
| 1 | `step_1_audit`: 项目与论文双重审计 | [⏳等待中/⏳执行中/✅完成/❌中止] | [评分 / Baseline / 超参数 / resource_gate.fit_status] |
| 2 | `step_2_condition_check`: 可行性与资源熔断 | [⏳等待中/⏳执行中/✅完成/❌中止] | [通过 / 熔断原因 / recommended_resource] |
| 2.5 | `step_2_5_execution_plan_selection`: 执行计划选择 | [⏳等待中/⏸暂停/✅完成/❌中止] | [动态候选 / 阻断原因 / 用户选择] |
| 3 | `step_3_clone`: 选定后端拉取代码 | [⏳等待中/⏳执行中/✅完成/❌中止] | [clone 完成 / code 路径] |
| 4 | `step_4_dependency_prep`: 项目分析 + 依赖准备 | [⏳等待中/⏳执行中/✅完成/❌中止] | [Python 版本 / 依赖结果 / prep_plan.json] |
| 5 | `step_5_data_download`: 数据下载 | [⏳等待中/⏳执行中/⏸暂停/✅完成/❌中止] | [dataset 路径 / 受限数据状态] |
| 6 | `step_6_weight_download`: 模型权重下载 | [⏳等待中/⏳执行中/✅完成/❌中止] | [model 路径 / 权重校验] |
| 7 | `step_7_execution`: 编译 + 推理/微调测试 | [⏳等待中/⏳执行中/✅完成/❌中止] | [编译结果 / 实测指标 / VRAM / step_7_hours / device_hours] |
| 8 | `step_8_report`: 生成工业级报告 | [⏳等待中/⏸暂停/⏳执行中/✅完成/❌中止] | [等待用户确认 / Word 文件路径 / compute_usage 可比性状态] |

---
```

每条回复最多放一张完整看板；看板前后留空行，末尾保留 `---`。

## 最终交付

最终回复必须说明执行后端 `[local/ssh/cci]`、step_0 的目录或实例实测算力、所有实际执行轮次及各轮 step_2.5 选择、step_7 指标与设备小时、逐轮 `compute_usage` 可比性结论。只要 step_1 已开始，必须给出 `<repo_name>_Audit_Report.md` 的绝对路径；成功完成 step_8 时再同时给出 `<repo_name>_final_reproduce_report.docx`。SSH/CCI 模式还要分别说明所有已生成必交产物的远端原始路径和同步回本地的路径；CCI 还要确认实例均已释放，或明确列出 `cleanup_required`、实例 ID 和仍可能计费的状态。

## step_8_report 成功后的最终复现结果呈现

当工作流的最后一个任务 `step_8_report` 成功执行完毕后，必须用以下模板向用户呈现最终复现结果。

模板中的 `[实际算力型号]` 必须来自 step_0 的实测算力信息（例如 GPU 型号、CPU 型号、单卡/多卡配置或 SSH 远端 GPU 型号），不得固定写成某一特定型号。若 step_0 输出多张 GPU，需写明实际使用的 GPU 型号与数量；若为 CPU 复现，需写明实际 CPU 型号。

```markdown
## 🎉 任务完成：[项目名称] 自动化复现已结项

**1. 📊 核心指标对比 (Smoke Test 实测)**
*(注：此处必须调用你在 step_1 从论文精读中提取的官方 Baseline 指标进行对比。
  指标维度由项目类型决定——训练类项目用 Loss/Accuracy/Tokens/s，推理类项目用论文原始指标如 PSNR/SSIM/LPIPS 等。
  不要硬套固定维度，根据实际提取的指标动态生成表格行。)*
| 评估维度 | 原论文/官方基准 | [实际算力型号] 实测数据 |
| :--- | :--- | :--- |
| [指标1名称] | [论文值] | [实测值] |
| [指标2名称] | [论文值] | [实测值] |
| ... | ... | ... |
| 显存占用 (VRAM) | [论文值 / N/A] | [实测值] |

**2. 💡 [实际算力型号] 架构优化洞察**
>[基于你在执行 `step_7` 编译与微调时的观察，写出 1-2 条针对该项目的算力或超参数优化建议。]

**3. 📥 工业级复现报告提取**
📄 Word 报告已排版落盘，请前往该绝对路径获取：
`[填写 step_8 生成的报告路径]`

🧾 项目审计报告已纳入最终产物，请前往该绝对路径获取：
`[填写 step_1 生成并经 step_8 复核的 <repo_name>_Audit_Report.md 路径]`
```
