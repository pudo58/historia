"""Bounded streaming RPC over an authenticated, fingerprint-pinned SSH PTY."""
import asyncio
import base64
import hashlib
import json
import shlex
import zlib
from pathlib import Path
from uuid import uuid4

import httpx

from ghm.executors.base import CommandResult
from ghm.executors.terminal_agent import AGENT


async def rpc(executor, payload, timeout=60):
    nonce = 'HST' + uuid4().hex
    encoded = base64.b64encode(zlib.compress(AGENT.encode())).decode()
    boot = "import base64,zlib;exec(zlib.decompress(base64.b64decode(" + repr(encoded) + ")))"
    command = 'python3 -u -c ' + shlex.quote(boot) + ' ' + nonce + '\n'
    if len(command) > 4000:
        raise ValueError('Terminal bootstrap exceeds safe line length.')
    request = json.dumps(payload, ensure_ascii=True).encode() + b'\n'
    if len(request) > 47*1024*1024:
        raise ValueError('Một lần truyền terminal giới hạn 32 MiB dữ liệu. Giảm kích thước ảnh tham chiếu.')
    process = None
    try:
        async with asyncio.timeout(timeout):
            connection = await executor.connection()
            process = await connection.create_process(term_type='dumb', term_size=(160, 24))
            process.stdin.write(command)
            banner = ''
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise ConnectionError('SSH đóng trước khi terminal sẵn sàng.')
                banner += line
                if line.strip() == nonce + ':READY':
                    break
                if len(banner) > 100_000:
                    raise ConnectionError('Không bắt tay được terminal RPC.')
            # The remote tty is now raw with echo disabled. Never log this payload.
            for offset in range(0, len(request), 32768):
                process.stdin.write(request[offset:offset+32768].decode('ascii'))
                await process.stdin.drain()
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise ConnectionError('SSH gián đoạn trước khi nhận mã kết thúc; không tự chạy lại lệnh.')
                if len(line) > 100_000 or not line.startswith(nonce + ':'):
                    raise ConnectionError('Terminal trả frame không hợp lệ.')
                _, kind, value = line.rstrip('\r\n').split(':', 2)
                data = base64.b64decode(value, validate=True)
                if kind == 'ERROR':
                    raise ConnectionError('Terminal RPC thất bại (' + data.decode('ascii', errors='replace') + ').')
                if kind not in {'HEAD', 'DATA', 'HASH', 'EXIT'}:
                    raise ConnectionError('Terminal trả loại frame không hợp lệ.')
                yield kind, data
                if kind == 'EXIT':
                    return
    finally:
        if process is not None:
            process.close()
            try:
                await asyncio.wait_for(process.wait_closed(), 6)
            except TimeoutError:
                pass


async def run(executor, command, timeout, on_output=None, input_data=None):
    output = bytearray()
    frames = rpc(executor, {'op': 'run', 'command': command,
                           'input': base64.b64encode((input_data or '').encode()).decode()}, timeout)
    try:
        async for kind, value in frames:
            if kind == 'DATA':
                output.extend(value)
                if len(output) > 2_000_000:
                    del output[:-2_000_000]
                if on_output:
                    on_output('stdout', value.decode('utf-8', errors='replace'))
            elif kind == 'EXIT':
                return CommandResult(int(value), output.decode('utf-8', errors='replace'), '')
        raise ConnectionError('Thiếu kết quả lệnh SSH.')
    finally:
        await frames.aclose()


async def upload(executor, source, destination):
    path = Path(source)
    if path.stat().st_size > 32*1024*1024:
        raise ValueError('File upload qua Basic SSH giới hạn 32 MiB.')
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    verified = False
    frames = rpc(executor, {'op': 'write', 'path': destination,
                            'body': base64.b64encode(data).decode(), 'sha256': digest}, 300)
    try:
        async for kind, value in frames:
            if kind == 'HASH':
                verified = json.loads(value) == {'sha256': digest, 'size': len(data)}
            if kind == 'EXIT' and int(value):
                raise ValueError('Upload không hoàn tất.')
    finally:
        await frames.aclose()
    if not verified:
        raise ValueError('Upload thiếu checksum xác nhận.')


async def download(executor, source, destination):
    target = Path(destination)
    part = target.with_name(target.name + '.part')
    h, size, confirmed = hashlib.sha256(), 0, None
    frames = rpc(executor, {'op': 'read', 'path': source}, 1800)
    try:
        with part.open('wb') as handle:
            async for kind, value in frames:
                if kind == 'DATA':
                    size += len(value)
                    if size > 2_000_000_000:
                        raise ValueError('File tải về vượt 2 GB.')
                    h.update(value)
                    handle.write(value)
                elif kind == 'HASH':
                    confirmed = json.loads(value)
                elif kind == 'EXIT' and int(value):
                    raise ValueError('Download không hoàn tất.')
    finally:
        await frames.aclose()
    if confirmed != {'sha256': h.hexdigest(), 'size': size}:
        raise ValueError('Download sai checksum hoặc bị gián đoạn.')
    part.replace(target)


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, frames):
        self.frames = frames

    async def __aiter__(self):
        async for kind, value in self.frames:
            if kind == 'DATA':
                yield value
            elif kind == 'EXIT' and int(value):
                raise httpx.ReadError('Remote HTTP proxy failed.')

    async def aclose(self):
        await self.frames.aclose()


class TerminalHTTP(httpx.AsyncBaseTransport):
    def __init__(self, executor, port):
        self.executor, self.port = executor, port

    async def handle_async_request(self, request):
        if request.url.host != '127.0.0.1' or request.url.port != self.port:
            raise httpx.ConnectError('Only the selected remote loopback port is allowed.')
        body = await request.aread()
        if len(body) > 32*1024*1024:
            raise ValueError('Ảnh gửi ComfyUI qua Basic SSH giới hạn 32 MiB.')
        frames = rpc(self.executor, {'op': 'http', 'port': self.port, 'path': request.url.raw_path.decode('ascii'),
                     'method': request.method, 'headers': dict(request.headers),
                     'body': base64.b64encode(body).decode(), 'timeout': 60}, 1800)
        try:
            try:
                kind, data = await anext(frames)
            except ConnectionError as exc:
                # The remote agent could not open the loopback port (service not listening yet).
                # That is a plain HTTP connect failure, exactly like a refused direct tunnel, so
                # callers that wait for a service to come up retry it instead of losing the job.
                if '(URLError)' in str(exc) or '(ConnectionRefusedError)' in str(exc):
                    raise httpx.ConnectError('Dịch vụ trên Pod chưa mở cổng ' + str(self.port) + '.') from exc
                raise
            if kind != 'HEAD':
                raise httpx.RemoteProtocolError('Missing HTTP response header.')
            header = json.loads(data)
            headers = {k: v for k, v in header['headers'].items() if k.lower() not in {'transfer-encoding','connection'}}
            return httpx.Response(header['status'], headers=headers, stream=ResponseStream(frames))
        except BaseException:
            await frames.aclose()
            raise
