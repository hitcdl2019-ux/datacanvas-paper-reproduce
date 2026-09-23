---
name: ar24-readme-parser
description: 在 AR24 复现流水线中通用解析 README，生成保留行号的结构化 AST、语义归类结果和各 step 可消费的派生字段。
---

# AR24 README 通用解析 Skill

本 skill 负责把项目 README 转换为可审计的结构化结果。它不直接执行任何命令，也不创建环境、下载数据、下载权重或运行推理。

## 解析原则

- 先做确定性 Markdown 结构解析，再做语义归类。
- 每个 README 行都必须归属到一个 block。
- 每个 block 保留原文、行号、标题路径、链接、inline code 和解析状态。
- 可执行派生必须引用来源 block，不能凭空生成。
- 无法自动执行的内容必须记录原因，例如引用、版权、人工下载、结构标题或上下文说明。

## 输出字段

`readme_parser.py` 提供两个主要入口：

```python
parse_readme_text(text, source="README.md")
analyze_readme_text(text, source="README.md")
```

`parse_readme_text` 输出：

- `source`
- `sha256`
- `line_count`
- `block_count`
- `blocks`
- `coverage`

`analyze_readme_text` 额外输出：

- `commands`
- `links`
- `issues`
- `unprojected_blocks`
- `block_semantics`
- `step_projection`

## Step 派生关系

- step_4 消费 `step_projection.dependency_cmds`。
- step_5 消费 `step_projection.data_cmds`，并读取 `restricted_datasets` 判断是否需要人工确认。
- step_6 消费 `step_projection.weight_cmds`。
- step_7 消费 `step_projection.deferred_step7_cmds`。
- step_8 消费 `readme_parse` 中的覆盖率、未投影原因、人工下载问题、引用和版权信息。

## 大模型边界

README 的结构保真解析不依赖大模型。大模型只能作为语义审核或弱语义补充，输出必须引用已有 block id，并且必须通过 schema 校验后才能进入 step 派生。
