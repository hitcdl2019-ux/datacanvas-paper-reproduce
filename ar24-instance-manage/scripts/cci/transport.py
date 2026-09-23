from __future__ import annotations

import json
import os
import posixpath
import stat
from pathlib import Path
from typing import Any

from .client import AlayaNewCciClient
from .provider import CciCloudProvider, SshConnectionInfo
from .redaction import redact_text
from .state import CciStateStore


class CciTransportError(RuntimeError):
    pass


def _paramiko() -> Any:
    try:
        import paramiko
    except ImportError as exc:
        raise CciTransportError(
            "CCI 分支缺少 Paramiko；请安装 ar24-instance-manage/requirements-cci.txt"
        ) from exc
    return paramiko


class CciTransport:
    def __init__(
        self,
        provider: CciCloudProvider,
        connection: SshConnectionInfo,
        *,
        connect_timeout: float = 30.0,
    ) -> None:
        self.provider = provider
        self.connection = connection
        self.connect_timeout = connect_timeout

    @classmethod
    def from_state(
        cls,
        state_path: str | Path,
        *,
        role: str,
        provider: CciCloudProvider | None = None,
    ) -> "CciTransport":
        store = CciStateStore(state_path)
        state = store.load()
        resource = state.get("resources", {}).get(role)
        if not resource or not resource.get("instance_id"):
            raise CciTransportError(f"CCI {role} 实例尚未创建")
        provider = provider or CciCloudProvider(AlayaNewCciClient.from_env())
        connection = provider.get_ssh_connection(str(resource["instance_id"]))
        return cls(provider, connection)

    def _connect(self) -> Any:
        paramiko = _paramiko()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.connection.host,
                port=self.connection.port,
                username=self.connection.user,
                password=self.connection.password,
                timeout=self.connect_timeout,
                banner_timeout=self.connect_timeout,
                auth_timeout=self.connect_timeout,
                look_for_keys=self.connection.password is None,
                allow_agent=self.connection.password is None,
            )
            active_transport = client.get_transport()
            if active_transport is not None:
                active_transport.set_keepalive(30)
        except Exception as exc:
            client.close()
            raise CciTransportError(
                redact_text(
                    f"CCI SSH 连接失败: {exc}",
                    secrets=[self.connection.password or ""],
                )
            ) from exc
        return client

    def execute_script(
        self,
        script: str,
        *,
        timeout: float | None = None,
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        client = self._connect()
        try:
            stdin, stdout, stderr = client.exec_command(
                "bash -s",
                timeout=timeout,
                environment=environment or {},
            )
            stdin.write(script)
            if not script.endswith("\n"):
                stdin.write("\n")
            stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
        except Exception as exc:
            raise CciTransportError(
                redact_text(
                    f"CCI 远程命令失败: {exc}",
                    secrets=[self.connection.password or ""],
                )
            ) from exc
        finally:
            client.close()
        return {
            "returncode": exit_code,
            "stdout": redact_text(out, secrets=[self.connection.password or ""]),
            "stderr": redact_text(err, secrets=[self.connection.password or ""]),
        }

    def pull(self, remote_path: str, local_path: str | Path) -> dict[str, Any]:
        local = Path(local_path).expanduser().resolve()
        if local in {Path("/"), Path.home().resolve()}:
            raise CciTransportError("拒绝把 CCI 产物同步到根目录或用户主目录")
        client = self._connect()
        transferred: list[str] = []
        try:
            sftp = client.open_sftp()
            self._pull_entry(sftp, remote_path, local, transferred)
        except Exception as exc:
            raise CciTransportError(
                redact_text(
                    f"CCI SFTP 下载失败: {exc}",
                    secrets=[self.connection.password or ""],
                )
            ) from exc
        finally:
            client.close()
        return {
            "direction": "pull",
            "remote_path": remote_path,
            "local_path": str(local),
            "files": transferred,
        }

    def push(self, local_path: str | Path, remote_path: str) -> dict[str, Any]:
        local = Path(local_path).expanduser().resolve()
        if local in {Path("/"), Path.home().resolve()}:
            raise CciTransportError("拒绝把根目录或用户主目录作为 CCI 同步源")
        if not local.exists():
            raise CciTransportError(f"本地同步源不存在: {local}")
        client = self._connect()
        transferred: list[str] = []
        try:
            sftp = client.open_sftp()
            self._push_entry(sftp, local, remote_path, transferred)
        except Exception as exc:
            raise CciTransportError(
                redact_text(
                    f"CCI SFTP 上传失败: {exc}",
                    secrets=[self.connection.password or ""],
                )
            ) from exc
        finally:
            client.close()
        return {
            "direction": "push",
            "local_path": str(local),
            "remote_path": remote_path,
            "files": transferred,
        }

    def _pull_entry(
        self,
        sftp: Any,
        remote: str,
        local: Path,
        transferred: list[str],
    ) -> None:
        attrs = sftp.lstat(remote) if hasattr(sftp, "lstat") else sftp.stat(remote)
        if stat.S_ISLNK(attrs.st_mode):
            raise CciTransportError(f"拒绝通过 SFTP 跟随远端符号链接: {remote}")
        if stat.S_ISDIR(attrs.st_mode):
            local.mkdir(parents=True, exist_ok=True)
            for item in sftp.listdir_attr(remote):
                self._pull_entry(
                    sftp,
                    posixpath.join(remote, item.filename),
                    local / item.filename,
                    transferred,
                )
            return
        local.parent.mkdir(parents=True, exist_ok=True)
        temporary = local.with_name(f".{local.name}.cci-part")
        try:
            sftp.get(remote, str(temporary))
            os.replace(temporary, local)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        transferred.append(str(local))

    def _push_entry(
        self,
        sftp: Any,
        local: Path,
        remote: str,
        transferred: list[str],
    ) -> None:
        if local.is_symlink():
            raise CciTransportError(f"拒绝通过 SFTP 跟随本地符号链接: {local}")
        if local.is_dir():
            _sftp_mkdirs(sftp, remote)
            for item in local.iterdir():
                self._push_entry(
                    sftp,
                    item,
                    posixpath.join(remote, item.name),
                    transferred,
                )
            return
        _sftp_mkdirs(sftp, posixpath.dirname(remote))
        temporary = f"{remote}.cci-part"
        try:
            sftp.put(str(local), temporary)
            if hasattr(sftp, "posix_rename"):
                sftp.posix_rename(temporary, remote)
            else:
                try:
                    sftp.remove(remote)
                except OSError:
                    pass
                sftp.rename(temporary, remote)
        except Exception:
            try:
                sftp.remove(temporary)
            except OSError:
                pass
            raise
        transferred.append(remote)


def execute_state_script(
    state_path: str | Path,
    script: str,
    *,
    role: str = "build",
    timeout: float | None = None,
) -> dict[str, Any]:
    return CciTransport.from_state(state_path, role=role).execute_script(
        script,
        timeout=timeout,
    )


def parse_last_json_line(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise CciTransportError("CCI 远程输出中未找到 JSON 对象")


def _sftp_mkdirs(sftp: Any, remote: str) -> None:
    if remote in {"", "/"}:
        return
    parts = remote.split("/")
    current = "/" if remote.startswith("/") else ""
    for part in parts:
        if not part:
            continue
        current = posixpath.join(current, part)
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)
