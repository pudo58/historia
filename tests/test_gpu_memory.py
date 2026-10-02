"""No paid inference: exercise lifecycle cleanup and shared-GPU admission offline."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.models import Host, PreflightSnapshot
from ghm.security import SecretStore
from studio.backend import ReconcileRequired, RemoteBackend
from studio.gpu_memory import process_limit
from studio.jobs import PauseAtBoundary
from studio.models import Job, JobEvent
from studio.schemas import ProjectInput


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['completed', 'paused', 'failed'])
async def test_job_boundary_releases_once_after_all_shots(outcome):
    calls, logs = [], []
    def response(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/queue':
            return httpx.Response(200, json={'queue_running': [], 'queue_pending': []})
        if request.url.path == '/free':
            assert json.loads(request.content) == {'unload_models': True, 'free_memory': True}
            return httpx.Response(200)
        return httpx.Response(200, json={'devices': [{'torch_vram_total': 0, 'vram_free': 1024}]})
    backend = RemoteBackend(None, None)
    job = SimpleNamespace(id=str(uuid4()), host_id='a')
    async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(response)) as client:
        @asynccontextmanager
        async def connection(_):
            yield None, client
        backend.connection = connection
        async def render():
            async with backend.generation_session(job):
                for _ in range(2):
                    # Nested sessions reuse the same connection and keep warm weights.
                    async with backend.generation_session(job), backend.generation_connection(job) as (_, state):
                        state.update(remote_settled=True, log=logs.append)
                    assert ('POST', '/free') not in calls
                if outcome == 'paused':
                    raise PauseAtBoundary()
                if outcome == 'failed':
                    raise ValueError('original render error')
        if outcome == 'completed':
            await render()
        else:
            with pytest.raises(PauseAtBoundary if outcome == 'paused' else ValueError):
                await render()
    assert calls.count(('POST', '/free')) == 1
    assert any('0 MiB' in message for message in logs)
    assert backend._generation.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['running', 'pending', 'unknown', 'ambiguous'])
async def test_cleanup_never_writes_when_busy_or_remote_state_unknown(blocked):
    calls = []
    def response(request):
        calls.append(request.method)
        queue = {'queue_running': [], 'queue_pending': []}
        if blocked in {'running', 'pending'}:
            queue['queue_' + blocked] = [[0, 'foreign']]
        if blocked == 'unknown':
            queue = {}
        return httpx.Response(200, json=queue)
    backend = RemoteBackend(None, None)
    job = SimpleNamespace(id=str(uuid4()), host_id='a')
    async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(response)) as client:
        @asynccontextmanager
        async def connection(_):
            yield None, client
        backend.connection = connection
        with pytest.raises(ReconcileRequired):
            async with backend.generation_session(job), backend.generation_connection(job) as (_, state):
                state['remote_settled'] = blocked != 'ambiguous'
                raise ReconcileRequired('original uncertainty')
    assert 'POST' not in calls


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_original_exception():
    def response(request):
        raise httpx.ReadError('secret transport diagnostic', request=request)
    backend = RemoteBackend(None, None)
    logs = []
    job = SimpleNamespace(id=str(uuid4()), host_id='a')
    async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(response)) as client:
        @asynccontextmanager
        async def connection(_):
            yield None, client
        backend.connection = connection
        with pytest.raises(ValueError, match='original error'):
            async with backend.generation_session(job), backend.generation_connection(job) as (_, state):
                state.update(remote_settled=True, log=logs.append)
                raise ValueError('original error')
    assert logs and all('secret' not in message for message in logs)


@pytest.mark.parametrize('vram,limit', [(None, 1), (float('nan'), 1), (40, 1), (80, 1), (96, 1), (140, 2), (192, 3)])
def test_conservative_capacity(vram, limit):
    assert process_limit(vram) == limit


@pytest.mark.parametrize('vram,expected', [(None, 2), (80, 2), (140, 3)])
def test_dispatch_counts_physical_gpu_and_keeps_other_gpus_parallel(tmp_path, monkeypatch, vram, expected):
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path / "db"}', studio_root=tmp_path / 'data'),
                     SecretStore('memory'), lambda h, s: FakeExecutor())
    service, jobs, hosts = app.state.studio, app.state.studio_jobs, app.state.host_service
    project = service.create_project(ProjectInput(title='Film', topic='Test'))
    ids, host_ids = [], []
    with service.sessions() as session:
        for gpu in (0, 0, 1):
            host = Host(label='lane', address='localhost', username='u', auth_kind='password', encrypted_secret='unused')
            session.add(host)
            session.flush()
            host_ids.append(host.id)
            if vram:
                session.add(PreflightSnapshot(host_id=host.id, status='pass', payload=json.dumps({
                    'status': 'pass', 'checks': [], 'gpu': {'name': 'A100', 'vram_gb': vram, 'driver_version': '580'}})))
            job = Job(project_id=project['id'], host_id=host.id, kind='keyframe', input_hash=str(gpu), snapshot={})
            session.add(job)
            session.flush()
            ids.append(job.id)
        session.commit()
    for i, host_id in enumerate(host_ids):
        hosts.save_setting(f'host_lane:{host_id}', json.dumps({'pod_id': 'pod', 'first': host_ids[0], 'index': i,
                                                            'gpu': 1 if i == 2 else 0}))
    release, started = asyncio.Event(), []
    async def execute(job):
        started.append(job.id)
        await release.wait()
    monkeypatch.setattr(jobs, 'execute', execute)
    async def scenario():
        assert jobs.dispatch() == expected
        await asyncio.sleep(0)
        assert ids[0] in started and ids[2] in started
        assert jobs.dispatch() == 0
        release.set()
        await asyncio.gather(*jobs.active.values())
        if expected == 2:
            assert jobs.dispatch() == 1
            await asyncio.gather(*jobs.active.values())
    asyncio.run(scenario())
    assert all(service.require(Job, id).status == 'completed' for id in ids)
    with service.sessions() as session:
        waits = list(session.query(JobEvent).filter(JobEvent.job_id == ids[1]))
    assert len(waits) == (1 if expected == 2 else 0)
