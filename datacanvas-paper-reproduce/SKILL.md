---
name: datacanvas-paper-reproduce
description: DataCanvas branded entrypoint for AR24 paper and GitHub project reproduction. Use when users ask to reproduce a paper, reproduce a repository, run a paper reproduction workflow, or generate an authorized reproduction report.
---

# DataCanvas Paper Reproduce

This is the DataCanvas-branded public entrypoint for the paper reproduction skill suite.

When this skill is invoked:

1. Treat sibling skill `ar24-auto-reproduct` as the authoritative execution workflow.
2. Read `../ar24-auto-reproduct/SKILL.md` completely before taking action.
3. When that file instructs you to read `PIPELINE.md`, read `../ar24-auto-reproduct/PIPELINE.md` and follow it as the single source of truth.
4. Keep all confirmation gates from `ar24-auto-reproduct` unchanged, especially backend selection and final DOCX authorization.
5. Use the sibling `ar24-*` skills by their original names; do not rename internal runner paths at runtime.

Branding rule: in user-facing summaries, call the product/workflow “DataCanvas Paper Reproduce”, while preserving exact artifact paths, skill names, run IDs, and command names when reporting technical evidence.
