---
name: ar24-instance-manage
description: 执行后端选择、算力探测与环境自检。支持本地、普通 SSH 和 CCI 云算力；CCI 分支提供明确资源确认、双实例生命周期、安全状态、Paramiko SSH/SFTP 与清理恢复。
---

# 后端选择、算力探测与 CCI 生命周期 Skill (ar24-instance-manage)

> **默认优先 CCI，可切换本地或普通 SSH**：顶层复现流程默认预选并首先展示 CCI，local/SSH 行为保持不变。预选不构成授权；只有用户明确确认“CCI 云算力”，完成只读预检并再次明确确认规格、镜像、已有 NAS 与价格后，CCI 分支才允许创建付费实例。

---

## 一、使用方法

`<SKILL_DIR>` 指本 skill 安装目录（OpenClaw: `~/.openclaw/skills/ar24-instance-manage`；Claude Code: `.claude/skills/ar24-instance-manage`；其他平台为脚本实际所在目录）。脚本通过 `__file__` 自定位，可从任意 CWD 调用。

```bash
# 完整探测 + 自检
python <SKILL_DIR>/scripts/probe.py --backend local

# SSH 远端探测
python <SKILL_DIR>/scripts/probe.py --backend ssh --ssh "ssh -p 40072 user@host"

# SSH password auth after missing_ssh_password pause
REPRO_SSH_PASSWORD='<password>' python <SKILL_DIR>/scripts/probe.py --backend ssh --ssh "ssh -p 40072 user@host"
python <SKILL_DIR>/scripts/probe.py --backend ssh --ssh "ssh -p 40072 user@host" --ssh-password '<password>'

# 仅输出 mode、gpu_count 与 cpu_model（精简）
python <SKILL_DIR>/scripts/probe.py --brief
```

CCI 的独立依赖只影响 CCI 分支：

```bash
python -m pip install -r <SKILL_DIR>/requirements-cci.txt
```

CCI 凭证只能通过进程环境读取：

```bash
export ALAYANEW_ACCESS_KEY="<从安全凭证存储注入>"
export ALAYANEW_SECRET_KEY="<从安全凭证存储注入>"
# 可选：ALAYANEW_ACCESS_TOKEN、ALAYANEW_BASE_URL
python <SKILL_DIR>/scripts/cci_manager.py preflight --session-id <session_id>
```

严禁把上述凭证放入命令参数、对话、JSON、日志、台账或报告。完整 CCI 操作见 [CCI 使用与恢复说明](../docs/cci-usage-and-recovery.md)。

退出码：`0` = 探测或 CCI 操作完成；`2` = 阻塞、失败或需要清理；`3` = `backend=ssh` 缺少连接信息，或 `backend=cci` 缺少状态文件。缺少 CCI 依赖或凭证只暂停 CCI 分支。

---

## 二、输出字段

| 字段 | 说明 |
|---|---|
| `backend` | `local` / `ssh` / `cci` —— 决定后续步骤的执行位置 |
| `status` | `ready` / `blocked` / `paused` / `error` |
| `mode` | `cpu` / `single_gpu` / `multi_gpu` —— 决定 step_4 执行分支 |
| `cpu_model` | 选定后端实测 CPU 型号；系统无法读取时为 `null`，不得编造通用型号 |
| `gpu_count` | 选定后端可见 GPU 数量 |
| `gpus[]` | `{index, name, vram_gb, capability}` 每张卡详情 |
| `arch_list` | `TORCH_CUDA_ARCH_LIST` 动态值（如 `"8.6;8.9"`），供 CUDA 扩展编译；探测不到则为空（交由 PyTorch 自动决定） |
| `cpu_cores` | 选定后端可见逻辑核总数，仅表示容量，不等于某阶段显式分配的逻辑核数量 |
| `workspace_root` | 显式路径确认后才有值；首次 step_0 通常为 `null` |
| `envs_root` | 显式路径确认后才有值；首次 step_0 通常为 `null` |
| `path_policy` | `pending_user_input` 表示 step_0 后必须请求用户输入存储基准路径并调用 `scripts/path_policy.py` |
| `env_backend` | `uv` / `conda`，优先 uv，缺 uv 时回退 conda |
| `conda_envs_root` | `envs_root` 的兼容别名 |
| `checks.uv` | `{available, path}` —— 优先后端 |
| `checks.conda` | `{available, path}` —— uv 不可用时的回退后端 |
| `checks.nvcc` | `{available, path, cuda_home}` —— 缺失非致命（仅影响源码编译扩展） |
| `checks.disk` | `{path, free_gb}` —— <50GB 给出告警 |
| `checks.memory/compiler/cpp_compiler/cmake/mpi/julia/openfoam/container` | 科学计算运行时与系统能力的路径、版本和可用性 |
| `ready` | 是否满足最低运行条件（无 blockers） |
| `warnings[]` | 不致命但需提示用户的问题（无 GPU / 缺 nvcc / 磁盘不足） |
| `blockers[]` | 连接或探测本身的致命问题，`ready=false` 时填充 |
| `ssh` | 仅 SSH 后端存在，结构化连接信息 `{user, host, port, target}` |
| `pause_reason` | 仅暂停时存在；缺少 SSH 信息时为 `missing_ssh_connection` |

---

## 三、运行模式决策

| GPU 数量 | mode | step_4 行为 |
|---|---|---|
| 0 | `cpu` | 跳过 CUDA 编译，仅 CPU 推理验证通跑性，VRAM 标 N/A |
| 1 | `single_gpu` | `CUDA_VISIBLE_DEVICES=0` 单卡推理/训练 |
| ≥2 | `multi_gpu` | 入口支持则 `torchrun --nproc_per_node=N`，否则降级单卡并提示 |

---

## 四、环境自检与降级

- **同时缺 uv 和 conda** → Python 候选受限，但非 Python 候选仍可继续。若 uv 可用则使用 `uv venv`；否则回退 conda。
- **无 GPU** → 非致命，降级纯 CPU 模式，明确提示用户「GPU 性能指标不可得」。
- **缺 nvcc（有 GPU）** → 非致命告警，需源码编译扩展（flash-attn/nvdiffrast 等）的项目可能失败，纯预编译 wheel 项目不受影响。
- **磁盘 <50GB** → 非致命告警，提示权重/数据下载可能不足。

---

## 五、注意事项

1. `capability` 字段依赖选定后端已装 `torch`；探测阶段通常未装，留空属正常，step_4 编译时会再行确定架构。
2. 本 skill **只读探测**；本地模式不联网，SSH 模式只连接用户提供的远端目标并执行内联探测脚本。
3. 工作目录根 / 虚拟环境根不由本 skill 自动兜底。step_0 完成后调用 `scripts/path_policy.py --base-root <exact_root> --repo-name <repo_name>`；精确基准路径本身就是 `REPRO_ROOT`，不得追加 `ar24` 或项目名。随后由流水线立即 `reserve-run` 并导出根路径、运行路径和共享资产变量。
4. 不要把用户输入的整条 SSH 命令作为 shell 字符串直接执行；脚本会解析为结构化字段后组装 `ssh -p <port> <target> <probe>`。
5. `cpu_cores` 不能直接写入复现阶段的 `device_count`。CPU `device_count` 必须来自命令、配置或环境变量中显式分配的逻辑核，并保留 `device_count_evidence`。
6. `cpu_model` 探测遵循 **OS 专用来源优先**：Linux/Unix 先解析 `/proc/cpuinfo`（可用时），Windows 先读取 `PROCESSOR_IDENTIFIER`；只有专用来源不可用时才回退 `platform.processor()`。本地探测与 SSH 内嵌探测脚本必须保持相同顺序，避免把 `x86_64`、`AMD64` 等通用架构字符串误报为具体 CPU 型号。
7. CCI 状态默认位于 `.ar24/cci/<session_id>/cci_state.json`，也可用 `AR24_CCI_STATE_ROOT` 指定本地安全目录。状态只保存选择、价格、实例 ID、时间和释放状态，不保存 SSH 密码或请求鉴权头。
8. CCI 平台自动停止和自动释放固定关闭。任何成功、失败、取消、继续其他计划和异常恢复路径都必须主动 `release`；失败时进入 `cleanup_required` 并阻止新建实例。
9. CCI step_0 的目录证据是 `cci_catalog_planned`，只能用于计划评估；创建实例后的 `probe` 才是 `cci_instance_measured`。GPU 实测不满足确认规格或计划最低要求时，禁止 step_7。
10. CCI 付费实例创建前在状态返回的 `local_staging_root` 建立 v3 根布局并保存控制面证据；构建实例探测通过后必须把根文件、共享目录和运行目录同步到用户确认的 NAS 精确根，再切换远端 `WORKSPACE_ROOT`。本地暂存根不得作为 NAS 实测证据。
