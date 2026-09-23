# paper-reproduce

面向智能体的 DataCanvas 论文/项目自动复现 skill 套件，从 `arx/src/ar24/vendor` 单独提取，便于作为独立 GitHub 项目维护、安装和分发。

> 来源与上游版本信息见 [`UPSTREAM_PROVENANCE.md`](UPSTREAM_PROVENANCE.md)。本仓库新增品牌化入口 `datacanvas-paper-reproduce`，同时保留内部 AR24 skill 名称，项目仓库名为 `paper-reproduce`。

## 这个仓库解决什么问题

`paper-reproduce` 提供一组可被 Claude、Codex、OpenCode、OpenClaw 等代码智能体加载的本地 skills，用于把“复现一个论文/项目”的请求拆成可审计、可暂停、可多轮记录的流程：

1. 预检公共复现记录；
2. 选择并确认执行后端；
3. 审计论文和项目；
4. 准备代码、依赖、数据与权重；
5. 执行 smoke test / 复现实验；
6. 记录多轮执行历史；
7. 在用户明确授权后生成 Word 复现报告。

## 包含的 skills

品牌入口：

- `datacanvas-paper-reproduce`：DataCanvas 品牌化论文/项目复现入口。推荐用户显式调用这个 skill。

内部主流程：

- `ar24-auto-reproduct`：全自动项目/论文复现主流程入口，由品牌入口委托调用。

流水线依赖 skills：

- `ar24-instance-manage`：本地、SSH、CCI 后端探测与实例管理。
- `ar24-project-analysis`：项目静态审计与可行性评分。
- `ar24-paper-analysis`：论文解析、baseline 和超参数提取。
- `ar24-readme-parser`：README 结构化解析。
- `ar24-project-checkout`：按后端拉取代码。
- `ar24-dependency-prep`：依赖环境规划与准备。
- `ar24-data-download`：数据集准备。
- `ar24-weight-download`：模型权重准备。
- `ar24-physics-validation`：SciML/计算物理结果验证。
- `ar24-result-figure-reproduction`：论文结果图复现。
- `ar24-repro-report`：生成最终 Word 复现报告。

## 目录结构

```text
.
├── README.md
├── UPSTREAM_PROVENANCE.md
├── datacanvas-paper-reproduce/
│   └── SKILL.md
├── ar24-auto-reproduct/
│   ├── SKILL.md
│   ├── PIPELINE.md
│   └── scripts/
├── ar24-instance-manage/
├── ar24-project-analysis/
└── ...
```

## 为什么不把所有内部 skill 都改成 datacanvas-*

可以全量改名，但当前流水线和 runner 中存在大量对 `ar24-*` 同级目录的路径引用，例如 `ar24-instance-manage`、`ar24-dependency-prep`、`ar24-repro-report`。为了降低断链风险，本仓库采用兼容方案：

- 对外入口使用 `datacanvas-paper-reproduce`，突出 DataCanvas 品牌；
- 内部执行仍使用原 `ar24-*` skill 名称，保证路径、脚本和历史文档稳定；
- 如未来需要彻底品牌化，可再做一次全量 rename + 引用迁移 + 回归测试。

## 安装到不同智能体

### 一键软链接安装

推荐使用软链接，便于在本仓库修改后立即生效：

```bash
cd paper-reproduce

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

如果你的运行环境不适合软链接，可以复制安装：

```bash
cd paper-reproduce
cp -a datacanvas-* ar24-* "$HOME/.claude/skills/"                             # Claude Code
cp -a datacanvas-* ar24-* "${CODEX_HOME:-$HOME/.codex}/skills/"                # Codex
cp -a datacanvas-* ar24-* "${XDG_CONFIG_HOME:-$HOME/.config}/opencode/skills/" # OpenCode
cp -a datacanvas-* ar24-* "$HOME/.openclaw/skills/"                            # OpenClaw
```

## 智能体如何调用该 skill

### 通用调用规则

无论使用哪个智能体，都建议遵守以下规则：

1. **只显式调用品牌入口**：优先让智能体使用 `datacanvas-paper-reproduce`；它会委托内部 `ar24-auto-reproduct`。
2. **不要跳过 `PIPELINE.md`**：品牌入口会委托内部 `ar24-auto-reproduct`，并要求读取其同目录的 `PIPELINE.md`；该文件是流水线步骤的事实源。
3. **保持所有 `ar24-*` 目录同级安装**：主流程会引用其他辅助 skills，目录名是路径约定的一部分，不建议改名。
4. **后端选择必须明确确认**：本地、SSH、CCI 等执行后端需要用户明确选择；沉默、超时或上下文不足不等于授权。
5. **报告生成必须二次确认**：执行结束后，只有用户明确选择生成 DOCX，智能体才能进入报告步骤。
6. **不要提交凭据**：SSH 密码、云算力 access key、token 等只能通过环境变量或本地私有配置传入。

推荐的通用提示词：

```text
Use $datacanvas-paper-reproduce to reproduce this GitHub project: <github_url>
Paper: <paper_url_or_pdf_path>
Please follow the skill instructions strictly. Do not start execution until I explicitly confirm the backend. At step_7.5, show the board and wait for my explicit decision before generating DOCX.
```

如果你的智能体不支持 `$skill-name` 语法，可以改成：

```text
Please load and follow the local skill at ./datacanvas-paper-reproduce/SKILL.md.
Then follow its delegation to ./ar24-auto-reproduct/SKILL.md and ./ar24-auto-reproduct/PIPELINE.md as the pipeline source of truth.
Reproduce this project: <github_url>
Paper: <paper_url_or_pdf_path>
```

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

如果在某个项目内使用，也可以放到项目级目录：

```text
<repo>/.claude/skills/datacanvas-paper-reproduce/SKILL.md
```

### Codex

安装位置：

```text
${CODEX_HOME:-~/.codex}/skills/datacanvas-paper-reproduce/SKILL.md
```

调用示例：

```text
Use $ar24-auto-reproduct to reproduce this GitHub project: https://github.com/<owner>/<repo>
Paper: /absolute/path/to/paper.pdf
Use local backend unless I explicitly choose another backend. Pause at every required confirmation gate.
```

项目级安装位置也可以使用：

```text
<repo>/.codex/skills/datacanvas-paper-reproduce/SKILL.md
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

### 其他智能体或纯 LLM 环境

如果智能体没有内置 skills 发现机制，可以把以下内容作为系统/开发者提示的一部分：

```text
You have access to a local paper reproduction skill suite under ./skills.
First read ./datacanvas-paper-reproduce/SKILL.md completely.
When it references PIPELINE.md or sibling ar24-* skills, read those files before acting.
Do not invent missing paths, do not skip confirmation gates, and never generate the final DOCX report without explicit user authorization.
```

## 复现任务输入建议

为了减少来回确认，建议用户一次性提供：

```yaml
github_url: https://github.com/<owner>/<repo>
paper: https://arxiv.org/abs/<id> 或 /absolute/path/to/paper.pdf
backend_preference: local | ssh | cci | ask_me
workspace_root: /absolute/path/to/workspace
expected_output: audit_report_only | smoke_test | full_docx_after_confirmation
```

最小输入也可以只有：

```text
Use $datacanvas-paper-reproduce to reproduce https://github.com/<owner>/<repo>
```

此时智能体应先做只读预检，然后询问缺失信息和执行后端。

## 安全与权限

- 不要把 `.env`、SSH 私钥、云算力密钥、GitHub token、API key 提交到仓库。
- SSH 密码应通过 `REPRO_SSH_PASSWORD` 等环境变量传入，不要写进 README、prompt 或日志。
- CCI / 云算力执行可能产生费用，必须在创建实例前获得用户明确确认。
- 复现失败、部分完成、用户中止等状态都应记录到执行历史，不能覆盖旧轮次。

## 本地校验

可用下面的命令确认 skill 结构没有明显问题：

```bash
cd paper-reproduce
for skill_dir in datacanvas-* ar24-*; do
  [ -d "$skill_dir" ] || continue
  python3 /path/to/quick_validate.py "$skill_dir"
done
```

也可以做 Python 语法烟测：

```bash
python3 -m compileall -q datacanvas-* ar24-*
find . -name '__pycache__' -type d -prune -exec rm -rf {} +
find . -name '*.pyc' -delete
```

## 本地 Git 提交与推送

首次提交前，如果当前机器没有配置 Git 身份，请先配置：

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

然后提交：

```bash
git add .
git commit -m "Initial extract paper reproduction skills"
```

之后添加你自己的远程仓库地址并推送：

```bash
git remote add origin <your-github-repo-url>
git push -u origin main
```
