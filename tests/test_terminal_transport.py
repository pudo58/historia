"""Protocol tests only. Real Runpod checks are documented separately."""
import asyncio
import base64
import hashlib
import json

import httpx
import pytest

from ghm.executors import terminal
from ghm.schemas import HostOptions


class Process:
    def __init__(self, frames):
        self.frames = frames
        self.stdin = self.stdout = self
        self.lines = asyncio.Queue()
        self.boot = None
        self.payload = None
        self.buffer = ''
        self.closed = False

    def write(self, text):
        if self.boot is None:
            self.boot = text
            self.nonce = text.strip().split()[-1]
            self.lines.put_nowait('shell banner\n')
            self.lines.put_nowait(self.nonce + ':READY\n')
            return
        self.buffer += text
        if self.buffer.endswith('\n'):
            self.payload = json.loads(self.buffer)
            for kind, value in self.frames:
                self.lines.put_nowait(self.nonce + ':' + kind + ':' + base64.b64encode(value).decode() + '\n')
            self.lines.put_nowait('')

    async def readline(self):
        return await self.lines.get()

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class Executor:
    def __init__(self, frames):
        self.process = Process(frames)

    async def connection(self):
        return self

    async def create_process(self, **kwargs):
        assert kwargs['term_type'] == 'dumb'
        return self.process


def test_rpc_real_exit_code_unicode_and_stdin_not_bootstrap():
    async def check():
        e = Executor([('DATA', 'Xin chào'.encode()), ('EXIT', b'7')])
        result = await terminal.run(e, 'exit 7', 2, input_data='private-token')
        assert result.rc == 7 and result.stdout == 'Xin chào'
        assert e.process.closed
        assert len(e.process.boot) < 4000
        assert 'private-token' not in e.process.boot
        assert base64.b64decode(e.process.payload['input']) == b'private-token'
    asyncio.run(check())


@pytest.mark.parametrize('frames', [[('DATA', b'partial')], [('ERROR', b'PermissionError')], [('OTHER', b'x')]])
def test_rpc_disconnect_or_invalid_frames_never_success(frames):
    async def check():
        e = Executor(frames)
        with pytest.raises(ConnectionError):
            await terminal.run(e, 'anything', 2)
        assert e.process.closed
    asyncio.run(check())


def test_binary_download_verified_and_corruption_preserves_target(tmp_path):
    async def check():
        content = bytes(range(256))*400
        target = tmp_path/'clip.bin'
        digest = {'size':len(content), 'sha256':hashlib.sha256(content).hexdigest()}
        e = Executor([('DATA', content[:49152]), ('DATA', content[49152:]), ('HASH', json.dumps(digest).encode()), ('EXIT', b'0')])
        await terminal.download(e, '/remote/clip', target)
        assert target.read_bytes() == content and e.process.closed
        bad = Executor([('DATA', b'corrupt'), ('HASH', json.dumps(digest).encode()), ('EXIT', b'0')])
        with pytest.raises(ValueError, match='checksum'):
            await terminal.download(bad, '/remote/clip', target)
        assert target.read_bytes() == content and bad.process.closed
    asyncio.run(check())


def test_upload_requires_checksum_and_exit(tmp_path):
    source = tmp_path/'input.bin'
    source.write_bytes(b'abc')
    async def check():
        e = Executor([('HASH', json.dumps({'size':3, 'sha256':hashlib.sha256(b'abc').hexdigest()}).encode()), ('EXIT', b'0')])
        await terminal.upload(e, source, '/tmp/example')
        assert e.process.closed
        missing = Executor([('EXIT', b'0')])
        with pytest.raises(ValueError, match='checksum'):
            await terminal.upload(missing, source, '/tmp/example')
        assert missing.process.closed
    asyncio.run(check())


def test_http_preserves_status_body_and_restricts_remote_target():
    async def check():
        body = b'not-found\x00\xff'
        e = Executor([('HEAD', json.dumps({'status':404,'headers':{'content-type':'application/octet-stream'}}).encode()), ('DATA', body), ('EXIT', b'0')])
        async with httpx.AsyncClient(transport=terminal.TerminalHTTP(e, 8188)) as client:
            response = await client.post('http://127.0.0.1:8188/upload/image', content=b'\x00\xff')
            assert response.status_code == 404 and response.content == body
            assert base64.b64decode(e.process.payload['body']) == b'\x00\xff'
            assert e.process.closed
            with pytest.raises(httpx.ConnectError):
                await client.get('http://example.com/credentials')
    asyncio.run(check())


def test_cancel_closes_channel_without_retry():
    async def check():
        e = Executor([])
        async def blocked():
            await asyncio.Event().wait()
        e.process.readline = blocked
        task = asyncio.create_task(terminal.run(e, 'long-command', 30))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert e.process.closed
    asyncio.run(check())


def test_custom_comfy_path_requires_explicit_adopt():
    options = HostOptions(root='/workspace/historia', adopt_existing=True, comfy_path='/ComfyUI')
    assert options.comfy_root == '/ComfyUI'
    with pytest.raises(ValueError):
        HostOptions(root='/workspace/historia', comfy_path='/ComfyUI')
    with pytest.raises(ValueError):
        HostOptions(adopt_existing=True, comfy_path='/tmp/../etc')


def test_http_refused_loopback_port_is_a_connect_error_not_a_lost_connection():
    """Basic SSH: ComfyUI not listening yet must look like a refused connection so installers retry."""
    async def check():
        e = Executor([('ERROR', b'URLError')])
        async with httpx.AsyncClient(transport=terminal.TerminalHTTP(e, 8190)) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get('http://127.0.0.1:8190/system_stats')
        assert e.process.closed
        other = Executor([('ERROR', b'ValueError')])
        async with httpx.AsyncClient(transport=terminal.TerminalHTTP(other, 8190)) as client:
            with pytest.raises(ConnectionError):
                await client.get('http://127.0.0.1:8190/system_stats')
    asyncio.run(check())
