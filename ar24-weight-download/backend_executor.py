import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def parse_ssh_connection(command: str) -> dict:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"SSH connection parse failed: {exc}") from exc
    if not parts or parts[0] != "ssh":
        raise ValueError("SSH command must start with ssh, for example: ssh -p 40072 user@host")

    port = 22
    target = None
    index = 1
    while index < len(parts):
        token = parts[index]
        if token in ("-p", "-l", "-i", "-J", "-F", "-o"):
            if index + 1 >= len(parts):
                raise ValueError(f"SSH option {token} is missing a value")
            if token == "-p":
                port = int(parts[index + 1])
            index += 2
            continue
        if token.startswith("-p") and len(token) > 2:
            port = int(token[2:])
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        target = token
        index += 1

    if not target:
        raise ValueError("SSH command is missing user@host or host")
    user, host = target.rsplit("@", 1) if "@" in target else (None, target)
    if not host:
        raise ValueError("SSH command is missing host")
    return {"user": user, "host": host, "port": port, "target": target}


def execute_script(
    script_content: str,
    backend: str = "local",
    ssh_command: str | None = None,
    ssh_password: str | None = None,
    cci_state: str | None = None,
    cci_role: str = "build",
    success_label: str = "项目准备",
) -> str:
    fd, temp_script_path = tempfile.mkstemp(suffix=".sh")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(script_content)

    try:
        if backend == "cci":
            if not cci_state:
                return f"❌ 缺少 CCI 状态文件，无法执行云端{success_label}。"
            scripts_dir = (
                Path(__file__).resolve().parents[1]
                / "ar24-instance-manage"
                / "scripts"
            )
            sys.path.insert(0, str(scripts_dir))
            from cci.transport import execute_state_script

            result = execute_state_script(
                cci_state,
                script_content,
                role=cci_role,
                timeout=7200,
            )
            output_log = "\n".join(
                part for part in (result["stdout"], result["stderr"]) if part
            )
            if result["returncode"] == 0:
                return f"✅ CCI 云端{success_label}成功：\n\n【底层日志截取】\n{output_log[-2000:]}"
            return (
                f"❌ CCI 云端{success_label}失败 (Exit Code {result['returncode']})。"
                f"\n\n【完整错误日志】\n{output_log}"
            )
        if backend == "ssh":
            if not ssh_command:
                return f"❌ 缺少 SSH 连接信息，无法执行远端{success_label}。"
            ssh = parse_ssh_connection(ssh_command)
            password = ssh_password or ""
            if password:
                cmd = [
                    "sshpass",
                    "-e",
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-p",
                    str(ssh["port"]),
                    ssh["target"],
                    "bash -s",
                ]
                env = os.environ.copy()
                env["SSHPASS"] = password
            else:
                cmd = ["ssh", "-p", str(ssh["port"]), ssh["target"], "bash -s"]
                env = None
            result = subprocess.run(
                cmd,
                input=script_content,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=7200,
                **({"env": env} if env is not None else {}),
            )
            output_log = result.stdout
            if result.returncode == 0:
                return f"✅ 远端{success_label}成功：\n\n【底层日志截取】\n{output_log[-2000:]}"
            return f"❌ 远端{success_label}失败 (Exit Code {result.returncode})。\n\n【完整错误日志】\n{output_log}"

        result = subprocess.run(
            ["bash", temp_script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=7200,
        )
        output_log = result.stdout
        if result.returncode == 0:
            return f"✅ 本地{success_label}成功：\n\n【底层日志截取】\n{output_log[-2000:]}"
        return f"❌ 本地{success_label}失败 (Exit Code {result.returncode})。\n\n【完整错误日志】\n{output_log}"
    except subprocess.TimeoutExpired:
        return f"❌ {success_label}执行超时（超过 2 小时）。"
    except Exception as exc:
        return f"❌ 系统级执行错误: {exc}"
    finally:
        try:
            os.remove(temp_script_path)
        except FileNotFoundError:
            pass
