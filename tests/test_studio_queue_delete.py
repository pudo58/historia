"""Local-only queue and deletion contracts; never start a real worker or SSH."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.models import HostRuntime, RecipeRun
from ghm.security import SecretStore
from studio.models import Job, JobEvent, Project, Source
from studio.schemas import ProjectInput, SceneInput, TextSourceInput


@pytest.fixture
def local(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'queue.db'}", studio_root=tmp_path / 'data'),
                     SecretStore('queue-test'), lambda h, s: FakeExecutor())
    client = TestClient(app)  # No lifespan: no durable worker or external calls.
    jobs, service = app.state.studio_jobs, app.state.studio
    host = client.post('/api/hosts', json={'label': 'GPU', 'address': 'gpu.example.test',
        'username': 'root', 'auth_kind': 'password', 'secret': 'secret'}).json()
    stored = jobs.hosts._require_host(host['id'])
    stored.pinned_fingerprint = 'SHA256:test'
    jobs.hosts._save(stored)
    project = service.create_project(ProjectInput(title='Film', topic='History', host_id=host['id']))
    return client, service, jobs, host['id'], project['id']


def add_job(local, status, result=None):
    _, service, _, host, project = local
    with service.sessions() as session:
        row = Job(project_id=project, host_id=host, kind='speech', status=status,
                  snapshot={}, input_hash='test', result=result or {})
        session.add(row)
        session.commit()
    return row.id


def test_abandon_preserves_evidence_fences_writes_and_unlocks(local):
    from studio.models import ProductionRun
    client, service, jobs, host, project = local
    scene = service.add_scene(project, SceneInput(title='Original', narration='Evidence', review_note='Reviewed'))
    snapshot = service.project(project)
    with service.sessions() as session:
        run = ProductionRun(project_id=project, idempotency_key='abandon', status='reconciling',
                            snapshot=snapshot, consent={}, checkpoint={'jobs': {}, 'media': {}})
        session.add(run)
        session.flush()
        job = Job(project_id=project, host_id=host, scene_id=scene['id'], kind='speech',
                  status='reconciling', snapshot={'production_run_id': run.id, 'project': snapshot,
                  'scene': scene}, input_hash='old', result={'speech_pending': True, 'remote_path': 'keep'})
        queued = Job(project_id=project, host_id=host, kind='keyframe', status='queued',
                     snapshot={'production_run_id': run.id}, input_hash='next', result={})
        session.add_all([job, queued])
        session.commit()
    path = service.root / 'retained.wav'
    path.write_bytes(b'old evidence')
    artifact = service.artifact(path, project, 'speech.wav', job.id)
    url = f'/api/studio/jobs/{job.id}/abandon'
    for payload in ({}, {'confirmed': True}, {'confirmed': 'true', 'remote_state_unknown': True}):
        assert client.post(url, json=payload).status_code == 409
    assert client.post(url, json={'confirmed': True, 'remote_state_unknown': True}).status_code == 200
    assert service.require(Job, job.id).result == job.result
    assert service.require(Job, job.id).snapshot == job.snapshot
    assert service.require(Job, queued.id).status == 'abandoned'
    assert service.require(ProductionRun, run.id).status == 'abandoned'
    assert service.require(ProductionRun, run.id).checkpoint == run.checkpoint
    service.assert_idle(project)
    jobs.patch(job.id, status='completed', result={'speech_pending': False})
    jobs.scene_result(scene['id'], job=job, speech_id='late')
    assert service.require(Job, job.id).status == 'abandoned'
    assert service.require(Job, job.id).result == job.result
    assert not service.project(project)['scenes'][0].get('speech_id')
    assert service.artifact_path(artifact['id']).read_bytes() == b'old evidence'
    for action in (lambda: jobs.resume(job.id), lambda: jobs.runs.action(run.id, 'resume'),
                   lambda: jobs.host_idle(host), lambda: jobs.recipes.assert_idle(host)):
        with pytest.raises(ValueError):
            action()
    new_host = client.post('/api/hosts', json={'label': 'New GPU', 'address': 'new.example.test',
        'username': 'root', 'auth_kind': 'password', 'secret': 'secret'}).json()['id']
    stored = jobs.hosts._require_host(new_host)
    stored.pinned_fingerprint = 'SHA256:new'
    jobs.hosts._save(stored)
    updated = client.patch(f'/api/studio/projects/{project}', json={**{k: v for k, v in snapshot.items() if k in ProjectInput.model_fields}, 'host_id': new_host, 'title': 'Editable'})
    assert updated.status_code == 200
    service.approve(scene['id'], service.project(project)['scenes'][0]['revision'], 'script', True)
    # New host does not inherit old-host readiness.
    assert client.post(f'/api/studio/projects/{project}/jobs', json={'kind': 'speech', 'scene_id': scene['id']}).status_code == 409
    jobs.installations.component_proven = lambda host_id, needed: host_id == new_host
    created = client.post(f'/api/studio/projects/{project}/jobs', json={'kind': 'speech', 'scene_id': scene['id']})
    assert created.status_code == 201
    assert created.json()['host_id'] == new_host
    jobs.assert_no_abandoned_remote(new_host)
    assert client.delete(f'/api/hosts/{host}').status_code == 409
    assert client.delete(f'/api/studio/projects/{project}').status_code == 409


@pytest.mark.parametrize('status', ['queued', 'running', 'cancelling', 'completed', 'cancelled'])
def test_abandon_rejects_live_or_resolved_jobs(local, status):
    _, service, jobs, _, _ = local
    id = add_job(local, status, {'speech_pending': True})
    with pytest.raises(ValueError):
        jobs.abandon(id, confirmed=True, remote_state_unknown=True)
    assert service.require(Job, id).status == status


def test_abandon_rejects_local_worker(local):
    _, _, jobs, _, _ = local
    id = add_job(local, 'reconciling', {'speech_pending': True})
    jobs.current = (id, object())
    with pytest.raises(ValueError):
        jobs.abandon(id, confirmed=True, remote_state_unknown=True)


def test_distinct_jobs_queue_and_duplicate_is_reused(local, monkeypatch):
    client, service, jobs, host, project = local
    scenes = [service.add_scene(project, SceneInput(title=f'Scene {n}', narration=f'Text {n}', review_note='Reviewed')) for n in range(2)]
    for scene in scenes:
        service.approve(scene['id'], 1, 'script', True)
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda *args: True)
    url = f'/api/studio/projects/{project}/jobs'
    first = client.post(url, json={'kind': 'speech', 'scene_id': scenes[0]['id']})
    assert first.status_code == 201
    jobs.patch(first.json()['id'], status='running')
    second = client.post(url, json={'kind': 'speech', 'scene_id': scenes[1]['id']})
    assert second.status_code == 201
    assert second.json()['status'] == 'queued'
    assert second.json()['id'] != first.json()['id']
    assert client.post(url, json={'kind': 'speech', 'scene_id': scenes[0]['id']}).json()['id'] == first.json()['id']
    assert client.post(url, json={'kind': 'speech', 'scene_id': scenes[0]['id'], 'force': True}).status_code == 409
    with pytest.raises(ValueError):
        jobs.recipes.assert_idle(host)


@pytest.mark.parametrize('status', ['queued', 'running', 'cancelling', 'reconciling', 'paused'])
def test_active_jobs_block_both_deletions(local, status):
    client, service, _, host, project = local
    add_job(local, status)
    assert client.delete(f'/api/hosts/{host}').status_code == 409
    assert client.delete(f'/api/studio/projects/{project}').status_code == 409
    assert service.project(project)


@pytest.mark.parametrize('status', ['failed', 'interrupted'])
def test_ambiguous_terminal_jobs_block_deletion(local, status):
    client, _, _, host, project = local
    add_job(local, status, {'speech_pending': True})
    assert client.delete(f'/api/hosts/{host}').status_code == 409
    assert client.delete(f'/api/studio/projects/{project}').status_code == 409


def test_host_deletion_retains_history_and_clears_binding(local):
    client, service, _, host, project = local
    job_id = add_job(local, 'completed', {'submissions': {'image': {'prompt_id': 'old'}}})
    assert client.delete(f'/api/hosts/{host}').status_code == 204
    assert service.require(Job, job_id).host_id is None
    assert service.project(project)['host_id'] is None
    assert client.delete(f'/api/hosts/{host}').status_code == 404


def test_project_deletion_cascades_records_but_keeps_files(local):
    client, service, _, host, project = local
    source = service.add_text(project, TextSourceInput(title='Evidence', text='Original'))
    job_id = add_job(local, 'completed')
    with service.sessions() as session:
        session.add(JobEvent(job_id=job_id, message='History'))
        session.commit()
    path = service.root / 'retained.txt'
    path.write_text('Keep local and remote files untouched')
    assert client.delete(f'/api/studio/projects/{project}').status_code == 204
    assert path.exists()
    with service.sessions() as session:
        assert session.get(Project, project) is None
        assert session.get(Source, source['id']) is None
        assert session.get(Job, job_id) is None
        assert session.scalar(select(JobEvent.id)) is None
    assert client.get(f'/api/hosts/{host}').status_code == 200
    assert client.delete(f'/api/studio/projects/{project}').status_code == 404


@pytest.mark.parametrize('blocker', ['recipe', 'tunnel'])
def test_host_deletion_blocks_other_ownership(local, blocker):
    client, service, _, host, _ = local
    with service.sessions() as session:
        if blocker == 'recipe':
            session.add(RecipeRun(host_id=host, recipe_name='test', status='running'))
        else:
            runtime = session.get(HostRuntime, host)
            if runtime is None:
                runtime = HostRuntime(host_id=host)
                session.add(runtime)
            runtime.tunnel_status = 'running'
            runtime.local_url = 'http://127.0.0.1:8188'
        session.commit()
    assert client.delete(f'/api/hosts/{host}').status_code == 409


@pytest.mark.parametrize('status,result', [
    ('reconciling', {}), ('paused', {'submissions': {'image': {'prompt_id': 'persisted'}}}),
    ('failed', {'speech_pending': True}), ('interrupted', {'outline_pending': True}),
])
def test_worker_does_not_pass_unresolved_remote_work(local, monkeypatch, status, result):
    _, service, jobs, _, _ = local
    add_job(local, status, result)
    queued = add_job(local, 'queued')
    async def forbidden(job):
        pytest.fail('Must not start inference while remote work is unresolved')
    monkeypatch.setattr(jobs, 'execute', forbidden)
    async def run():
        worker = asyncio.create_task(jobs.loop())
        try:
            await asyncio.sleep(.05)
            assert service.require(Job, queued).status == 'queued'
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())


def test_recipe_still_blocks_queue_admission(local, monkeypatch):
    client, service, jobs, host, project = local
    scene = service.add_scene(project, SceneInput(title='Scene', narration='Text', review_note='Reviewed'))
    service.approve(scene['id'], 1, 'script', True)
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda *args: True)
    with service.sessions() as session:
        session.add(RecipeRun(host_id=host, recipe_name='test', status='running'))
        session.commit()
    response = client.post(f'/api/studio/projects/{project}/jobs', json={'kind': 'speech', 'scene_id': scene['id']})
    assert response.status_code == 409
    assert jobs.list(project) == []


def test_worker_executes_queue_sequentially(local, monkeypatch):
    _, service, jobs, _, _ = local
    ids = [add_job(local, 'queued') for _ in range(3)]
    calls = []
    async def execute(job):
        calls.append(job.id)
        assert all(service.require(Job, earlier).status == 'completed' for earlier in calls[:-1])
        await asyncio.sleep(0)
    monkeypatch.setattr(jobs, 'execute', execute)
    async def run():
        worker = asyncio.create_task(jobs.loop())
        try:
            for _ in range(200):
                if all(service.require(Job, id).status == 'completed' for id in ids):
                    break
                await asyncio.sleep(.01)
            assert calls == ids
            assert all(service.require(Job, id).status == 'completed' for id in ids)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())
