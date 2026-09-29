"""SSH transport: authenticate only after the pinned server key has been verified."""
import asyncio
import hmac
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import asyncssh

from ghm.executors.base import CommandResult
from ghm.executors.pty_probe import (
    PreflightTransportError,
    PTYRequiredError,
    parse_frames,
    probe_script,
    requires_pty,
)


class _PinnedClient(asyncssh.SSHClient):
    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        return hmac.compare_digest(key.get_fingerprint("sha256"), self.fingerprint)


def _load_private_key(path: str):
    """Read only the private key file; a sidecar .pub that is stale must not block a working key."""
    from pathlib import Path
    try:
        return asyncssh.import_private_key(Path(path).expanduser().read_bytes())
    except FileNotFoundError:
        raise ValueError("Không tìm thấy file khóa SSH riêng: " + path) from None
    except asyncssh.KeyImportError as exc:
        raise ValueError("Không đọc được khóa SSH riêng (khóa có passphrase hoặc sai định dạng): " + str(exc)) from exc


class SSHExecutor:
    def __init__(self, host: str, port: int, username: str, auth_kind: str,
                 secret: str, pinned_fingerprint: str | None = None) -> None:
        self.host, self.port, self.username = host, port, username
        self.auth_kind, self.secret = auth_kind, secret
        self.pinned_fingerprint = pinned_fingerprint
        self._connection = None
        self.terminal_only = host.lower().rstrip('.') == 'ssh.runpod.io'

    def _options(self) -> dict:
        options = {'host': self.host, 'port': self.port, 'username': self.username,
                   'config': None, 'agent_path': None, 'connect_timeout': 15, 'login_timeout': 20,
                   'keepalive_interval': 15, 'keepalive_count_max': 3}
        if self.auth_kind == "password":
            options.update(password=self.secret, client_keys=[])
        else:
            options["client_keys"] = [_load_private_key(self.secret)]
        return options

    async def inspect_host_key(self) -> str:
        # Key exchange only: do not transmit a username/password/private-key signature.
        # Explicit options: never load default ~/.ssh keys for a host-key-only handshake (a stale .pub aborts it).
        key = await asyncio.wait_for(
            asyncssh.get_server_host_key(self.host, self.port, options=asyncssh.SSHClientConnectionOptions(
                config=None, agent_path=None, client_keys=None)), timeout=15
        )
        if key is None:
            raise RuntimeError("The server did not present an SSH host key.")
        return key.get_fingerprint("sha256")

    async def _connect(self):
        if not self.pinned_fingerprint:
            raise RuntimeError("Inspect and confirm the SSH fingerprint before connecting.")
        self._connection = await asyncssh.connect(
            known_hosts=(), client_factory=lambda: _PinnedClient(self.pinned_fingerprint),
            **self._options()
        )
        return self._connection

    async def connection(self):
        if self._connection is None or self._connection.is_closed():
            self._connection = None
            return await self._connect()
        return self._connection

    async def run(self, command: str, timeout: float | None = None,
                  on_output: Callable[[str, str], None] | None = None,
                  input_data: str | None = None) -> CommandResult:
        if self.terminal_only:
            from ghm.executors.terminal import run
            return await run(self, command, timeout, on_output, input_data)
        connection = await self.connection()
        process = await connection.create_process(command, input=input_data)
        output = {"stdout": [], "stderr": []}

        async def drain(reader, channel):
            while True:
                # Bounded chunks also support progress output without newlines.
                chunk = await reader.read(2048)
                if not chunk:
                    break
                output[channel].append(chunk)
                if sum(map(len, output[channel])) > 131072:
                    output[channel] = output[channel][-32:]
                if on_output:
                    on_output(channel, chunk)

        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(drain(process.stdout, "stdout"), drain(process.stderr, "stderr"))
                await process.wait_closed()
        except BaseException:
            process.terminate()
            process.close()
            raise
        result = CommandResult(
            process.exit_status if process.exit_status is not None else -1,
            "".join(output["stdout"]), "".join(output["stderr"])
        )
        if requires_pty(result):
            raise PTYRequiredError("Gateway SSH yêu cầu terminal PTY, không thực hiện lệnh exec. Preflight hỗ trợ PTY; cài đặt và truyền file cần Full SSH.")
        return result

    async def stream(self, command: str, timeout: float | None = None) -> AsyncIterator[str]:
        queue = asyncio.Queue()
        task = asyncio.create_task(self.run(command, timeout, lambda channel, text: queue.put_nowait(text)))
        try:
            while not task.done() or not queue.empty():
                try:
                    yield await asyncio.wait_for(queue.get(), 0.1)
                except TimeoutError:
                    pass
            result = await task
            if result.rc:
                raise RuntimeError(f"Remote command exited with status {result.rc}.")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run_preflight_pty(self, commands: dict[str, str], timeout: float = 90) -> dict[str, CommandResult]:
        """Only preflight uses interactive PTY; never retry mutating recipes in a shell."""
        nonce = "GHM" + uuid4().hex
        process = None
        try:
            async with asyncio.timeout(timeout):
                connection = await self.connection()
                process = await connection.create_process(term_type="dumb", term_size=(160, 24))
                process.stdin.write(probe_script(commands, nonce))
                buffer = ""
                while True:
                    chunk = await process.stdout.read(8192)
                    if not chunk:
                        raise PreflightTransportError("SSH đã đóng trước khi kiểm tra xong. Chưa thể xác định GPU.")
                    buffer += chunk
                    if len(buffer) > 2_000_000:
                        raise PreflightTransportError("Phản hồi terminal vượt giới hạn; chưa thể xác định GPU.")
                    results = parse_frames(buffer, nonce, set(commands))
                    if set(results) == set(commands):
                        return results
        except TimeoutError as exc:
            raise PreflightTransportError("Hết thời gian chờ terminal SSH. Chưa thể xác định GPU; không phải kết luận thiếu GPU.") from exc
        finally:
            if process is not None:
                process.close()
                try:
                    await asyncio.wait_for(process.wait_closed(), 5)
                except TimeoutError:
                    pass

    async def upload(self, source: str, destination: str) -> None:
        if self.terminal_only:
            from ghm.executors.terminal import upload
            return await upload(self, source, destination)
        connection = await self.connection()
        await asyncssh.scp(source, (connection, destination))

    async def check_install_transport(self) -> None:
        """Read-only check for file transfer before a large install is offered."""
        if self.terminal_only:
            result = await self.run_input("python3 -c 'import sys; print(sys.stdin.read())'", 'TERMINAL_OK', timeout=30)
            if result.rc or result.stdout.strip() != 'TERMINAL_OK':
                raise ValueError('Không xác nhận được truyền dữ liệu qua terminal SSH.')
            return
        result = await self.run('command -v scp', timeout=15)
        if result.rc or not result.stdout.strip():
            raise ValueError('Full SSH cần lệnh scp trên máy GPU để trao đổi worker/output.')
        connection = await self.connection()
        try:
            async with asyncio.timeout(20):
                async with connection.start_sftp_client() as sftp:
                    await sftp.stat('.')
        except (asyncssh.Error, TimeoutError):
            raise ValueError('SSH không hỗ trợ SFTP. Dùng Full SSH trực tiếp trước khi cài bộ AI.') from None

    async def download(self, source: str, destination: str) -> None:
        if self.terminal_only:
            from ghm.executors.terminal import download
            return await download(self, source, destination)
        connection = await self.connection()
        await asyncssh.scp((connection, source), destination)

    async def run_input(self, command: str, data: str, timeout: float = 60, on_output=None) -> CommandResult:
        """Pass sensitive configuration via stdin, never shell arguments or recipe logs."""
        return await self.run(command, timeout=timeout, on_output=on_output, input_data=data)

    async def forward_local_port(self, local_port: int, remote_port: int):
        connection = await self.connection()
        return await connection.forward_local_port("127.0.0.1", local_port, "127.0.0.1", remote_port)

    async def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            await self._connection.wait_closed()
            self._connection = None
