---
name: ar24-project-analysis
description: 面向 SciML、计算物理、流体力学和流变学论文的复现可行性评估与静态排雷工具。使用唯一物理评分量表，扫描多运行时环境、物理配置、代码高危密钥泄露、数据死链及受限模型。
---

# Repo Auditor Skill (纯净安全版)

项目复现可行性静态审计工具，帮助您在投入高昂的算力和时间配环境之前，以秒级速度评估一个 GitHub 项目是否具备可复现性，避免跌入“学术垃圾”或“配置死胡同”的陷阱。

## 核心理念

*   **“静态扫描，排雷先行”** —— 不依赖任何外网大模型 API、不需要在服务器配置大模型 Key [1.2, 1.3]。通过纯本地的高性能静态规则扫描、API 在线状态检测，保障数据安全性。
*   **“单一职责，只读安全”** —— 本 Skill 遵循**只读原则**。它仅负责暴露风险并输出报告，绝不篡改、写入或修补用户的代码仓库。所有的依赖准备与环境构建工作，均交由协作 Skill `ar24-dependency-prep` 完成。

---

## 核心功能

### 1. 唯一物理评分档案
- 所有进入本 Skill 的仓库都使用 `sciml_physics` 评分档案，不再提供普通软件工程或传统机器学习评分维度。
- 领域关键词识别只用于报告这是 PINN、神经算子、CFD、湍流、流变学或其他物理子方向，不再决定评分量表。
- 即使关键词识别不准确，也必须执行物理工况、数值方法、资产和验证门禁，不得回退到普通项目评分。

### 2. 高危密钥泄露扫描 (Secrets Leak Scan) 🆕
- **安全红线审计**：在所有模式下均会自动检索代码及配置文件，扫描是否硬编码泄露了 OpenAI API Key、AWS Access Key、数据库密码、私钥等高危敏感信息，防范安全泄露。

### 3. 百分之百离线超参评估 🆕
- **本地启发式网格复杂度分析**：无需调用任何大模型，利用同义词映射扫描主流格式配置文件，提炼出论文复现点、超参数，并根据时空复杂度模型（Batch * Seq * Spatial^2）推算出每个复现点在 H100 (80GB) 及 RTX 4090 (24GB) 上的理论显存与运行时长。
- **Resource Gate 资源门控**：结合 step_0 选定后端的真实 GPU/显存、模型参数量线索、`from_pretrained` 模型 ID、checkpoint 与 batch/resolution/seq_len，输出 `resource_gate`。字段包括 `fit_status`（`fit` / `risky` / `insufficient` / `unknown`）、`recommended_resource`、最低/推荐显存与限制建议；在 `score >= 60` 时资源不足不得隐藏候选执行计划，只能标注限制与恢复条件。

### 4. GPU兼容性与旧版 Python EOL 排雷
- **PyTorch 版本限制**：自动检测硬编码旧版 PyTorch 1.x 的项目并报警（H100 架构必须使用 PyTorch >= 2.0）。
- **Apex 编译雷区**：检测是否有已弃用且极难在 H100 下顺利编译的 `NVIDIA Apex` 混合精度库。
- **Python 3.8 停维预警**：扫描并阻击 Python $\le 3.8$ 的项目，避免在 CUDA 12.x 环境下因缺少编译好的 wheel 导致本地编译失败。

### 5. 外部链接探测与受限模型（Gated Model）阻碍审计
- 自动提取 README 中的外部链接，并开展连通性探测。采用学术平台域名白名单机制（如 Zenodo, HuggingFace），过滤因本地网络限制导致的临时 403 误报。
- 扫描 `from_pretrained` 模型引用，自动识别 LLaMA、Gemma 等受限模型，并配合本地 `gated_model_mirrors.yaml` 数据库检测是否有可用的国内/局域网免 Token 镜像源。

---

## 项目结构

本 Skill 的目录结构保持极度纯粹，仅包含主审计代码、静态比对数据库：

```text
ar24-project-analysis/  (职责：只读静态分析与可行性打分)
├── scripts/
│   └── main.py                 # 主审计脚本（输出 Markdown 审计报告）
├── config/                     # 存放审计过程中所需读取的静态比对库
│   └── gated_model_mirrors.yaml# 静态模型镜像表（供识别并映射受限模型）
└── SKILL.md                    # 技能说明文档
```

---

## 使用方法

运行审计：
```bash
python <SKILL_DIR>/scripts/main.py <GitHub仓库URL> --run-id "$RUN_ID"
```

---

## 评分与后续工作流建议

评分采用唯一的 **SciML/计算物理 v4.0 确定性多维量表**，固定输出 `physics_fidelity` 与 `physics_gate`。大模型不直接决定分数，只解释脚本生成的证据与扣分项；资源不足只限制候选计划，不能改变 `overall_score`。

维度权重固定为 100 分：

| 维度键 | 展示名 | 权重 | 客户可理解含义 |
|---|---|---:|---|
| `documentation_entry` | 文档与入口闭环 | 10 | README 是否说明训练、推理、数值模拟和验证入口 |
| `environment` | 多运行时环境可构建性 | 15 | Python、Julia、MPI、编译器、OpenFOAM或容器环境是否可重建 |
| `artifacts` | 数据、网格与参数资产 | 15 | 实验/DNS/LES数据、网格、权重、normalizer和物性参数是否可获得 |
| `migration_cost` | 运行迁移成本 | 10 | 路径、参数、编译和系统求解器迁移成本；当前机器资源不在此扣分 |
| `experiment_reproduction` | 计算链复现实验闭环 | 20 | 是否有训练、推理、数值模拟、统计评价和结果验证入口 |
| `physics_fidelity` | 物理工况与验证可信度 | 20 | 方程、状态变量、几何网格、初边值条件、物性、数值方法和指标证据 |
| `risk_gate` | 安全/合规红线 | 10 | 密钥、私钥、token、许可证、受限数据/模型等准入风险 |

`migration_cost` 固定包含四个子信号：

| 子信号 | 含义 |
|---|---|
| 路径迁移 | 是否写死 `/home/xxx`、`/data/xxx`、Windows 用户目录等作者本机路径 |
| 硬件/GPU 迁移 | 作者/论文 GPU 与本机 GPU 的架构、显存、CUDA 扩展兼容性 |
| 运行参数迁移 | batch size、device id、distributed 参数、num_workers 是否写死 |
| 系统依赖迁移 | nvcc、gcc、OpenGL/EGL、MPI、Taichi/CUDA 扩展等系统依赖 |

硬件/GPU信息会优先读取环境变量 `AUTHOR_GPU_SPEC` / `PAPER_GPU_SPEC`，否则从 README/配置中启发式识别作者 GPU；本机 GPU 通过 `nvidia-smi` 或 `LOCAL_GPU_SPEC` 探测。硬件差异只形成资源提示并进入 `resource_gate`，不扣减论文/项目的 `overall_score`。只有代码自身写死架构、路径或不可迁移系统依赖时，才允许在迁移成本维度扣分。

输出产物：
- Markdown 审计报告：`$RUN_ROOT/step_1/<repo_name>_Audit_Report.md`。这是当前运行和最终产物集的强制交付物，必须使用 UTF-8、非空且可读；至少包含仓库来源、SciML/计算物理 v4 各维度评分与扣分证据、`resource_gate`、`physics_gate`、安全/合规风险、阻断项、硬件迁移说明和最终 verdict。缺失、空文件、不可读或命名不规范都必须令当前运行在 step_1 失败。
- 机器可复核评分：`audit_score.json`
- 资源门控字段：`audit_score.json.resource_gate`，包含 `fit_status`、`recommended_resource`、`message`、选定后端资源与需求证据。后续流水线使用它标注候选执行计划的资源限制，不得在 `score >= 60` 时删除候选。

审计报告与 `audit_score.json` 必须写入当前运行的同一 `step_1/` 并保持结论一致。`audit_score.json` 必须是有效的 `scoring_profile=sciml_physics` 对象，并包含 `overall_score`、`verdict`、`dimensions`、`resource_gate`、`physics_gate`。流水线后续步骤不得删除、覆盖或把审计报告改名；step_8 会按获授权 `run_id` 再次校验两份证据。

Verdict 规则：

1. `BLOCKED`：命中红线 blocker，如高危密钥泄露、仓库无法 clone 等。
2. `PASS`：`overall_score >= 80` 且无 blocker。
3. `CONDITIONAL`：`60 <= overall_score < 80` 且无 blocker。
4. `HIGH_RISK`：`overall_score < 60` 且无 blocker。
5. `UNSCORABLE`：证据不足或仓库无法访问。

后续流水线应优先读取 `audit_score.json` 的 `verdict`、`blockers`、`overall_score`、`hardware_migration`、`resource_gate` 和 `risk_gate_status`，不要让模型根据 Markdown 自由改分。`score < 60` 才阻止进入执行计划选择；`resource_gate.fit_status=insufficient` 用于标注受阻候选并提示用户换用 `recommended_resource`，但不得隐藏该候选。

物理项目还必须读取 `physics_gate.execution_blockers` 与 `physics_gate.claim_blockers`。前者只阻止依赖缺失配置的候选，后者阻止 step_7/8 宣称物理复现成功。
