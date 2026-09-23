# Paper Analysis Skill (多领域自适应论文解析工具)

全自动、多领域论文解析 skill。接收本地 PDF 绝对路径、arXiv PDF 链接或 GitHub 项目链接，解析论文内容并由底层协同 Agent 智能判定其学术领域。自适应提炼多领域的：核心复现点、验证指标和超参数，生成结构化报告保存至工作区。

---

## 核心能力

- **多源数据读取与无损提取**：
  - **本地 PDF 导入（优先级最高）**：直接解析本地磁盘上的 `.pdf` 论文。
  - **自动网络下载**：从 arXiv 链接、标准 PDF 链接直接下载。
  - **GitHub 间接解析**：自动从 README 提取论文链接并下载。
- **智能多领域自适应（Domain Adaptation）指导**：
  - **🧬 生物计算**：引导 Agent 提炼蛋白质结构预测、配体分子对接等复现点；提取 TM-score、lDDT、RMSD 等物理指标；关联 PDB 等数据集。
  - **💬 大语言模型**：引导 Agent 提炼预训练、监督微调（SFT）、人类反馈偏好（RLHF/DPO）等复现点；提取 PPL、BLEU、ROUGE、MMLU、GSM8K 等指标。
  - **🌀 科学计算与物理 AI (流体)**：引导 Agent 提炼 2D 管道流、3D 槽道流、周期山丘等物理复现案例；提取平均流速剖面、脉动均方根（RMS）、雷诺应力、湍流能量谱等物理指标。
  - **🖼️ 计算机视觉**：引导 Agent 提炼目标检测、扩散生成、三维重建等复现点；提取 PSNR、SSIM、LPIPS、FID、mAP 等指标。

---

## 输入参数说明

| 参数名 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `--paper-path` | string | 否 | **本地 PDF 文件的绝对路径（优先级最高）**。若提供，则直接进行本地解析。 |
| `--paper-url` | string | 否 | arXiv 论文 PDF 链接或标准的网络 PDF 链接（可选）。 |
| `--github-url` | string | 否 | 论文配套的 GitHub 仓库链接（可选）。 |

> 📌 **输入解析优先级**：`--paper-path`（本地直接解析） > `--paper-url`（网络下载） > `--github-url`（间接解析）

---

## 使用方法

### 方式一：作为 Agent Skill 自动触发
当用户对智能体说“分析论文”、“解析 paper”等意图时，智能体将自动匹配并激活本 Skill，率先调用 Python 脚本跑通文本提取，随后由大模型进行自我总结。

### 方式二：终端命令行手动调用
```bash
python <SKILL_DIR>/scripts/analyze_paper.py \
  --project-name CoNFiLD \
  --paper-path "/absolute/path/to/CoNFiLD.pdf"
```

运行前必须已完成路径确认并导出 `REPRO_OUTPUT_ROOT=$REPRO_ROOT`。

---

## 自动化分析流程

```text
1. 输入校验
   ├── 优先检测 --paper-path ──> 存在则直接本地解析 (Local Mode)
   ├── 其次检测 --paper-url  ──> 使用 urllib 下载并存盘 (Download Mode)
   └── 最后检测 --github-url ──> 获取 README 文本 ──> 正则过滤 arXiv 链接 ──> 下载

2. PDF 文本提取
   └── 调用 PyMuPDF (fitz) 按页将 PDF 还原为纯文本 ──> 归档为 paper_text.txt

3. 语义理解与报告生成 (Agent 启动其底层原生大模型)
   ├── 读取上一步生成的 paper_text.txt 文本
   ├── 自动判定该论文所属领域（生物计算/大模型/流体物理/CV）
   ├── 提炼出该领域的：核心复现点、验证指标、基准数据集及训练超参数
   └── 输出标准的 Markdown 格式报告：<paper_name>_report.md
```

---

## 推荐 Agent 输出的 Markdown 报告结构

当协同 Agent 调用其底层原生大模型阅读由本脚本生成的 `paper_text.txt` 后，建议在论文输出目录下生成如下结构的报告：

```markdown
# <Paper Name> 论文分析报告

## 一、 基本信息
- 识别学术领域: **[生物计算/大模型/流体物理/CV]**
- arXiv ID: [ID]
- Venue: [NeurIPS/ICLR/Nature Communications...]

## 二、 研究背景与创新点
[从 Abstract/Introduction 中提炼的创新点]

## 三、 核心复现点与复现指标
### 1. 核心复现点（Reproduction Cases / Tasks）
- [x] Case 1: ...
- [x] Case 2: ...

### 2. 复现验证/对比评估指标
- [x] 指标 1 (如雷诺剪切应力)
- [x] 指标 2 (如湍流能量谱)

## 四、 实验设置与基准数据集
[论文中使用的标准基准数据集]

## 五、 核心数值表格（Baseline 基准对照）
[提取论文中的关键数值表格，特别是 Baseline 对比数据]

## 六、 训练超参数
- 学习率 (Learning Rate): [Value]
- 批次大小 (Batch Size): [Value]
- 迭代轮数 (Epochs/Steps): [Value]
```

---

## 技术栈与依赖

- **PyMuPDF (fitz)** —— PDF 全文无损文本提取
- **urllib** —— 内置标准网络下载
- **Python 3.8+**
```
