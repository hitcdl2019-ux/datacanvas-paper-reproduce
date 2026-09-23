---
name: ar24-paper-analysis
description: 全自动多领域论文解析工具。当用户要求『分析论文』『解析 paper』『读论文』『论文解析』『提取论文指标』『论文 baseline』或英文 paper analysis / parse paper 时使用；也用于论文复现流程中提取 Baseline 与超参数的环节。接收本地 PDF 路径、GitHub URL 或 arXiv 链接，利用 PyMuPDF 将全文无损提取为文本。随后，请调用本技能的 Agent 结合自身原生大模型，对提取出的文本开展学术领域分类、复现点提炼及超参总结，生成标准报告。
---

# Paper Analysis Skill v2.0 (结构化提取 + Agent 语义协同)

全自动、多领域的论文文本提取与智能分析工具。v2.0 核心升级：**脚本层做结构化提取，Agent 层做语义推理**，分工明确、各司其职。

---

## 核心能力

- **多源数据读取与无损提取**：
  - **本地 PDF 导入（最稳妥）**：直接解析本地磁盘上的 `.pdf` 论文。
  - **自动网络下载**：从 arXiv 链接、标准 PDF 链接直接下载。
  - **GitHub 间接解析**：自动从 README 提取论文链接并下载。
  - **PyMuPDF 支撑**：快速提取超长论文文本，并安全归档至本地。
- **结构化提取**（脚本层自动完成）：
  - PDF 元数据 (标题/作者/DOI/页数)
  - 章节智能分段 (Abstract→Introduction→Methods→Results→Conclusion)
  - 公式/表格页面高清图像导出 (200 DPI)
  - 超参模式匹配 (lr/batch_size/epochs/optimizer/weight_decay/dropout/warmup/grad_clip/mixed_precision/latent_dim/diffusion_steps 等)
  - 评估指标模式匹配 (PSNR/SSIM/LPIPS/FID/BLEU/ROUGE/雷诺应力/能量谱/TKE 等)
  - Baseline 数值对比表格自动定位
  - 实验结果图候选检测与裁剪，排除明显的方法/结构/流程图
  - 领域关键词检测 + 置信度评分
- **智能多领域自适应 (Domain Adaptation)**：
  - **🧬 生物计算**：提炼蛋白质结构预测、配体分子对接等复现点；提取 TM-score、lDDT、RMSD 等指标；关联 PDB 等数据集。
  - **💬 大语言模型**：提炼预训练、SFT、RLHF/DPO 等复现点；提取 PPL、BLEU、ROUGE、MMLU、GSM8K 等指标。
  - **🌀 科学计算与物理 AI (流体/CFD)**：提炼 2D 管道流、3D 槽道流、周期山丘等物理复现案例；提取雷诺应力、湍动能谱、摩擦速度等指标。
  - **🖼️ 计算机视觉**：提炼目标检测、扩散生成、三维重建等复现点；提取 PSNR、SSIM、LPIPS、FID、mAP 等指标。

---

## 核心协同工作流 (Agent Execution Flow)

当用户发出"分析论文"指令时，底层 Agent 必须严格按以下步骤闭环执行：

```text
Step 1 — 运行结构化提取脚本
  python <SKILL_DIR>/scripts/analyze_paper.py \
    --project-name <ProjectName> \
    --paper-path "/path/to/paper.pdf"

  🚫 产物强制落盘: ${RUN_ROOT}/step_1/（RUN_ROOT 来自路径确认后的 reserve-run）
  Agent 必须从论文标题/文件名中推断 ProjectName

  产出物:
  ├── paper_text.txt          # 纯文本全文 (向后兼容)
  ├── paper_meta.json         # 结构化元数据 ⭐ (核心新产物)
  ├── scientific_repro_contract.json # 方程、工况、网格、求解器、验证证据与 physics gate
  ├── paper_sections.json     # 章节分段文本
  ├── paper_result_figure_specs.json # 结果图规范骨架；Agent 补齐后供 step_7 使用
  ├── images/                 # 公式/表格页面图像
  │   ├── page_X_formula.png
  │   └── ...
  ├── paper_report.md         # 论文分析报告
  ├── paper_execution_options.md   # 候选执行计划（给用户选择）
  └── paper_execution_options.json # 候选执行计划（给工作流读取）

Step 2 — Agent 读取结构化元数据 (必须优先读 paper_meta.json)
  2a. 读取 paper_meta.json，获取:
      - 领域自动判定结果 + 置信度
      - 已提取的超参列表 (可直接在报告中引用)
      - 已提取的指标列表 (可直接在报告中引用)
      - 章节分段结构 (用于定位全文结构和交叉核对)
      - 公式页面图像路径 (供 vision model 分析)
      - 同时读取 scientific_repro_contract.json，核对方程、变量、单位、无量纲数、几何、网格、初边值条件、物性、求解器、离散格式、时间步和验证指标；缺失项不得由模型补猜
      - 读取 paper_result_figure_specs.json，只保留实验结果图，逐图核对裁剪图、图注、正文、实验设置和附录

  2b. 通读 paper_text.txt 全文:
      - 必须完整阅读正文、实验设置、训练细节、评估协议、附录、消融实验和补充说明
      - 从全文提取论文原生提出的推理、训练、评估、消融或多阶段复现方式
      - 从 Experiments / Implementation Details / Appendix 提取实验范围、墙钟时间、设备类型/型号/数量，并保留原文 evidence 与 source_page
      - 不得只读取局部章节后直接给出论文复现结论或执行计划

  2c. 对关键公式/表格页调用 vision model:
      - 读取 images/ 下导出的公式密集页面
      - 提取公式中的准确数学表达式和表格数值

  2d. 补全实验结果图复现规范:
      - 排除网络结构、方法流程、系统架构和证据不足的图片
      - 填写关联 case、带页码 reproduction_evidence、真实数据需求、变量、坐标、单位、采样范围和 model_source
      - 填写数据变换、归一化/反归一化、聚合/切片/统计规则及每个面板的图类型、坐标轴、尺度、范围、刻度、图例、颜色和色条
      - 优先选择经审计仓库内的官方绘图脚本，否则使用 generic renderer
      - 关键字段缺失时保持 status=blocked 并记录 blocking_reasons；禁止根据图面猜测

Step 3 — Agent 生成论文报告
  在同目录下输出: paper_report.md
  结构见下方「报告模板」。

Step 4 — Agent 生成候选执行计划
  在同目录下输出:
  - paper_execution_options.md
  - paper_execution_options.json

  候选执行计划必须覆盖两类来源:
  - 论文原生方式: 论文/README/官方脚本中提出的推理、训练、评估、消融或多阶段流程
  - Skill 默认方式: smoke test、轻量化测试、推理验证、完整训练 + 测试流程
  - 每个候选增加 `result_figures`，引用需要复现的论文结果图；每个执行案例增加 `model_source=self_trained|pretrained|not_applicable`

  候选数量必须由实际发现结果决定:
  - 完整列出论文、README 和官方脚本明确提出的有效复现方式，不得合并、截断或固定凑成四项
  - 论文原生方式之外，只追加有实际意义且不重复的 Skill 默认方式
  - 不得因算力、依赖、数据、权重或代码缺失而删除候选项；必须保留并标注阻断原因和恢复条件
  - 选择前尚未创建复现实例，无法静态确认的环境条件必须标记为 `unknown`，不得伪造实测结果
```

---

## 输入参数说明

| 参数名                | 类型   | 必填   | 说明                                                      |
| --------------------- | ------ | ------ | --------------------------------------------------------- |
| `--project-name`      | string | **是** | 项目名，仅用于论文元数据；产物输出到 `${RUN_ROOT}/step_1/` |
| `paper_path`          | string | 否     | **本地 PDF 文件的绝对路径（优先级最高）**                 |
| `paper_url`           | string | 否     | arXiv 论文 PDF 链接或标准的网络 PDF 链接                  |
| `github_url`          | string | 否     | 论文配套的 GitHub 仓库链接                                |
| `--no-images`         | flag   | 否     | 跳过公式图像导出（加速）                                  |
| `--max-formula-pages` | int    | 否     | 最大导出公式页数 (默认 8)                                 |

> 📌 **输入解析优先级**：`paper_path` > `paper_url` > `github_url`
>
> 🚫 **硬约束**: 流水线中的所有论文产物必须落在当前预留运行的 `${RUN_ROOT}/step_1/`。`RUN_ROOT` 或 `RUN_ID` 缺失时必须停止，不允许脚本猜测输出路径；不得写入项目包装目录或其他运行。

---

## 使用方法

### 方式一：Agent Skill 自动触发
当用户说"分析论文"、"解析 paper"、"提取论文指标"等，Agent 自动匹配激活本 Skill。

### 方式二：终端命令行手动调用
```bash
python <SKILL_DIR>/scripts/analyze_paper.py \
  --project-name CoNFiLD \
  --paper-path "/path/to/paper.pdf"
# 产物落盘: ${REPRO_OUTPUT_ROOT}/CoNFiLD/
```

---

## Agent 输出报告模板 (必须遵循)

Agent 在阅读 `paper_meta.json`、完整通读 `paper_text.txt` 并核对公式图像后，必须在论文输出目录下生成如下结构的 `paper_report.md`：

```markdown
# <论文标题> 论文分析报告

> 解析引擎: ar24-paper-analysis v2.0 | 分析时间: YYYY-MM-DD

## 一、基本信息

| 字段          | 值                                                  |
| ------------- | --------------------------------------------------- |
| 学术领域      | **[从 paper_meta.json domain.primary_domain 获取]** |
| 置信度        | **XX%**                                             |
| DOI           | [从 paper_meta.json 获取]                           |
| arXiv ID      | [从 paper_meta.json 获取]                           |
| 发表期刊/会议 | [Agent 从文本推断]                                  |
| 论文页数      | XX 页                                               |

## 二、研究背景与创新点

[Agent 阅读 Abstract + Introduction 章节后提炼，3-5 个要点]

## 三、核心复现点与验证指标

### 3.1 核心复现点 (Reproduction Tasks)

- [x] Case 1: **[任务名]** — [简要描述 + 预期输入输出]
- [x] Case 2: **[任务名]** — [简要描述 + 预期输入输出]
- ...

### 3.2 复现验证/对比评估指标

| 指标                              | 论文报告值 | 说明             |
| --------------------------------- | ---------- | ---------------- |
| [从 paper_meta.json metrics 获取] | [值]       | [Agent 补充说明] |
| ...                               | ...        | ...              |

## 四、实验设置与基准数据集

[Agent 阅读 Experiments/Setup 章节后提炼]

### 数据集
| 数据集 | 规模 | 用途 | 下载方式 |
| ------ | ---- | ---- | -------- |
| ...    | ...  | ...  | ...      |

### 硬件与框架
| 项目         | 配置 |
| ------------ | ---- |
| GPU          | ...  |
| 深度学习框架 | ...  |
| 关键依赖     | ...  |

## 五、Baseline 基准对照数值表

[Agent 从 paper_meta.json baseline_table_regions + 原文 Experiments 章节提取]

| 方法           | 指标1   | 指标2   | ...     |
| -------------- | ------- | ------- | ------- |
| Method A       | ...     | ...     | ...     |
| Method B       | ...     | ...     | ...     |
| **本论文方法** | **...** | **...** | **...** |

## 六、训练超参数

| 参数                                  | 值   | 来源       |
| ------------------------------------- | ---- | ---------- |
| [从 paper_meta.json hyperparams 获取] | [值] | paper_meta |
| ...                                   | ...  | Agent 补充 |

## 七、算力与训练时长

将论文原文证据整理为可直接写入 `compute_usage.paper` 的对象：

```json
{
  "disclosed": true,
  "scope": "<full_training | fine_tuning | inference | evaluation>",
  "scope_detail": {
    "dataset": "<数据集名称>",
    "data_scale": "<数据规模>",
    "epochs": "<轮数；真正不适用时 not_applicable>",
    "steps": "<步数；真正不适用时 not_applicable>",
    "sample_count": "<样本数；真正不适用时 not_applicable>",
    "checkpoint": "<检查点/权重标识；真正不适用时 not_applicable>",
    "evaluation_protocol": "<评估协议；真正不适用时 not_applicable>"
  },
  "wall_clock_hours": 24.0,
  "resource_type": "gpu | cpu",
  "device_model": "<论文披露的设备型号>",
  "device_count": 8,
  "device_hours": 192.0,
  "evidence": "<支持上述字段的论文原文>",
  "source_page": "<原文页码>"
}
```

`scope_detail` 必须是结构化对象。ML v1 使用 `dataset`、`data_scale`、`epochs`、`steps`、`sample_count`、`checkpoint`、`evaluation_protocol`；SciML v2 使用适用的 `training`、`inference`、`numerical_simulation`、`experimental_fitting`、`coupled_simulation` 类别。无论哪种 schema，每个规范字段都必须出现。真正不适用的字段显式填 `not_applicable`；缺失、`null`、空字符串、`unknown` 或 `undisclosed` 表示证据不完整并阻断直接比较。训练类别的 `epochs 或 steps` 至少一项必须有实质值。

任何墙钟或设备小时比例都要求非空 `evidence` 与非空 `source_page`；两者必须忠实引用论文披露及其所在页。设备小时比较还必须有论文正整数 `device_count`，即使论文给出显式 `device_hours` 也不例外。仅当论文明确披露 `wall_clock_hours` 与 `device_count` 时，才可用二者的乘积填写 `device_hours`。若论文同时披露墙钟时间、设备数量与显式 `device_hours`，必须核对显式总数是否等于 `墙钟时间 × 设备数量`；不一致时拒绝该设备小时总数并记录校验说明。

若论文未披露墙钟时间，必须写 `disclosed=false`。已披露的 `resource_type`、`device_model`、`device_count`、`evidence` 和 `source_page` 必须保留；仅将未知的 `wall_clock_hours` 与 `device_hours` 置为 `null`。禁止推测：不得根据 epoch、step、数据集规模、硬件型号或其他论文的结果估算任何时间或设备小时。

## 八、关键模型架构 (可选)

[如有公式图像，Agent 通过 vision model 分析后描述]

## 九、复现注意事项与潜在难点

[Agent 综合评估：数据集下载难度、算力需求、依赖兼容性、代码开放程度]


## 十、候选执行计划摘要

[按实际数量简要列出 `paper_execution_options.md/json` 中的 A、B、C……候选计划。这里只写摘要，完整字段以 `paper_execution_options.json` 为准。]
```

---

## 候选执行计划产物格式

Agent 必须在论文输出目录下生成 `paper_execution_options.md`，格式如下：

```markdown
## 请选择本次复现方式

| 选项 | 复现计划 | 来源 / 类型 | 可行状态 | 核心资源需求 | 预计耗时 | 限制或阻断原因 | 推荐 / 可选 |
|---|---|---|---|---|---|---|---|
| A | 官方推理验证 | 论文原生 / 推理 | 环境待确认 | 1×GPU，显存≥24GB | 约 1 小时 | CUDA 版本待验证 | 推荐 / 是 |
| B | 完整训练评估 | 论文原生 / 完整流程 | 不可执行 | 8×GPU | 约 72 小时 | 当前 GPU 数量不足 | 可选 / 否 |

请回复可选计划编号，例如 A。
```

候选项按实际数量使用 A、B、C……连续编号。只发现两种时只输出 A、B；发现六种时输出 A 至 F。禁止为了凑数生成重复计划，也禁止将超过四种的计划截断为 A/B/C/D。

表格是 `paper_execution_options.md` 的唯一候选展示形式，不得在表格后逐项展开详情。字段映射规则如下:
- `availability=runnable` → `可执行`、`是否可选=是`
- `availability=blocked` → `不可执行`、`是否可选=否`
- `availability=unknown` → `环境待确认`、`是否可选=是`
- `source=paper_native | skill_default` 分别显示为 `论文原生 | skill 默认`；`type` 使用对应中文类型，并与来源合并到“来源 / 类型”
- `required_resources` 压缩为一句核心资源需求；`known_constraints` 与 `blocking_reasons` 合并到“限制或阻断原因”
- `recommendation=recommended | optional | not_recommended` 分别显示为 `推荐 | 可选 | 不推荐`；多个限制或资源项使用分号分隔，空值显示 `-`
- 每个单元格必须保持单行；将字段内换行压缩为分号，将未转义的 `|` 替换为 `/`，避免破坏 Markdown 表格

MD 只保留上述决策摘要。论文依据、数据、权重、完整软件环境、代码支持、成功判据、风险和解锁条件不得丢失，必须完整写入 JSON。

Agent 必须同时生成 `paper_execution_options.json`，结构固定为：

```json
{
  "options": [
    {
      "id": "A",
      "name": "",
      "source": "paper_native | skill_default",
      "type": "inference | training | evaluation | smoke_test | mini_training | full_training_eval",
      "paper_evidence": "",
      "required_data": [],
      "required_weights": [],
      "required_resources": {
        "cpu": "",
        "memory": "",
        "gpu": "",
        "gpu_memory": "",
        "disk": "",
        "software": []
      },
      "code_support": "supported | partially_supported | missing | unknown",
      "availability": "runnable | blocked | unknown",
      "known_constraints": [],
      "blocking_reasons": [],
      "unblock_requirements": [],
      "estimated_gpu_time": "",
      "success_criteria": [],
      "result_figures": ["Fig. 4", "Fig. 5"],
      "execution_items": [
        {"name": "<案例>", "model_source": "self_trained | pretrained | not_applicable"}
      ],
      "risks": [],
      "recommendation": "recommended | optional | not_recommended"
    }
  ]
}
```

状态规则:
- `runnable`: 静态材料已表明代码、数据、权重和资源要求具备可执行路径。
- `blocked`: 已知缺少必要代码、数据、权重或资源；候选仍必须保留，但在选择门中禁止选择。
- `unknown`: 只有创建实例后才能确认的 CUDA、显存、依赖版本等条件；选择前不得据此删除候选，选择门中允许用户选择并提示后续验证。

---

## 技术栈与依赖

- **PyMuPDF (fitz)** — PDF 全文无损文本提取 + 图像渲染
- **Pillow** — 图像格式支持
- **urllib** — 内置标准网络下载
- **Python 3.8+**

---

## 版本历史

| 版本 | 日期       | 变更                                                                                      |
| ---- | ---------- | ----------------------------------------------------------------------------------------- |
| v2.0 | 2026-06-05 | 新增结构化提取：元数据/章节分段/公式图像/超参匹配/指标匹配/领域检测/Baseline定位/JSON输出 |
| v1.0 | 2026-03    | 初始版本：纯文本提取 + Agent 全量语义分析                                                 |
