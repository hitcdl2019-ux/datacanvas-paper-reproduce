# PIPELINE.md — CCI 优先、本地与 SSH 可选的三后端复现流水线权威规范

> **本文件是包含公共仓库预检与执行计划选择门的复现流水线唯一事实源（single source of truth）。**
> 无 workflow 引擎的平台（Claude Code / Codex）由 agent 直接逐步跟随本文档执行。`local_reproduce.yaml` 是兼容参考，不是默认入口。

---

## 输入参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `github_url` | 是 | 需要复现的 GitHub 仓库地址 |
| `paper_url` | 否 | 关联论文 PDF/arXiv 链接 |
| `public_repro_repo_url` | 否 | 公共复现仓库地址；为空时跳过历史复现预检 |
| `public_repro_repo_type` | 否 | `git \| http_index \| filesystem`；默认 `unset` |
| `backend_choice` | 交互必填，默认预选 `cci` | 公共复现仓库预检确认需要重新复现后，按 `CCI 云算力（首选）`、`本地算力`、`SSH 远程算力` 的顺序询问用户确认。默认值不构成授权；确认前不得执行 step_0 |

## 路径与环境变量

路径不做自动兜底。step_0 探测 GPU、CPU、RAM、磁盘、编译器、CMake、MPI、Julia、OpenFOAM、容器与 uv/conda/nvcc；step_0 不创建环境。step_0 完成后必须暂停，请用户输入单个复现项目专用的精确基准路径，并调用 `ar24-instance-manage/scripts/path_policy.py --base-root <exact_root> --repo-name <repo_name>` 校验。该路径必须已存在、是目录、可读、可写、可进入，并且本身就是复现根；不得自动追加 `ar24`、项目名或自定义兜底目录。不合理时最多重问 **3 次**。

确认后必须显式导出：

```bash
export REPRO_BASE_ROOT="<exact_root>"
export REPRO_ROOT="$REPRO_BASE_ROOT"
export WORKSPACE_ROOT="$REPRO_ROOT"
export REPRO_OUTPUT_ROOT="$REPRO_ROOT"
export REPRO_ENVS_ROOT="$REPRO_ROOT/envs"
export CONDA_ENVS_ROOT="$REPRO_ENVS_ROOT"
export CONDA_ENVS_PATH="$REPRO_ENVS_ROOT${CONDA_ENVS_PATH:+:$CONDA_ENVS_PATH}"
export RUN_ID="run-NNN"
export RUN_ROOT="$WORKSPACE_ROOT/$RUN_ID"
export RUN_OUTPUT_ROOT="$RUN_ROOT/step_7"
export DATASET_DIR="$WORKSPACE_ROOT/dataset"
export MODEL_DIR="$WORKSPACE_ROOT/model"
```

根目录固定保存 `path_layout.json`、`execution_run_history.json`、共享的 `dataset/`、`model/`、`envs/<repo_name>/`，以及独立的 `run-NNN/`。每个运行目录都必须完整包含 `step_precheck`、`step_0`、`step_1`、`step_2`、`step_2_5`、`step_3`、`step_4`、`step_5`、`step_6`、`step_7`、`step_7_5`、`step_8`；代码固定在 `$RUN_ROOT/step_3/code`，本轮验证与运行产物固定在 `$RUN_ROOT/step_7`。路径清单使用 `ar24-path-layout/v3`，台账使用 `schema_version=2.0`、`layout_version=3`。

SSH 后端时，路径校验和目录创建都在远端执行；本机不得创建项目目录。CCI 后端的 `REPRO_BASE_ROOT` 与 `REPRO_ROOT` 都是用户明确确认的已有 NAS 精确目录，不追加任何后缀；创建构建实例后通过临时 SSH 做真实可写性和布局校验。付费实例创建前，控制面证据、路径清单和初始台账暂存到 CCI 状态返回的 `local_staging_root`，它不是远端复现根。

## 全局执行纪律

1. 用户给出 `github_url` 后，先执行 `step_precheck_public_repro`。命中完整历史复现时等待用户选择直接使用或重新复现；未配置、未命中或产物不齐时继续。
2. 需要重新复现时，默认预选并首先展示 `CCI 云算力`，随后提供 `本地算力` / `SSH 远程算力` 切换项。必须取得用户明确确认；默认值、沉默或超时均不得触发 step_0，也不得审计、克隆或安装。
3. step_1 必须完整通读论文并生成 `paper_execution_options.md/json` 与 `paper_result_figure_specs.json`；没有论文时基于 README 与静态审计生成 skill 默认候选并将结果图复现显式标记为不适用。
4. step_2 只使用 `score` 判断项目能否进入计划选择。`score < 60` 立即熔断且不展示候选；`score >= 60` 才进入 step_2.5。
5. step_2.5 必须展示全部候选，不得因 step_0 探测到的 CPU/GPU、显存或依赖条件隐藏候选。用户确认有效候选前禁止进入 step_3。
6. step_3、step_4、step_5、step_6 任一步失败，都禁止进入 step_7。
7. step_4 是唯一创建/复用 `environment_bundle` 的步骤。Python 候选必须使用同一个 uv/conda 环境；需要结果图复现时在本步准备 NumPy、Matplotlib 和 Pillow，step_7 禁止临时安装。Julia、OpenFOAM、MPI 与容器候选使用同一 bundle 中记录的运行时和版本。非 Python 候选不得因缺少 uv/conda 被全局熔断，也不得自动安装需管理员权限的系统软件。
8. 每轮实际执行结束（包括失败、部分完成或跳过）都必须进入非看板暂停点 step_7.5。`finish-run` 必须原子写入终态并直接返回完整看板、三项选择和一次性确认编号；把返回内容原样展示给用户后立即结束当前回复等待用户新输入。标准流程不得依赖第二次可选调用。只有用户明确回复选择“直接生成最终 DOCX”才可调用 `decide` 和 step_8；沉默、超时、时间不足、token/上下文预算、任务即将结束或 agent 自己的判断都不是授权。
9. 路径确认后必须立即通过 `scripts/execution_history.py reserve-run` 原子创建运行编号和全部步骤目录；每次尝试都占用 `run_id`，包括审计失败、低分熔断和用户停止。step_2.5 使用 `bind-plan` 绑定完整计划。同一计划或案例重复执行时创建新的 `run_id`，不得覆盖既有记录。
10. 最终复现报告只允许生成 `<repo_name>_final_reproduce_report.docx`，但最终产物集必须同时包含该 DOCX 与 step_1 真实生成的 `<repo_name>_Audit_Report.md`。审计报告是独立的强制交付物，不是 DOCX 的降级替代；禁止创建任何 `*_final_reproduce_report.md`、`*_final_report.md` 或其他 Markdown 最终报告。
11. 开始执行时，以及每完成一个 step 时，输出一张更新后的**完整 11 行 Markdown 看板**。
12. CCI 不增加看板行。所有付费实例必须来源于用户对最新预检结果的明确确认；沉默、过期确认和自动选择第一项均无效。恢复流程先查活跃资源，存在未释放资源或 `cleanup_required` 时禁止新建。

---

## step_precheck_public_repro — 公共复现仓库预检

从 `github_url` 提取 `repo_name`。`public_repro_repo_url` 为空或类型为 `unset` 时输出“公共复现仓库未配置，跳过历史复现预检”，继续后端选择。配置后按 `git`、`http_index` 或 `filesystem` 进行只读同名搜索，不做论文分析、环境探测、克隆或安装。

完整历史复现必须包含 `*_final_reproduce_report.docx`、`*_Audit_Report.md`、`paper_execution_options.md/json`、论文输入存在时的 `paper_report.md`，以及至少一个 `repro_run.log`、`env_patches.md`、结果目录或等价运行日志。产物不齐时列出缺失项并继续；产物齐全时展示路径和版本信息，等待用户选择：A 直接使用并终止新复现，B 继续重新复现。

---

## step_0 — 已选后端的算力探测与环境自检

进入 step_0 前必须已完成后端选择。

本地后端：

```bash
python <SKILL_DIR(ar24-instance-manage)>/scripts/probe.py --backend local
```

SSH 后端：

```bash
python <SKILL_DIR(ar24-instance-manage)>/scripts/probe.py --backend ssh --ssh "ssh -p 40072 user@host"
```

缺少 SSH 连接信息时返回 `pause_reason=missing_ssh_connection`，agent 必须暂停并请求用户输入形如 `ssh -p 40072 user@host` 的连接命令。普通 SSH 认证失败且需要密码时返回 `pause_reason=missing_ssh_password`，随后通过 `REPRO_SSH_PASSWORD` 或 `--ssh-password` 从 step_0 继续：

```bash
REPRO_SSH_PASSWORD='<password>' python <SKILL_DIR(ar24-instance-manage)>/scripts/probe.py --backend ssh --ssh "ssh -p 40072 user@host"
python <SKILL_DIR(ar24-instance-manage)>/scripts/probe.py --backend ssh --ssh "ssh -p 40072 user@host" --ssh-password '<password>'
```

step_0 输出的 `backend`、`mode`、实测 `cpu_model`、`cpu_cores`、`gpu_count`、`gpus`、`arch_list`、`env_backend`、`checks.memory/compiler/cpp_compiler/cmake/mpi/julia/openfoam/container`、`ssh` 供后续步骤使用。`cpu_cores` 是可见逻辑核容量，不等于阶段实际分配量。缺少单一运行时只限制需要它的候选；连接或探测本身失败导致的 `ready=false` 才全局终止。step_0 不创建环境；环境创建只能发生在 step_4。

CCI 后端先执行只读预检，不创建资源：

```bash
python <SKILL_DIR(ar24-instance-manage)>/scripts/cci_manager.py \
  preflight --session-id <session_id>
```

只从 `ALAYANEW_ACCESS_KEY`、`ALAYANEW_SECRET_KEY`、可选 `ALAYANEW_ACCESS_TOKEN` 和 `ALAYANEW_BASE_URL` 读取凭证；不得把凭证放入参数或持久化文件。预检必须展示 CPU/GPU 候选、原始价格、库存、镜像和已有 NAS；无 NAS 时暂停且不得创建。此时资源证据固定标记为 `cci_catalog_planned`，不得冒充实测。

用户明确选择构建规格、执行规格、同一镜像、同一 AIDC、已有 NAS 和该 NAS 上的项目专用精确目录后，保存确认：

```bash
python <SKILL_DIR(ar24-instance-manage)>/scripts/cci_manager.py \
  --state <cci_state.json> confirm-selection \
  --repo-name <repo_name> \
  --base-root <confirmed_exact_nas_root> \
  --build-product <cpu_product_code> \
  --execution-product <gpu_product_code> \
  --image-key <image_selection_key> \
  --nas-id <nas_id> \
  --confirmation-id <confirmation_id> \
  --user-response "<用户原始明确回复>"
```

确认前不得创建付费资源；选择项必须来自最新预检且未过期。CCI 平台自动停止与自动释放固定关闭，实例从创建成功起持续计费，直到本流程主动释放成功。

---

## step_0.5 — 精确根路径确认与运行预留

local/SSH 调用 `path_policy.py --base-root <exact_root> --repo-name <repo_name>` 校验用户输入的项目专用目录；CCI 使用资源确认中的 NAS 精确目录。检测到旧 `ar24/`、旧版顶层 `step_*`、不兼容路径清单、项目名不匹配或其他外来内容时必须停止，禁止移动、删除或覆盖。合法的共享目录、根台账和完整 `run-NNN/` 可继续使用。

路径确认后立即初始化根台账并 `reserve-run`，导出上文包括 `RUN_ID` 在内的全部变量。step_0 在确认前的探测结果先暂存，再写入 `$RUN_ROOT/step_0/`。CCI 付费实例创建前在 `local_staging_root` 建立相同 v3 布局；构建实例真实探测通过后，把根文件、共享目录和运行目录同步到已确认的 NAS 精确根，再切换环境变量。该步骤是强制暂停点，但不单独占用看板节点。

```bash
python <SKILL_DIR(ar24-auto-reproduct)>/scripts/execution_history.py \
  --history "$WORKSPACE_ROOT/execution_run_history.json" \
  init --project-name <repo_name>
python <SKILL_DIR(ar24-auto-reproduct)>/scripts/execution_history.py \
  --history "$WORKSPACE_ROOT/execution_run_history.json" \
  reserve-run --backend <local|ssh|cci>
```

---

## step_1 — 项目复现可行性分析

调用 `ar24-project-analysis --run-id "$RUN_ID"` 审计 GitHub 仓库，强制生成 `$RUN_ROOT/step_1/<repo_name>_Audit_Report.md` 和同目录 `audit_score.json`。审计报告必须使用 UTF-8、非空、可读，至少包含仓库来源、SciML/计算物理 v4 各维度评分与扣分证据、`resource_gate`、`physics_gate`、安全/合规风险、阻断项、硬件迁移说明和最终 verdict；内容与 `audit_score.json` 必须一致。缺失、空文件、不可读或命名不规范都视为 step_1 失败，当前预留运行以早期失败终态结束且保留编号；后续步骤不得删除或覆盖该文件。如提供 `paper_url`，调用 `ar24-paper-analysis` 完整通读论文，并把论文分析、候选计划、结果图规范和科学契约写入同一 `step_1/`。

必须产出 `paper_execution_options.md/json`、`paper_result_figure_specs.json` 和 `scientific_repro_contract.json`。结果图规范只收录曲线、场分布、误差、统计分布、消融等实验结果图，排除方法结构图；每张图必须从图注前后正文、实验设置和附录提取带页码的绘制方法、坐标、单位、尺度、范围、刻度、图例、分面、色条、数据变换和真实数据需求。关键字段缺失时保持 `blocked`，禁止目测猜测。科学契约逐项记录方程、状态变量、单位、无量纲数、几何、网格、初边值条件、物性、求解器、离散格式、时间步/收敛性及验证指标；每项都带 `status/values/evidence`，未披露内容必须显式保留为空，不得推测。候选 JSON 增加 `result_figures`，执行案例增加 `model_source=self_trained|pretrained|not_applicable`；MD 仍只展示原八列表格。候选数量按论文、README 和官方脚本的实际发现结果动态生成；无论文时生成不重复且有实际意义的 skill 默认候选。代码、数据、权重或资源受阻只能写入 `availability`、`blocking_reasons` 和 `unblock_requirements`，不得删除候选。

所有进入流水线的项目统一使用 SciML/计算物理 v4 评分，普通计算机、软件工程和传统机器学习评分档案不得启用。固定包含 `physics_fidelity` 维度；领域关键词识别只用于报告提示，不得改变评分维度。`resource_gate` 只能描述选定后端是否满足候选需求，不能扣减 `overall_score`。审计还必须输出 `physics_gate`：配置缺失写入 `execution_blockers`，验证证据缺失写入 `claim_blockers`。

审计结果必须包含 `resource_gate`：

```json
{
  "fit_status": "fit | risky | insufficient | unknown",
  "action": "proceed | confirm | stop",
  "recommended_resource": "...",
  "message": "..."
}
```

---

## step_2 — 复现可行性熔断判断

先检查 `score`：低于 60 时立即终止，不进入 step_2.5，也不展示候选执行计划；终止回复仍必须列出已生成的 `<repo_name>_Audit_Report.md` 与配套 `audit_score.json` 路径，审计报告不得因熔断被删除或遗漏。不低于 60 时通过。随后应用 `physics_gate`：关键配置缺失只阻止依赖该配置的对应候选，验证失败或无证据则阻止成功结论与等级提升。`resource_gate` 记录当前资源、最低需求、`recommended_resource` 和 `message`，但不得修改可复现性分数，也不得在 60 分通过后过滤候选。

只要 step_1 已开始，后续任何终态（熔断、准备失败、执行失败、用户停止或成功结项）都必须保留并在最终回复中列出 `<repo_name>_Audit_Report.md`。未获得 step_8 授权或未成功生成 DOCX 时，审计报告仍作为必交终态产物单独输出。

---

## step_2.5 — 通读论文后的执行计划选择

读取全部 `paper_execution_options.md/json`，按实际数量使用 A、B、C……连续编号。物理项目按证据动态生成公开权重评估、物理统计评估、分阶段训练、DNS/数值数据生成、耦合求解器模拟和完整链路等候选；默认边界是公开实验/DNS 数据之后的计算链，湿实验与完整 DNS 必须作为独立候选。候选可以少于、等于或多于四项；不得凑数、合并、截断，或因当前 CPU/GPU、显存、依赖、数据、权重条件隐藏候选。

Agent 必须把 `paper_execution_options.md` 中的八列表格原样输出到终端/对话，列为“选项、复现计划、来源 / 类型、可行状态、核心资源需求、预计耗时、限制或阻断原因、推荐 / 可选”，表后只输出“请回复可选计划编号，例如 A。”，不得逐项展开详情。`blocked` 显示为“不可执行 / 否”，`unknown` 显示为“环境待确认 / 是”，`runnable` 显示为“可执行 / 是”；所有候选均须保留。完整论文依据、数据、权重、软件环境、代码支持、成功判据、风险和解锁条件只从 JSON 读取。

所有项均受阻时仍先展示完整表格再终止。用户回复编号后，必须从 JSON 的 `options` 中按 `id` 提取完整候选对象并保存为 `selected_execution_plan`，不能只保存表格摘要。未确认前禁止进入 step_3、创建环境、下载、编译或真实运行。

当前 `run_id` 已在路径确认后预留。用户确认计划后，将完整 `selected_execution_plan` 写入本轮 `step_2_5/` 并用 `bind-plan` 把状态从 `reserved` 变为 `planned`：

```bash
python <SKILL_DIR(ar24-auto-reproduct)>/scripts/execution_history.py \
  --history "$WORKSPACE_ROOT/execution_run_history.json" \
  bind-plan --run-id "$RUN_ID" \
  --plan-json <selected_execution_plan.json> --option-id <A/B/C/...>
```

开始执行前调用 `start-run`。审计失败、step_2 低于 60 分或用户提前停止时，也要以当前阶段和证据结束本轮，不回收编号。继续另一候选时调用 `reserve-run --source-run-id <previous_run_id>` 创建新编号，把仍有效的 `step_precheck` 至 `step_2` 控制面证据快照到新运行，再独立保存本轮 step_2.5 选择。

---

## 多轮计划的增量复用判定

第二轮及以后在进入 step_3 前，为新计划生成 `step_3` 至 `step_6` 的需求指纹：代码使用 commit/tag/submodule/LFS 约束，环境使用运行时与依赖锁定信息，数据和权重使用所需资产集合、来源与校验和。调用：

```bash
python <SKILL_DIR(ar24-auto-reproduct)>/scripts/execution_history.py \
  --history "$WORKSPACE_ROOT/execution_run_history.json" \
  reuse-plan --run-id <run_id> --requirements-json <preparation_requirements.json>
```

只允许复用历史终态轮次中最近一份满足以下条件的步骤证据：状态为 `completed`、需求指纹完全相同，且证据路径/校验和仍有效。最近一轮若为跳过、仅复用或没有该步骤证据，可以继续向更早轮次查找；但任一步仍无法复用时，该步及所有下游准备步骤都重新执行，不得因“看起来相同”跳过。step_7 永远重新执行。代码复用时必须把来源运行的 `step_3/code` 作为 `--reuse-source-code` 传给 checkout runner；runner 创建无硬链接的独立 clone、检出来源 HEAD 并校验 commit，禁止直接在旧运行工作树继续编译。数据、模型和环境按指纹复用共享目录，但新运行仍在自己的步骤目录写完整日志和结构化回执。step_3～step_6 runner 必须接收 `--history` 与 `--run-id` 并原子更新 schema `2.0` 台账；`--standalone` 只允许脱离主流水线时显式使用。任何回执或台账写入失败都必须暂停。

CCI 在取得 `run_id` 后执行双实例映射：

1. 创建 CPU 构建实例并挂载已确认 NAS；创建前刷新规格、镜像和 NAS，结果不明确时进入 `cleanup_required`。
2. `probe --role build` 通过临时 SSH 实测资源与 NAS 精确根的可写性和 v3 兼容性。
3. 通过 `sync --role build --direction push` 把本地 v3 暂存根上传到同一 NAS 精确根；整根同步必须把路径清单与台账中的暂存前缀改写为远端根，同时保留云资源证据中的远端原始路径。上传和完整性校验成功后再切换环境变量。
4. step_3～6 全部使用 `backend=cci --cci-state <cci_state.json>` 在构建实例运行。
5. 无论 step_3～6 成功、失败或用户取消，都先 `release --role build`。释放成功后才允许创建 GPU。
6. 创建 GPU 执行实例并挂载同一 NAS；创建前传入所选计划最低 GPU 数量和显存，随后 `probe --role execution` 实测 GPU、显存、CUDA 与工具链。
7. 实测与已确认规格或计划最低要求不一致时，状态为 `resource_mismatch` 且 `step_7_allowed=false`。必须暂停；用户明确重新确认后才可调用 `confirm-measured`，否则释放并重新选择。

```bash
python <SKILL_DIR(ar24-instance-manage)>/scripts/cci_manager.py --state <cci_state.json> create --role build --run-id <run_id>
python <SKILL_DIR(ar24-instance-manage)>/scripts/cci_manager.py --state <cci_state.json> probe --role build
python <SKILL_DIR(ar24-instance-manage)>/scripts/cci_manager.py --state <cci_state.json> sync --role build --direction push --local-path <local_staging_root> --remote-path <confirmed_exact_nas_root>
# step_3～6
python <SKILL_DIR(ar24-instance-manage)>/scripts/cci_manager.py --state <cci_state.json> release --role build
python <SKILL_DIR(ar24-instance-manage)>/scripts/cci_manager.py --state <cci_state.json> create --role execution --minimum-gpu-count <n> --minimum-vram-gb <gb>
python <SKILL_DIR(ar24-instance-manage)>/scripts/cci_manager.py --state <cci_state.json> probe --role execution
```

---

## step_3 — 选定后端拉取代码

在执行任何拉取命令前记录带时区的 ISO-8601 时间戳 `end_to_end_started_at`（例如 `2026-07-13T15:30:00+08:00`）。该时间戳是本次复现端到端计时的起点，必须保留到 step_7 结束，不得用事后估算值替代。

使用 `ar24-project-checkout` skill 中的 `checkout_runner.py`，只负责目录创建、仓库拉取和目录校验。

本地后端：

```bash
python <SKILL_DIR(ar24-project-checkout)>/checkout_runner.py --backend local --github_url <github_url> --repo_name <repo_name> --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

SSH 后端：

```bash
python <SKILL_DIR(ar24-project-checkout)>/checkout_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --github_url <github_url> --repo_name <repo_name> --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

CCI 构建实例：

```bash
python <SKILL_DIR(ar24-project-checkout)>/checkout_runner.py --backend cci --cci-state <cci_state.json> --github_url <github_url> --repo_name <repo_name> --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

成功产出：`$RUN_ROOT/step_3/code/` 是本轮独立工作树，共享的 `$DATASET_DIR` 和 `$MODEL_DIR` 已就绪，并在本轮 `step_3/` 生成 checkout 证据，准确记录 commit、exact tag、递归子模块证据和 Git LFS 状态/文件证据。

---

## step_4 — 项目分析与依赖准备

step_4 使用 `ar24-dependency-prep` skill，生成统一 `environment_bundle`，可同时包含 Python、Julia、系统求解器、MPI 和容器：

1. 调用 `prep_plan_builder.py --auto-detect`，通过 `ar24-readme-parser` 解析 `README*`、`requirements*.txt`、`pyproject.toml`、`setup.cfg`，写入 `prep_plan.json.readme_parse`。
2. `prep_plan.json` 的主消费契约是 `readme_parse.step_projection`。本步骤只消费 `readme_parse.step_projection.dependency_cmds`，并只记录 step_5、step_6、step_7 的投影，不执行它们。
3. 调用 `dependency_runner.py --prep-plan`。Python 计划创建/复用 uv 或 conda 环境；Julia 计划只允许审计后执行 `Pkg.instantiate()`；系统求解器与容器只校验已有版本，不自动进行管理员安装。

写入计划：

```bash
python <SKILL_DIR(ar24-dependency-prep)>/prep_plan_builder.py --backend local --repo_name <repo_name> --run-id "$RUN_ID" --auto-detect
python <SKILL_DIR(ar24-dependency-prep)>/prep_plan_builder.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --run-id "$RUN_ID" --auto-detect
```

本地依赖准备：

```bash
python <SKILL_DIR(ar24-dependency-prep)>/dependency_runner.py --backend local --repo_name <repo_name> --python_version <v> --mode <step_0.mode> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

SSH 依赖准备：

```bash
python <SKILL_DIR(ar24-dependency-prep)>/dependency_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --python_version <v> --mode <step_0.mode> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

CCI 在构建实例写入计划并准备依赖：

```bash
python <SKILL_DIR(ar24-dependency-prep)>/prep_plan_builder.py --backend cci --cci-state <cci_state.json> --repo_name <repo_name> --run-id "$RUN_ID" <来自审计和README解析的结构化参数>
python <SKILL_DIR(ar24-dependency-prep)>/dependency_runner.py --backend cci --cci-state <cci_state.json> --repo_name <repo_name> --python_version <v> --mode <build_probe.mode> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

危险编译命令如 `pip install -e .`、`python setup.py`、`cmake`、`make`、`ninja` 留给 `readme_parse.step_projection.deferred_step7_cmds`，不得放入 step_4 依赖投影。

若 step_0 输出 `env_backend=uv`，step_4 的依赖安装最终必须统一为 `uv pip install --python "$REPRO_ENVS_ROOT/<repo_name>/bin/python" ...`；不得在 uv 后端下使用裸 `pip`、`python -m pip`、`conda install`、`mamba install` 或 `micromamba install`。

旧的一体化项目准备入口已移除；新流程只使用 `ar24-*` 独立 skill。

---

## step_5 — 数据下载

使用 `ar24-data-download` skill 中的 `data_runner.py`，先激活 step_4 创建的同一个环境，再只读取并执行 `readme_parse.step_projection.data_cmds`，目标目录只能是 `dataset/`。

本地后端：

```bash
python <SKILL_DIR(ar24-data-download)>/data_runner.py --backend local --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

SSH 后端：

```bash
python <SKILL_DIR(ar24-data-download)>/data_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

CCI 构建实例：

```bash
python <SKILL_DIR(ar24-data-download)>/data_runner.py --backend cci --cci-state <cci_state.json> --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

除实验/合成/DNS/LES 数据外，还必须准备网格、坐标、初边值条件、参考工况和求解器 case。受限数据集需要暂停并询问用户是否跳过并用示例数据做 Smoke Test。完成后生成带来源、大小、SHA-256 和物理元数据槽位的 `artifact_manifest.json`。`readme_parse.step_projection.data_cmds` 为空时记录跳过并视为成功。

---

## step_6 — 模型权重下载

使用 `ar24-weight-download` skill 中的 `weight_runner.py`，先激活 step_4 创建的同一个环境，再只读取并执行 `readme_parse.step_projection.weight_cmds`，目标目录只能是 `model/`。下载后必须校验模型目录下无零字节权重文件。

本地后端：

```bash
python <SKILL_DIR(ar24-weight-download)>/weight_runner.py --backend local --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

SSH 后端：

```bash
python <SKILL_DIR(ar24-weight-download)>/weight_runner.py --backend ssh --ssh "ssh -p 40072 user@host" --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

CCI 构建实例：

```bash
python <SKILL_DIR(ar24-weight-download)>/weight_runner.py --backend cci --cci-state <cci_state.json> --repo_name <repo_name> --prep-plan "$RUN_ROOT/step_4/prep_plan.json" --history "$WORKSPACE_ROOT/execution_run_history.json" --run-id "$RUN_ID"
```

除常规权重外，step_6 还处理 BSON/JLD2 参数、normalizer、scaler、坐标尺度和本构参数，并合并更新 `artifact_manifest.json`。HuggingFace 权重默认 `HF_ENDPOINT=https://hf-mirror.com`。`readme_parse.step_projection.weight_cmds` 为空时记录跳过并视为成功。


---

## step_7 — 选定后端编译、推理/微调测试与指标抓取（算力自适应）

激活 step_4 创建的同一个环境，进入 `$RUN_ROOT/step_3/code/`，并显式导出 `RUN_OUTPUT_ROOT=$RUN_ROOT/step_7`、`DATASET_DIR` 和 `MODEL_DIR`。项目命令、验证图和本轮结构化结果只写入本轮步骤目录；下载投影必须引用明确共享变量，禁止依赖 `../dataset` 或 `../model`。`mode=cpu` 时跳过 CUDA 编译；`mode!=cpu` 时使用 step_0 的 `arch_list` 设置 `TORCH_CUDA_ARCH_LIST`，动态编译必要扩展。抓取 Loss、Accuracy、VRAM、每步耗时，与 step_1 Baseline 对比。编译、推理、训练和补充安装命令都必须在该环境内执行，不得裸用 python、pip 执行项目命令。

CCI 的 step_7、验证与结果图复现只在已完成真实探测和资源适配确认的 GPU 执行实例运行，通过 `cci_manager.py exec --role execution` 发送脚本；不得直接使用普通 SSH 密码命令。步骤日志、指标、图片和台账仍写入同一 NAS。`exec` 会在未探测或资源差异未确认时拒绝执行。

进入本步骤时记录 `step_7_started_at`，结束时记录 `step_7_ended_at` 和 `end_to_end_ended_at`；全部使用带时区的 ISO-8601 时间戳。编译、推理、训练/微调等每个实际执行阶段都必须记录以下台账：

```json
{
  "name": "<compile | inference | training | fine_tuning | evaluation>",
  "status": "<completed | partial | failed>",
  "started_at": "<ISO-8601 时间>",
  "ended_at": "<ISO-8601 时间>",
  "duration_hours": 1.0,
  "resource_type": "gpu | cpu",
  "device_model": "<来自 step_0 的实测设备型号>",
  "device_count": 2,
  "device_count_evidence": "<CPU 必填：命令、配置或环境变量；GPU 可为 null>",
  "device_hours": 2.0
}
```

GPU `device_count` 是该阶段实际使用的正整数卡数，不得照抄 step_0 的可用设备总数，`device_model` 取自 step_0 `gpus[].name`。CPU `device_model` 取自 step_0 实测 `cpu_model`；CPU `device_count` 表示命令、配置或环境变量中显式分配的逻辑核，并把原始依据写入 `device_count_evidence`。无法提供分配证据时，不得计算 CPU-hours，且不得用 `cpu_cores` 总容量猜测。按 `(ended_at - started_at) / 3600` 计算 `duration_hours`；证据充分时再计算 `device_hours = duration_hours × device_count`。跳过的阶段不得伪造耗时。

必须跟踪阶段完整性：无效、失败、部分完成或被拒绝的实际阶段都要保留状态/校验说明，不能从台账中静默删除后只累计其余阶段。存在这类阶段，或任一 CPU 阶段缺少分配证据时，step_8 必须阻断设备小时比例和节省结论；如果论文 provenance、双方范围和墙钟时间各自完整，墙钟比较可独立保留。

step_7 结束时令 `end_to_end_ended_at = step_7_ended_at`，并按时间戳差值换算为小时：

- `step_7_hours = (step_7_ended_at - step_7_started_at) / 3600`，表示本步骤墙钟时间；
- `end_to_end_hours = (end_to_end_ended_at - end_to_end_started_at) / 3600`，表示 step_3 开始至 step_7 结束的端到端墙钟时间。

### step_7 强制结果验证

执行构建、训练、推理或数值模拟后，必须调用 `ar24-physics-validation`，严格完成以下固定子阶段；任何阶段都不能用绘图图片替代原始数值数据：

1. `validation_preflight`：核对变量、单位、坐标、网格、时间索引、边界、归一化和参考工况。Reynolds 数、网格、时间步、边界条件或 ensemble 数量不同必须阻断直接比较；仅当显式允许插值且记录方法与插值误差时才允许网格对齐。
2. `reference_acquisition`：读取公开参考，或运行 OpenFOAM/DNS/解析模型；保存版本、case、网格、配置、日志和校验和。
3. `prediction_acquisition`：在相同工况和采样点运行模型并保存原始预测。
4. `field_alignment`：统一变量顺序、维度、坐标、单位和归一化。
5. `quantitative_validation`：确定性任务计算 L1/L2/RMSE/相对误差/最大误差、剖面与时间演化；随机任务计算 ensemble 均值/RMS、PDF、雷诺应力、TKE 谱、两点相关、置信区间和统计收敛；条件生成增加配对 MSE、恢复区域误差和覆盖率。
6. `physics_validation`：只检查实际适用的边界条件、守恒、张量对称性、参考系不变性、无量纲参数、稳定性和收敛性。
7. `validation_artifacts`：向 `$RUN_OUTPUT_ROOT` 输出 `validation_result.json` 及 Ground Truth/Prediction/Error 三联图、切线图、误差演化图和适用统计图；每个指标记录参考值、实测值、容差、状态、数据路径和证据来源。
8. `claim_gate`：进程成功至多为 `software_runnable`；权威容差下数值通过才是 `numerically_consistent`；适用物理/统计检查通过才是 `physics_validated`；执行范围与论文一致才是 `paper_scope_reproduced`。失败结果必须保留，禁止提升等级。

完成坐标、单位和原始数值对齐后，在同一个 step_4 环境内调用 `ar24-result-figure-reproduction`。运行器读取 `paper_result_figure_specs.json`，优先调用已审计 `code/` 内的官方绘图脚本，否则使用声明式 renderer；禁止 `eval`、shell 字符串和从论文截图反推数据。输出固定写入 `$RUN_OUTPUT_ROOT/figures/`，生成 `figure_reproduction_result.json`，并把成功图片以 `role=paper_result_figure` 合并到资产清单、实验详情和验证产物。无目标图时显式写 `skipped_not_applicable`；目标图存在但缺少坐标、单位、数据绑定、论文方法证据或输出校验和时只阻断对应图片，其他图片继续，但该轮最高只能是 `partial`。SSH 模式在远端生成并在报告前同步图片、规范与结果 JSON。

容差只能来自论文、补充材料、官方代码或用户确认。未披露容差时只允许 smoke/定性判据。

多案例计划必须先为每个真实 case 生成独立验证结果，再调用 `validate_results.py --aggregate-cases <cases.json> --selected-plan <selected_execution_plan.json> --output-dir "$RUN_OUTPUT_ROOT"` 生成统一的 `$RUN_ROOT/step_7/validation_result.json`。聚合结果必须列出七个验证阶段、每个 case 的结果路径、定量指标、物理检查、验证产物和总体 `claim_gate`；总体可信等级取所有已执行 case 的最低等级。只保存计划名（如 `E`）而没有 `execution_items` case 明细，或案例结果无法逐项匹配时，禁止标记 `passed`。

---

## step_7.5 — 报告前确认与多轮执行决策

这是强制暂停点，但不增加看板节点。任一轮次实际开始后，无论最终为 `passed`、`partial`、`failed` 或 `skipped`，都必须先把结果写入台账；step_3 至 step_6 失败时仍需将该轮标记为 `failed`，但不得绕过“禁止进入 step_7”的纪律。

每轮结束后把逐案例结果、实验详情、算力台账和本轮证据路径写入：

```bash
python <SKILL_DIR(ar24-auto-reproduct)>/scripts/execution_history.py \
  --history "$WORKSPACE_ROOT/execution_run_history.json" \
  finish-run --run-id <run_id> --status <passed|partial|failed|skipped> \
  --actual-test-outcomes <actual_test_outcomes.json> \
  --experiment-details <experiment_details.json> \
  --compute-usage <compute_usage.json> \
  --scientific-repro-contract <scientific_repro_contract.json> \
  --artifact-manifest <artifact_manifest.json> \
  --validation-result "$RUN_ROOT/step_7/validation_result.json" \
  --figure-reproduction-result "$RUN_ROOT/step_7/figures/figure_reproduction_result.json" \
  --terminal-summary <terminal_summary.json> \
  --cloud-resources <cloud_resources.json>
```

`--cloud-resources` 对 local/SSH 是可选字段；CCI 必须先调用 `cci_manager.py evidence` 生成脱敏证据，其中包含构建/执行/恢复实例角色、实例 ID、原始价格、起止时间、释放状态、同步路径和 `cleanup_required`。不得把完整 API 响应、Authorization 或临时 SSH 信息写入台账。

`terminal_summary.json` 必须包含真实 `stage`、`reason` 和非空 `evidence`。`passed` 要求 step_3～6 均为有效完成/复用/明确不需要，计划 case 与逐案例结果完全匹配，实验详情、算力台账、科学契约、资产清单、七阶段汇总验证和计划声明的全部结果图均完整；目标图任一非 `passed` 时本轮不得写 `passed`。`partial` 必须进入 step_7 且至少保留一个实际 case；`failed` 必须写明失败步骤，并把后续准备步骤记为 `not_reached`；`skipped` 必须有跳过原因。`finish-run` 在同一次原子写入中校验这些条件、把报告门置为 `awaiting_user`，生成新的 `confirmation_id`，记录 `prompt_displayed_at`，并返回 `board`、`pause_required=true`、`next_action=await_user_report_decision`、`prompt`、`confirmation_id` 和停止指令。它不会授权报告。

把命令返回的 `board`、`prompt` 和 `confirmation_id` 原样展示给用户；看板中的 step_8 必须为 `⏸等待用户确认`，不得显示“正在执行 step_8”。随后**立即结束当前回复**。不得在同一回复中替用户选择、调用 `decide`、进入 step_8 或创建任何最终报告。三项选择固定为：

CCI 在此暂停期间保留 GPU 执行实例，不设置平台自动释放。看板核心产出必须同时显示实例 ID、原始单价、仍在计费和人工清理提示。每次恢复先运行 `status`；发现活跃或 `cleanup_required` 资源时先恢复/清理，不得重复申请。

1. **直接生成最终 DOCX**：收到用户明确的新回复 `1` 或等价的明确 DOCX 指令后，调用 `decide --decision generate_report --confirmation-id <id> --user-response "<用户原始回复>"`；通过 `validate --for-report` 后进入 step_8。
2. **继续其他已有候选计划**：收到用户明确的新回复 `2` 后，调用 `decide --decision continue_existing --confirmation-id <id> --user-response "<用户原始回复>"`。CCI 必须先释放当前 GPU；释放成功后立即 `reserve-run --source-run-id <previous_run_id>` 创建新运行并快照有效控制面证据，再返回 step_2.5 绑定本轮选择，然后按增量复用判定进入必要的 step_3 至 step_7。
3. **输入其他要求**：用户必须回复 `3` 并写明要求。先判断为执行类还是报告类，并向用户复述判断结果。执行类要求调用 `decide --decision custom_execution --confirmation-id <id> --user-response "<用户原始回复>" --request "<用户原始要求>"`，再转换为带 `source=user_custom` 的 `CUSTOM-001`、`CUSTOM-002`……候选，重新执行审计、可行状态判定和用户确认后才能创建新轮次；报告类要求调用 `decide --decision report_requirement --confirmation-id <id> --user-response "<用户原始回复>" --request "<用户原始要求>"` 保存。该调用会原子生成新的 `awaiting_user` 门、提示和确认编号，必须将返回内容原样展示并再次暂停，不能直接生成报告。

`request-decision` 仅作为弃用的兼容入口保留：它只能把旧台账的 `awaiting_prompt` 恢复为 `awaiting_user`，或重新显示已经存在的提示；不能从 `not_ready` 创建确认门，也不能绕过 `finish-run` 或授权报告。`decide` 必须校验一次性确认编号、提示已展示状态和用户原始回复与决策的一致性。没有用户新回复、回复含糊、用户沉默、交互超时、运行时间不足、token/上下文预算不足、任务被压缩或 agent 想尽快结束时，一律保持 `awaiting_user` 并停止；禁止自行选择 1，禁止把“先给一个 Markdown”视为替代方案。

同一候选允许重复执行，但每次必须创建新 `run_id`。已选择但从未开始的 `planned` 轮次不进入报告；任何终态轮次都不得删除或被后续轮次覆盖。存在无法解释的 `running` 记录、台账损坏或无终态轮次时，必须暂停并让用户选择恢复执行或将该轮标记为失败。

---

## step_8 — 生成 Word 复现报告

进入 step_8 前必须执行：

```bash
python <SKILL_DIR(ar24-auto-reproduct)>/scripts/execution_history.py \
  --history "$WORKSPACE_ROOT/execution_run_history.json" \
  validate --for-report
```

`validate --for-report` 只接受 schema `2.0`、layout `3`，并对所有终态轮次执行与 `finish-run` 相同的条件化严格校验，错误必须精确到字段路径。它根据获得 DOCX 授权的 `run_id` 定位该轮 `step_1/<repo_name>_Audit_Report.md` 与同目录 `audit_score.json`，确认报告非空、UTF-8 可读，评分为有效 `scoring_profile=sciml_physics` 对象并包含 `overall_score`、`verdict`、`dimensions`、`resource_gate`、`physics_gate`。审计维度低于满分却无扣分证据同样阻断。任一条件不满足都必须停止，禁止在 step_8 临时编造证据。

旧 schema 或 layout 台账默认阻断，不自动迁移或覆盖。只有同一 v3 布局内的可修复台账且用户提供真实步骤回执、逐案例结果、实验详情、算力、科学契约、资产清单、验证结果或终态摘要时，才可调用 `repair-run --reason "<补录原因>"` 显式补录；不得推测任何缺失值。修复完整后生成新的 step_7.5 一次性确认门，旧报告授权作废，必须再次等待用户明确选择 DOCX。

step_8 的唯一最终复现报告输出是获授权运行的 `$RUN_ROOT/step_8/<repo_name>_final_reproduce_report.docx`；最终产物集固定为该 DOCX 加同轮 `step_1/<repo_name>_Audit_Report.md`。DOCX 必须在固定附录中嵌入审计报告正文：报告采用封面、目录、十章正文、附录 A 全部实验产出图片、附录 B 输出文件索引、附录 C 完整项目审计报告的固定模板化结构。正文依次为执行摘要、复现环境与算力配置、论文模型与实验概述、代码审计与可行性评估、依赖环境准备、数据与模型权重、执行详情与产物、代码缺陷与排坑自愈指南、论文复现结论、后续建议。第一章必须保留执行状态摘要；第二章合并算力统计和 CCI 资源证据；第九章合并核心验证与自训练模型/预训练模型对比，且只有在同案例、同数据集、同评估协议和同指标下才计算差值，否则明确标记不可比较。审计报告正文完整写入附录 C。全文中文统一使用宋体（`SimSun`），英文、数字及拉丁字符统一使用 Times New Roman；调用方不得覆盖该字体组合。即使 DOCX 依赖缺失、生成失败、时间不足或即将超时，也必须返回错误或暂停等待修复，绝对禁止自行生成 Markdown、纯文本或其他格式的最终复现报告来替代 DOCX。`backend=ssh` 或 `backend=cci` 时必须分别把准确的 DOCX 与审计报告路径同步回本地，任一文件未同步都不得结项。

动态参数表、数据流表、普通结果表和可选环境配置表采用统一的稀疏省略规则：表头不参与统计，键值表只统计值列，数据流表不统计阶段标签列；空值、空集合和通用“未提供/未记录/N/A/unknown”等占位符计为缺失，具有业务含义的“论文未披露”“本步骤不需要”“未进入 step_X”“不可比较”“文件未同步”等状态不计为缺失。缺失率严格超过 50% 或无数据时，保留小节标题并显示“有效信息不足，已省略该表格（缺失 X/Y，比例 Z%）”，但不得创建空表、题注或消耗表格编号；恰好 50% 仍须生成。执行状态、审计评分、步骤证据、CCI 资源、产物清单和附录 B 输出索引属于关键证据表，不得因稀疏自动隐藏，必填证据缺失继续由现有门禁阻断。

CCI 报告优先在仍存活的 GPU 执行实例生成。若报告前 GPU 意外消失，则只允许创建用户已确认的 CPU 构建规格作为 `recovery` 实例，挂载同一 NAS，真实探测后生成和同步报告，完成后立即释放。DOCX、审计报告、台账、日志、指标和图片用 `sync --direction pull` 同步回本地；校验两项必交产物均存在、非空且所需证据完整后，才释放 GPU 或恢复实例。同步失败不得释放，必须保留状态并提示仍在计费；释放失败进入 `cleanup_required`。

`scope_detail` 优先使用 v2 分类结构：`training`、`inference`、`numerical_simulation`、`experimental_fitting`、`coupled_simulation`；每个实际执行类别记录工况、数据规模、步数/时间窗、网格、求解器和评估协议。读取器继续兼容下方现有 ML v1 平面结构。

先将 step_1 的论文证据和 step_3 至 step_7 的实测时间台账组装为 `ar24-repro-report` 接受的 `compute_usage`：

```json
{
  "paper": {
    "disclosed": true,
    "scope": "<论文实验范围>",
    "scope_detail": {
      "dataset": "<数据集>",
      "data_scale": "<数据规模>",
      "epochs": "<轮数；真正不适用时 not_applicable>",
      "steps": "<步数；真正不适用时 not_applicable>",
      "sample_count": "<样本数；真正不适用时 not_applicable>",
      "checkpoint": "<检查点；真正不适用时 not_applicable>",
      "evaluation_protocol": "<评估协议；真正不适用时 not_applicable>"
    },
    "wall_clock_hours": 24.0,
    "resource_type": "gpu | cpu",
    "device_model": "<论文披露型号>",
    "device_count": 8,
    "device_hours": 192.0,
    "evidence": "<论文原文证据>",
    "source_page": "<页码>"
  },
  "reproduction": {
    "scope": "<本次实际执行范围>",
    "scope_detail": {
      "dataset": "<实际数据集>",
      "data_scale": "<实际数据规模>",
      "epochs": "<实际轮数；真正不适用时 not_applicable>",
      "steps": "<实际步数；真正不适用时 not_applicable>",
      "sample_count": "<实际样本数；真正不适用时 not_applicable>",
      "checkpoint": "<实际检查点；真正不适用时 not_applicable>",
      "evaluation_protocol": "<实际评估协议；真正不适用时 not_applicable>"
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
        "device_count_evidence": "<CPU 逻辑核分配依据；GPU 可为 null>",
        "device_hours": 2.0
      }
    ]
  },
  "comparison": {
    "comparable": false,
    "reason": "<可比性结论>"
  }
}
```

必须显式比较 `paper.scope` 与 `reproduction.scope`，不得把目标范围当作实际范围。双方必须使用相同的 `scope_detail` schema：ML v1 要求七字段完整，v2 要求相同的适用类别及类别内字段完整；无论哪种 schema，每个规范字段都必须非空。真正不适用时显式写 `not_applicable`，缺失、`null`、空字符串、`unknown` 或 `undisclosed` 均阻断比较。训练类别的 `epochs 或 steps` 至少一项必须为实质值。

任何比例都要求论文提供非空 `evidence` 与非空 `source_page`。设备小时比例还要求论文正整数 `device_count`，即使存在显式 `device_hours` 也不例外；若论文墙钟时间、设备数量和显式 `device_hours` 同时存在，必须校验显式值等于 `墙钟时间 × 设备数量`，不一致时拒绝该总数并写入校验说明。旧版自由文本、范围细节不完整/不一致、`paper.disclosed=false` 或 provenance 不足时，必须令 `comparison.comparable=false`，`reason` 以 `不可直接比较` 开头，并禁止所有比例与节省结论。阶段完整性不足仅阻断设备小时比例和节省结论；其他证据独立完整时，墙钟比较可独立保留。

必须将 `execution_run_history.json` 作为 `execution_run_history` 传给 `ar24-repro-report`。报告第三、第四、第五、第六和第七部分只能按台账的终态轮次及其计划内顺序生成；不得使用外部 `selected_execution_plan` 覆盖历史，也不得引入从未执行的候选。缺失、为空或无法验证台账时必须停止，旧的无台账单轮调用不再允许生成最终 DOCX。

每轮按实验名组装 `actual_test_outcomes` 与可选 `experiment_details`。`experiment_details` 每项包含 `case_name`、`status` 和动态 `sections`，section 类型可为 `text`、`list`、`key_value`、`flow`、`table` 或 `artifacts`。报告生成器还会从该轮科学契约、`compute_usage.scope_detail`、资产清单与验证结果自动补充工况、参数、数据流、指标和产物。同名实验以 `(run_id, case_name)` 区分；未标注证据只能进入所属轮次的“公共/未归属证据”，禁止跨轮复制。

整篇正文跨所有实际终态轮次最多精选 8 张有效实验图片，可以少于 8 张但不得超过；`paper_result_figure` 优先进入正文，再从验证图和其他重点产物补足，表格不占图片额度。SVG 与常见位图直接嵌入，CSV/TSV/JSON 最多预览 20 行、10 列。附录 A 按路径或 SHA-256 去重后嵌入全部有效实验图片，正文精选图片仍重复收录，并为每项保留全部关联 `run_id`；缺失、损坏或远程未同步图片必须在附录 A 列出路径和失败原因。跨轮完整产物清单继续记录来源、大小、SHA-256 和状态。第二章分别渲染每轮算力可比性，并仅把累计值作为会话运行成本；不同实验范围不得合并计算论文效率比例。`backend=ssh` 或 `backend=cci` 时最终报告必须同步回本地工作区，并给出本地路径和远端原始路径；需要内嵌的产物必须在生成报告的后端可访问，远端与本地路径同时存在时分别记录。CCI 的 `cloud_resources` 还应记录构建/执行/恢复实例 ID、角色、起止时间、原始价格与单位、远端/本地产物路径，以及仅在价格单位和时长可换算时计算的实际费用。

---

## 实时进度看板模板（每步追加播报）

```markdown
#### 📊 复现流水线实时看板: <项目名>

| 序号 | 步骤 | 状态 | 核心产出 |
| :-- | :-- | :-- | :-- |
| P | step_precheck 公共复现仓库预检 | [⏳/⏸/✅/❌] | [未配置/未命中/产物缺失/直接复用] |
| 0 | step_0 后端选择+算力探测+自检 | [⏳/⏸/✅/❌] | [backend / mode / GPU / uv或conda·nvcc·磁盘] |
| 1 | step_1 双重审计 | [⏳/✅/❌] | [评分 / Baseline / 超参] |
| 2 | step_2 可行性熔断 | [⏳/✅/❌] | [通过 / 熔断原因] |
| 2.5 | step_2.5 执行计划选择 | [⏳/⏸/✅/❌] | [动态候选 / 阻断原因 / 用户选择] |
| 3 | step_3 拉取代码 | [⏳/✅/❌] | [clone / code 路径] |
| 4 | step_4 项目分析+依赖准备 | [⏳/✅/❌] | [Python 版本 / 依赖结果 / prep_plan.json] |
| 5 | step_5 数据下载 | [⏳/⏸/✅/❌] | [dataset 路径 / 受限数据状态] |
| 6 | step_6 模型权重下载 | [⏳/✅/❌] | [model 路径 / 权重校验] |
| 7 | step_7 编译+推理 | [⏳/✅/❌] | [编译 / 指标 / VRAM / step_7_hours / device_hours] |
| 8 | step_8 生成报告 | [⏳/⏸/✅/❌] | [等待用户确认 / Word 路径 / compute_usage 可比性状态] |

---
```

每条回复消息最多放一张完整看板；看板前后留空行，末尾保留 `---` 分隔线。
