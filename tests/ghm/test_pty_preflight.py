import asyncio
import base64
import re

import asyncssh
import pytest

from ghm.executors.base import CommandResult
from ghm.executors.fake import FakeExecutor
from ghm.executors.pty_probe import PreflightTransportError, parse_frames, probe_script
from ghm.executors.ssh import SSHExecutor
from ghm.preflight import COMMANDS, collect, evaluate


def test_frames_ignore_terminal_echo_and_preserve_real_exit_code():
    nonce = 'GHM123'
    script = probe_script({'gpu': 'nvidia-smi'}, nonce)
    assert max(map(len, script.splitlines())) < 160
    assert 'GHM123:gpu:' not in script
    data = base64.b64encode(b'nvidia-smi: not found').decode()
    result = parse_frames(script + f'\x1b[0m\r\n{nonce}:gpu:127:{data}\r\n', nonce, {'gpu'})
    assert result['gpu'].rc == 127
    assert result['gpu'].stdout == 'nvidia-smi: not found'
    assert parse_frames(script + f'{nonce}:gpu:127:{data}', nonce, {'gpu'}) == {}
    with pytest.raises(PreflightTransportError):
        parse_frames(f'{nonce}:gpu:0:\n{nonce}:gpu:0:\n', nonce, {'gpu'})


def test_pty_error_with_zero_exit_cannot_be_dns_pass_or_missing_gpu():
    report = evaluate({key: CommandResult(0, "Error: Your SSH client doesn't support PTY\n", '') for key in COMMANDS})
    assert report.status == 'fail'
    assert report.gpu is None
    assert len(report.checks) == 1
    assert report.checks[0].name == 'SSH command execution'
    assert 'PTY' in report.checks[0].message


@pytest.mark.asyncio
async def test_fallback_only_for_explicit_pty_error():
    class TerminalFake(FakeExecutor):
        calls = 0
        async def run_preflight_pty(self, commands):
            self.calls += 1
            return {name: CommandResult(0, '', '') for name in commands}
    executor = TerminalFake(results={COMMANDS['os']: CommandResult(0, "Error: Your SSH client doesn't support PTY", '')})
    values = (await collect(executor)).values
    assert values['_transport'].stdout == 'basic_pty'
    assert executor.calls == 1
    assert executor.commands == [COMMANDS['os']]
    normal = TerminalFake()
    await collect(normal)
    assert normal.calls == 0


@pytest.mark.asyncio
async def test_real_ssh_pty_shell_rejects_exec_but_accepts_framed_probe():
    key = asyncssh.generate_private_key('ssh-ed25519')
    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False
    async def process_handler(process):
        if not process.term_type:
            process.stdout.write("Error: Your SSH client doesn't support PTY\n")
            process.exit(0)
            return
        process.stdout.write('Gateway banner\r\nroot@pod:/# ')
        lines = []
        while True:
            line = await process.stdin.readline()
            if not line:
                return
            process.stdout.write(line)  # terminal echo must not count as result
            lines.append(line)
            if line.strip().startswith('GHM_INPUT_'):
                break
        nonce = lines[-1].strip().removeprefix('GHM_INPUT_')
        decoded = base64.b64decode(''.join(lines[1:-1])).decode()
        names = re.findall(re.escape(nonce) + r':([a-z_]+):', decoded)
        for name in names:
            value = 'NVIDIA RTX PRO 4500 Blackwell, 32623, 580.126.16, 12.0' if name == 'gpu' else ''
            payload = base64.b64encode(value.encode()).decode()
            process.stdout.write(f'\r\n{nonce}:{name}:0:{payload}\r\n')
        await process.stdin.read()
        process.exit(0)
    server = await asyncssh.listen('127.0.0.1', 0, server_factory=Server, process_factory=process_handler,
                                   server_host_keys=[key], line_editor=False)
    executor = SSHExecutor('127.0.0.1', server.get_port(), 'user', 'password', '', key.get_fingerprint('sha256'))
    try:
        result = await collect(executor)
        assert result.values['gpu'].stdout.startswith('NVIDIA RTX PRO 4500')
        assert evaluate(result.values).gpu.vram_gb == 31.86
    finally:
        await executor.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_pty_probe_timeout_closes_channel():
    class Reader:
        async def read(self, size):
            await asyncio.sleep(10)
    class Process:
        stdout = Reader()
        closed = False
        class Input:
            def write(self, data): pass
        stdin = Input()
        def close(self): self.closed = True
        async def wait_closed(self): pass
    process = Process()
    class Connection:
        def is_closed(self): return False
        async def create_process(self, **kwargs): return process
    executor = SSHExecutor('test', 22, 'user', 'password', '', 'pinned')
    executor._connection = Connection()
    with pytest.raises(PreflightTransportError, match='terminal SSH'):
        await executor.run_preflight_pty({'gpu': 'nvidia-smi'}, timeout=.03)
    assert process.closed
