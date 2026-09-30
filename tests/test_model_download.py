"""Runs the real batch download script locally against a Range-capable HTTP fixture server."""
import asyncio
import hashlib
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ghm.executors.base import CommandResult
from ghm.model_download import BATCH_SCRIPT, download_many, failed_names, snapshot_items

FILES = {f'/f{i}.bin': bytes([i]) * (3*1024*1024 + i*1000) for i in range(1, 5)}


class Handler(BaseHTTPRequestHandler):
    active = 0
    peak = 0
    lock = threading.Lock()
    ignore_range = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = FILES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        start = 0
        header = self.headers.get('Range')
        if header and not Handler.ignore_range:
            start = int(header.removeprefix('bytes=').rstrip('-'))
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{len(body)-1}/{len(body)}')
        else:
            self.send_response(200)
        self.send_header('Content-Length', str(len(body)-start))
        self.end_headers()
        with Handler.lock:
            Handler.active += 1
            Handler.peak = max(Handler.peak, Handler.active)
        try:
            for offset in range(start, len(body), 256*1024):
                self.wfile.write(body[offset:offset+256*1024])
                time.sleep(0.01)
        finally:
            with Handler.lock:
                Handler.active -= 1


@pytest.fixture
def server():
    Handler.peak = Handler.active = 0
    Handler.ignore_range = False
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()


def item(base, root, path, name=None, digest=None, algorithm='sha256'):
    body = FILES[path]
    if digest is None:
        digest = (hashlib.sha256(body).hexdigest() if algorithm == 'sha256' else
                  hashlib.sha1(f'blob {len(body)}\0'.encode() + body).hexdigest())
    return {'name': name or path.strip('/'), 'target': str(root / 'models' / path.strip('/')),
            'url': base + path, 'digest': digest, 'algorithm': algorithm,
            'size_bytes': len(body), 'auth': False}


def run(root, items, workers=4):
    payload = json.dumps({'roots': [str(root / 'models')], 'items': items, 'workers': workers, 'token': None,
                          'progress_bytes': 1024*1024})
    return subprocess.run([sys.executable, '-c', BATCH_SCRIPT], input=payload, capture_output=True,
                          text=True, timeout=60, check=False)


def test_downloads_in_parallel_and_verifies_while_streaming(server, tmp_path):
    items = [item(server, tmp_path, p) for p in FILES]
    items[1]['algorithm'] = 'git-sha1'
    body = FILES['/f2.bin']
    items[1]['digest'] = hashlib.sha1(f'blob {len(body)}\0'.encode() + body).hexdigest()
    result = run(tmp_path, items)
    assert result.returncode == 0, result.stdout + result.stderr
    assert Handler.peak >= 2
    for path, body in FILES.items():
        assert (tmp_path / 'models' / path.strip('/')).read_bytes() == body
    assert result.stdout.count('Model ready: ') == 4
    assert 'Downloaded bytes: ' in result.stdout and ' of ' in result.stdout
    assert not list((tmp_path / 'models').glob('*.part'))


def test_resumes_partial_and_keeps_verified_files(server, tmp_path):
    items = [item(server, tmp_path, '/f1.bin'), item(server, tmp_path, '/f2.bin')]
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'f1.bin').write_bytes(FILES['/f1.bin'])
    (models / 'f2.bin.part').write_bytes(FILES['/f2.bin'][:1024*1024])
    result = run(tmp_path, items)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (models / 'f2.bin').read_bytes() == FILES['/f2.bin']
    assert Handler.peak == 1  # only the partial file was requested


def test_server_ignoring_range_restarts_cleanly(server, tmp_path):
    Handler.ignore_range = True
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'f3.bin.part').write_bytes(FILES['/f3.bin'][:500_000])
    result = run(tmp_path, [item(server, tmp_path, '/f3.bin')])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (models / 'f3.bin').read_bytes() == FILES['/f3.bin']


def test_checksum_mismatch_quarantines_and_reports_only_names(server, tmp_path):
    bad = item(server, tmp_path, '/f1.bin', digest='0'*64)
    good = item(server, tmp_path, '/f4.bin')
    result = run(tmp_path, [bad, good], workers=1)
    assert result.returncode == 1
    assert failed_names(result.stdout) == ['f1.bin']
    models = tmp_path / 'models'
    assert not (models / 'f1.bin').exists()
    assert list(models.glob('f1.bin.part.invalid-*'))


def test_existing_file_with_wrong_checksum_is_never_overwritten(server, tmp_path):
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'f1.bin').write_bytes(b'user data')
    result = run(tmp_path, [item(server, tmp_path, '/f1.bin')])
    assert result.returncode == 1
    assert (models / 'f1.bin').read_bytes() == b'user data'
    assert 'different checksum' in result.stdout


def test_destination_outside_roots_is_rejected(server, tmp_path):
    escaped = item(server, tmp_path, '/f1.bin')
    escaped['target'] = str(tmp_path / 'elsewhere' / 'f1.bin')
    result = run(tmp_path, [escaped])
    assert result.returncode == 1
    assert not (tmp_path / 'elsewhere' / 'f1.bin').exists()


def test_insufficient_space_downloads_nothing(server, tmp_path):
    huge = item(server, tmp_path, '/f1.bin')
    huge['size_bytes'] = 1 << 60
    result = run(tmp_path, [huge])
    assert result.returncode == 1
    assert 'Insufficient free space' in result.stdout
    assert Handler.peak == 0


def test_wrapper_surfaces_names_and_keeps_token_off_argv():
    seen = {}

    class Executor:
        async def run_input(self, command, data, timeout=60, on_output=None):
            seen['command'], seen['data'] = command, json.loads(data)
            return CommandResult(1, 'Model failed: wan-high (Checksum mismatch)\nhf_secret https://x?sig=1\n', '')

    items = [{'name': 'wan-high', 'target': '/ComfyUI/models/x', 'url': 'https://huggingface.co/a/b',
              'digest': 'a'*64, 'algorithm': 'sha256', 'size_bytes': 1, 'auth': True}]
    with pytest.raises(ValueError) as error:
        asyncio.run(download_many(Executor(), items, ['/ComfyUI/models'], 'hf_secret', 60))
    assert str(error.value).startswith('wan-high: ')
    assert 'hf_secret' not in str(error.value) and 'sig=' not in str(error.value)
    assert 'hf_secret' not in seen['command'] and seen['data']['token'] == 'hf_secret'


def test_snapshot_items_match_service_layout():
    items = snapshot_items([{'name': 'vieneu', 'repo': 'org/VieNeu-TTS', 'revision': 'a'*40,
                             'files': [{'filename': 'sub/config.json', 'size_bytes': 10,
                                        'digest': 'b'*40, 'algorithm': 'git-sha1'}]}], '/workspace/historia')
    assert items[0]['target'] == '/workspace/historia/service-models/org/VieNeu-TTS/sub/config.json'
    assert items[0]['url'] == 'https://huggingface.co/org/VieNeu-TTS/resolve/' + 'a'*40 + '/sub/config.json'
    with pytest.raises(ValueError):
        snapshot_items([{'name': 'x', 'repo': 'org/x', 'revision': 'a'*40,
                         'files': [{'filename': '../escape', 'size_bytes': 1, 'digest': 'b'*40,
                                    'algorithm': 'git-sha1'}]}], '/r')


def run_cached(root, items, **extra):
    payload = json.dumps({'roots': [str(root / 'models')], 'items': items, 'workers': 2, 'token': None,
                          'cache': str(root / '.historia-verified.json'), **extra})
    return subprocess.run([sys.executable, '-c', BATCH_SCRIPT], input=payload, capture_output=True,
                          text=True, timeout=60, check=False)


def test_verified_cache_skips_rehash_until_file_changes(server, tmp_path):
    items = [item(server, tmp_path, '/f1.bin')]
    assert run_cached(tmp_path, items).returncode == 0
    cache = json.loads((tmp_path / '.historia-verified.json').read_text())
    target = tmp_path / 'models' / 'f1.bin'
    assert cache[str(target.resolve())]['digest'] == items[0]['digest']
    # Same size + mtime: trusted without reading (a lying cache entry proves it was not re-hashed).
    cache[str(target.resolve())]['digest'] = 'f' * 64
    (tmp_path / '.historia-verified.json').write_text(json.dumps(cache))
    items[0]['digest'] = 'f' * 64
    assert run_cached(tmp_path, items).returncode == 0
    # recheck ignores the cache and catches the mismatch.
    assert run_cached(tmp_path, items, recheck=True).returncode == 1
    # A modified file (new mtime) is re-hashed even without recheck.
    items[0]['digest'] = hashlib.sha256(FILES['/f1.bin']).hexdigest()
    target.write_bytes(b'x' * len(FILES['/f1.bin']))
    result = run_cached(tmp_path, items)
    assert result.returncode == 1 and 'different checksum' in result.stdout


def test_downloader_writes_and_removes_pidfile(server, tmp_path):
    pidfile = tmp_path / 'dl.pid'
    assert run_cached(tmp_path, [item(server, tmp_path, '/f2.bin')], pidfile=str(pidfile)).returncode == 0
    assert not pidfile.exists()


def test_stop_remote_downloader_kills_only_numeric_pid():
    from studio.installer import stop_remote_downloader
    seen = []

    class Recorder:
        async def run(self, command, timeout=None):
            seen.append(command)
            raise ConnectionError('gone')  # must be swallowed
    asyncio.run(stop_remote_downloader(Recorder(), '/w/.historia-download.pid'))
    assert "*[!0-9]*" in seen[0] and 'kill "$pid"' in seen[0]


def test_inventory_trusts_cache_and_recheck_ignores_it(tmp_path):
    from studio.install_checks import INVENTORY
    from ghm.model_download import CACHE_HELPERS
    root = tmp_path / 'root'
    model = root / 'ComfyUI' / 'models' / 'a.bin'
    model.parent.mkdir(parents=True)
    model.write_bytes(b'abc')
    st = model.stat()
    (root / '.historia-verified.json').write_text(json.dumps(
        {str(model.resolve()): {'digest': 'd' * 64, 'stat': [st.st_size, st.st_mtime_ns]}}))

    def state(recheck):
        payload = {'root': str(root), 'comfy': str(root / 'ComfyUI'), 'port': 1, 'cache': '.historia-verified.json',
                   'recheck': recheck, 'files': [{'name': 'a', 'area': 'models', 'relative': 'a.bin',
                                                  'size_bytes': 3, 'digest': 'd' * 64, 'algorithm': 'sha256'}]}
        out = subprocess.run([sys.executable, '-c', INVENTORY.replace('__CACHE__', CACHE_HELPERS)],
                             input=json.dumps(payload), capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)['files'][0]['state']
    assert state(False) == 'valid'
    assert state(True) == 'conflict'
