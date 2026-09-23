# Vendored AR24 reproduction skills

These directories are a verbatim copy of the AR24 skill suite. They are the
executable half of the pipeline: `ar24.pipeline` owns the step ledger, and each
step delegates its real work to the runner scripts here.

## Source

| Field | Value |
| --- | --- |
| Upstream | `/home/lumk/AutoReproduction` |
| Revision | `ed4de969eede54e8ac8e6b70c284a60fef57896f` |
| Vendored on | 2026-07-30 |
| Files | 86 (`__pycache__` and `*.pyc` excluded) |
| Skills | 12 (includes `ar24-result-figure-reproduction`) |

`ar24-auto-reproduct/PIPELINE.md` is the single source of truth for step
semantics; `local_reproduce.yaml` is kept only for backward compatibility.

## Rules

- Do not hand-edit these files. Fixes belong upstream, then re-vendor, so the
  revision above stays meaningful.
- Directory names are load-bearing: `SKILL.md` files reference sibling runners
  by paths like `ar24-instance-manage/scripts/probe.py`.
- `ar24.pipeline.runners` resolves these paths at runtime; add new steps to the
  catalog in `ar24/pipeline/steps.py` rather than hardcoding paths elsewhere.

## Re-vendoring

```sh
rm -rf src/ar24/vendor/ar24-*
cp -r <upstream>/ar24-* src/ar24/vendor/
find src/ar24/vendor -name __pycache__ -type d -exec rm -rf {} +
find src/ar24/vendor -name '*.pyc' -delete
```

Then update the revision row above and run the test suite.
