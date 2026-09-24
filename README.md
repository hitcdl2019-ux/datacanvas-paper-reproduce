# DataCanvas Paper Reproduce

面向 Claude Code、Codex、OpenCode、OpenClaw 等代码智能体的论文与 GitHub 项目自动复现 Skill 套件。

它把“帮我复现这篇论文 / 跑通这个开源项目”拆解为一套可审计、可暂停、可追踪、可多轮复盘的智能体工作流，适用于 AI 社区论文复现、智算资源试跑、开源项目环境排坑和复现实验报告生成。

> 本仓库对外提供 DataCanvas 品牌入口 `datacanvas-paper-reproduce`；内部复现流水线保留 `ar24-*` skill 名称，以保证脚本路径和历史流程稳定。

## 核心能力

- **论文与项目解析**：读取论文、README 和仓库结构，提取 baseline、超参数、数据与权重需求。
- **复现可行性审计**：生成项目审计报告，记录依赖风险、资源需求、数据/模型限制和潜在阻断项。
- **多后端执行**：支持本地、SSH 远程 GPU 节点，以及具备相应脚本和凭据的 CCI / 智算资源环境。
- **环境与资产准备**：按步骤准备代码、依赖环境、数据集和模型权重，避免跳步执行。
- **复现实验执行**：执行 smoke test、推理、微调或数值实验，并记录指标、日志、耗时和设备信息。
- **结果验证与图表复现**：对 SciML / 计算物理等场景进行结果验证，并在条件满足时复现论文结果图。
- **多轮执行台账**：保留每次尝试的状态、失败原因和产物路径，不覆盖历史轮次。
- **报告交付**：用户明确授权后，生成工业级 Word 复现报告。

## 适用场景

本项目适合发布在智算中心或 AI 社区中，用于：

- 论文复现任务的标准化执行；
- GitHub 开源项目的环境搭建、依赖排坑和 smoke test；
- GPU / CPU / SSH 远程节点上的复现实验试跑；
- SciML、计算物理、流体力学、深度学习等项目的复现审计；
- 需要保留过程证据、失败记录、算力消耗和最终报告的科研工程任务。

> 说明：本仓库提供智能体 skill 与本地脚本，不自带云账号、数据集、模型权重或论文项目的授权访问能力。受限数据、闭源权重和云算力资源需要用户自行确认权限。

## 快速开始

克隆仓库：

```bash
git clone https://github.com/hitcdl2019-ux/datacanvas-paper-reproduce.git
cd datacanvas-paper-reproduce
```

以 Codex 为例，安装到本地 skills 目录：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
for skill_dir in datacanvas-* ar24-*; do
  [ -d "$skill_dir" ] || continue
  ln -sfn "$(pwd)/$skill_dir" "${CODEX_HOME:-$HOME/.codex}/skills/$(basename "$skill_dir")"
done
```

在智能体中调用：

```text
Use $datacanvas-paper-reproduce to reproduce this GitHub project: <github_url>
Paper: <paper_url_or_pdf_path>
```

如果暂时没有论文，也可以只提供仓库地址。智能体会先执行只读预检，再询问缺失信息和执行后端。

## 工作流

```text
用户输入论文 / GitHub 仓库
        ↓
DataCanvas Paper Reproduce
        ↓
公共复现预检
        ↓
论文解析 + 项目审计
        ↓
用户确认执行后端
        ↓
代码 / 依赖 / 数据 / 权重准备
        ↓
复现实验 / Smoke Test
        ↓
结果验证 + 执行台账
        ↓
用户确认是否生成报告
        ↓
Word 复现报告
```

关键约束：

- 后端选择必须由用户明确确认；沉默、超时或上下文不足不构成授权。
- 最终 DOCX 报告必须在实验结束后由用户再次明确授权生成。
- 每轮执行都应保留独立 run id、日志、状态和产物路径。

## 支持的智能体

| 智能体 | 默认安装目录 | 推荐调用方式 |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/` | `Use $datacanvas-paper-reproduce ...` |
| Codex | `${CODEX_HOME:-~/.codex}/skills/` | `Use $datacanvas-paper-reproduce ...` |
| OpenCode | `${XDG_CONFIG_HOME:-~/.config}/opencode/skills/` | `Use skill datacanvas-paper-reproduce` |
| OpenClaw | `~/.openclaw/skills/` | `Use $datacanvas-paper-reproduce ...` |

## 安装方式

### 软链接安装（推荐）

软链接便于后续更新仓库后立即生效。

```bash
cd datacanvas-paper-reproduce

# Claude Code
mkdir -p "$HOME/.claude/skills"
for skill_dir in datacanvas-* ar24-*; do
  [ -d "$skill_dir" ] || continue
  ln -sfn "$(pwd)/$skill_dir" "$HOME/.claude/skills/$(basename "$skill_dir")"
done

# Codex
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
for skill_dir in datacanvas-* ar24-*; do
  [ -d "$skill_dir" ] || continue
  ln -sfn "$(pwd)/$skill_dir" "${CODEX_HOME:-$HOME/.codex}/skills/$(basename "$skill_dir")"
done

# OpenCode
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills"
for skill_dir in datacanvas-* ar24-*; do
  [ -d "$skill_dir" ] || continue
  ln -sfn "$(pwd)/$skill_dir" "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills/$(basename "$skill_dir")"
done

# OpenClaw
mkdir -p "$HOME/.openclaw/skills"
for skill_dir in datacanvas-* ar24-*; do
  [ -d "$skill_dir" ] || continue
  ln -sfn "$(pwd)/$skill_dir" "$HOME/.openclaw/skills/$(basename "$skill_dir")"
done
```

### 复制安装

如果运行环境不适合软链接，可以复制安装：

```bash
cd datacanvas-paper-reproduce

mkdir -p "$HOME/.claude/skills"
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills"
mkdir -p "$HOME/.openclaw/skills"

cp -a datacanvas-* ar24-* "$HOME/.claude/skills/"
cp -a datacanvas-* ar24-* "${CODEX_HOME:-$HOME/.codex}/skills/"
cp -a datacanvas-* ar24-* "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills/"
cp -a datacanvas-* ar24-* "$HOME/.openclaw/skills/"
```

## 如何调用

### 最小输入

```text
Use $datacanvas-paper-reproduce to reproduce https://github.com/<owner>/<repo>
```

### 推荐输入

```yaml
github_url: https://github.com/<owner>/<repo>
paper: https://arxiv.org/abs/<id> 或 /absolute/path/to/paper.pdf
backend_preference: local | ssh | cci | ask_me
workspace_root: /absolute/path/to/workspace
expected_output: audit_report_only | smoke_test | full_docx_after_confirmation
```

### 通用提示词

```text
Use $datacanvas-paper-reproduce to reproduce this GitHub project: <github_url>
Paper: <paper_url_or_pdf_path>
Please follow the skill instructions strictly. Do not start execution until I explicitly confirm the backend. At step_7.5, show the board and wait for my explicit decision before generating DOCX.
```

### 不支持 `$skill-name` 语法的智能体

如果智能体没有内置 skills 发现机制，可以显式要求它读取本仓库中的入口文件：

```text
Please load and follow the local skill at ./datacanvas-paper-reproduce/SKILL.md.
Then follow its delegation to ./ar24-auto-reproduct/SKILL.md and ./ar24-auto-reproduct/PIPELINE.md as the pipeline source of truth.
Reproduce this project: <github_url>
Paper: <paper_url_or_pdf_path>
```

## 各智能体调用示例

### Claude Code

安装位置：

```text
~/.claude/skills/datacanvas-paper-reproduce/SKILL.md
```

调用示例：

```text
Use $datacanvas-paper-reproduce to reproduce this repository: https://github.com/<owner>/<repo>
The paper is: https://arxiv.org/abs/<id>
Start with the required public repro precheck and ask me to confirm the execution backend before running anything mutable.
```

### Codex

安装位置：

```text
${CODEX_HOME:-~/.codex}/skills/datacanvas-paper-reproduce/SKILL.md
```

调用示例：

```text
Use $datacanvas-paper-reproduce to reproduce this GitHub project: https://github.com/<owner>/<repo>
Paper: /absolute/path/to/paper.pdf
Use local backend unless I explicitly choose another backend. Pause at every required confirmation gate.
```

### OpenCode

安装位置：

```text
${XDG_CONFIG_HOME:-~/.config}/opencode/skills/datacanvas-paper-reproduce/SKILL.md
```

调用示例：

```text
Use skill datacanvas-paper-reproduce.
Target repository: https://github.com/<owner>/<repo>
Paper: https://arxiv.org/abs/<id>
Follow the pipeline exactly, keep execution history, and wait for my explicit DOCX authorization after step_7.5.
```

### OpenClaw

安装位置：

```text
~/.openclaw/skills/datacanvas-paper-reproduce/SKILL.md
```

调用示例：

```text
Use $datacanvas-paper-reproduce to run a paper reproduction task.
GitHub repo: https://github.com/<owner>/<repo>
Paper: <paper_url_or_pdf_path>
Before step_0, ask me to confirm local / SSH / CCI backend.
```

## 典型产物

一次复现任务可能产生以下产物：

- 项目审计报告：`<repo_name>_Audit_Report.md`
- 执行历史台账：`execution_run_history.json`
- 复现实验日志、指标、图表和中间文件
- 结果验证文件：科学契约、资产清单、验证结果等
- 最终 Word 报告：`<repo_name>_final_reproduce_report.docx`

实际产物取决于论文项目、执行后端、数据/权重可访问性和用户选择的执行计划。

## 架构说明：datacanvas-* 与 ar24-* 的关系

本仓库同时包含 `datacanvas-paper-reproduce` 和多个 `ar24-*` 目录：

- `datacanvas-paper-reproduce`：DataCanvas 品牌化入口，推荐用户直接调用。
- `ar24-auto-reproduct`：内部复现主流程，定义 step_0 到 step_8 的执行规则。
- 其他 `ar24-*`：流水线依赖模块，分别负责算力探测、论文解析、项目审计、代码拉取、依赖准备、数据下载、权重下载、结果验证和报告生成。

保留 `ar24-*` 命名是为了避免破坏内部脚本路径和历史流程引用。对外使用时，只需要调用 `datacanvas-paper-reproduce`。

## 目录结构

```text
.
├── README.md
├── datacanvas-paper-reproduce/
│   └── SKILL.md
├── ar24-auto-reproduct/
│   ├── SKILL.md
│   ├── PIPELINE.md
│   └── scripts/
├── ar24-instance-manage/
├── ar24-project-analysis/
├── ar24-repro-report/
└── ...
```

## 安全与权限

- 不要把 `.env`、SSH 私钥、云算力密钥、GitHub token、API key 提交到仓库。
- SSH 密码应通过 `REPRO_SSH_PASSWORD` 等环境变量传入，不要写进 README、prompt 或日志。
- 受限数据集、闭源模型权重和商业软件许可证需要用户自行确认访问权限。
- CCI / 云算力执行可能产生费用，必须在创建实例前获得用户明确确认。
- 复现失败、部分完成、用户中止等状态都应记录到执行历史，不能覆盖旧轮次。

## 本地校验

确认 skill 结构：

```bash
cd datacanvas-paper-reproduce
for skill_dir in datacanvas-* ar24-*; do
  [ -d "$skill_dir" ] || continue
  python3 /path/to/quick_validate.py "$skill_dir"
done
```

Python 语法烟测：

```bash
python3 -m compileall -q datacanvas-* ar24-*
find . -name '__pycache__' -type d -prune -exec rm -rf {} +
find . -name '*.pyc' -delete
```

## 维护说明

- 面向用户的入口是 `datacanvas-paper-reproduce`。
- 内部 `ar24-*` 目录名是流水线脚本引用的一部分，不建议随意改名。
- 更新内部流水线时，应同步检查 `ar24-auto-reproduct/PIPELINE.md`、各 runner 脚本和 README。
- 发布到 AI 社区前，建议补充项目封面、示例复现截图或示例报告路径，以提高可读性。

