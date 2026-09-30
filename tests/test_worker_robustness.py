"""Worker survives database hiccups; hot paths don't re-read unchanged media; list payload stays small."""
import asyncio
import os
import sqlite3

from fastapi.testclient import TestClient

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from studio import media
from studio.jobs import summarize_job


def make_app(tmp_path):
    return create_app(Settings(database_url=f"sqlite:///{tmp_path / 'db'}", studio_root=tmp_path / 'data'),
                      SecretStore('robust'), lambda h, s: FakeExecutor())


def test_digest_is_cached_until_file_changes(tmp_path, monkeypatch):
    path = tmp_path / 'clip.bin'
    path.write_bytes(b'a' * 1000)
    first = media.digest(path)
    reads = []
    real_open = open
    monkeypatch.setattr('builtins.open', lambda *a, **k: reads.append(a[0]) or real_open(*a, **k))
    assert media.digest(path) == first and not reads  # unchanged: no re-read
    path.write_bytes(b'b' * 1000)
    os.utime(path, ns=(1, 1))
    assert media.digest(path) != first and reads  # changed content/mtime: re-hashed


def test_probe_cache_returns_copies(tmp_path, monkeypatch):
    path = tmp_path / 'a.wav'
    path.write_bytes(b'x')
    calls = []
    monkeypatch.setattr(media, '_probe', lambda p: calls.append(p) or {'duration': 3.0})
    first = media.probe(path)
    first['duration'] = 99
    assert media.probe(path) == {'duration': 3.0} and len(calls) == 1


def test_wal_mode_enabled(tmp_path):
    TestClient(make_app(tmp_path)).get('/api/hosts')
    assert sqlite3.connect(tmp_path / 'db').execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


def test_dispatch_error_does_not_kill_worker(tmp_path, monkeypatch):
    jobs = make_app(tmp_path).state.studio_jobs
    calls = {'n': 0}

    def flaky():
        calls['n'] += 1
        if calls['n'] == 1:
            raise sqlite3.OperationalError('database is locked')
        raise asyncio.CancelledError  # stop the loop after proving it survived
    monkeypatch.setattr(jobs, 'dispatch', flaky)
    monkeypatch.setattr(jobs.runs, 'tick', lambda: None)

    async def fast_sleep(_):
        return None
    monkeypatch.setattr('studio.jobs.asyncio.sleep', fast_sleep)
    try:
        asyncio.run(jobs.loop())
    except asyncio.CancelledError:
        pass
    assert calls['n'] == 2


def test_job_list_summary_drops_heavy_snapshot():
    row = {'id': 'j', 'result': {'clip_ids': ['c']}, 'snapshot': {
        'production_run_id': 'r', 'scene': {'id': 's', 'title': 'Cảnh 1', 'narration': 'x' * 5000},
        'project': {'id': 'p', 'title': 'Phim', 'sources': ['y' * 200000]}}}
    slim = summarize_job(row)
    assert slim['snapshot'] == {'production_run_id': 'r', 'scene': {'id': 's', 'title': 'Cảnh 1'},
                                'project': {'id': 'p', 'title': 'Phim'}}
    assert slim['result'] == {'clip_ids': ['c']} and len(str(slim)) < 300
