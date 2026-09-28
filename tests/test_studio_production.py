"""Local-only short-film orchestration tests; no paid inference or GPU acceptance."""
import asyncio
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from studio.installations import identity
from studio.models import Installation, Job, Project, Scene
from studio.packs import PACK_ID, load_graph
from studio.schemas import ProjectInput, SceneInput, TextSourceInput
from studio.service import canonical_hash


@pytest.fixture
def local(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'production.db'}", studio_root=tmp_path / 'data'),
                     SecretStore('production-test'), lambda h, s: FakeExecutor())
    # No application lifespan: deliberately do not start the durable worker.
    return TestClient(app), app.state.studio, app.state.studio_jobs


@pytest.mark.parametrize('seconds', [60, 90, 180, 300])
def test_short_duration(local, seconds):
    client, _, _ = local
    response = client.post('/api/studio/projects', json={'title': 'Short', 'topic': 'History', 'duration_seconds': seconds})
    assert response.status_code == 201
    assert response.json()['duration_seconds'] == seconds
    assert response.json()['duration_minutes'] == seconds / 60


def test_legacy_duration(local):
    _, service, _ = local
    with service.sessions() as session:
        row = Project(data={'title': 'Old', 'topic': 'Old', 'duration_minutes': 15})
        session.add(row)
        session.commit()
    assert service.project(row.id)['duration_seconds'] == 900
    assert ProjectInput(title='Old', topic='Old', duration_minutes=20).duration_seconds == 1200


def test_production_gates_and_checklist(local):
    client, service, _ = local
    project = service.create_project(ProjectInput(title='Short', topic='History', duration_seconds=60))
    scene = service.add_scene(project['id'], SceneInput(title='First', narration='Text', review_note='Reviewed'))
    url = f"/api/studio/projects/{project['id']}/production"
    assert client.post(url, json={'stage': 'assets'}).status_code == 409
    service.approve(scene['id'], 1, 'script', True)
    assert client.post(url, json={'stage': 'assets'}).status_code == 409  # No verified host.
    assert client.post(url, json={'stage': 'clips'}).status_code == 409
    state = client.get(url).json()
    assert state['target_duration_seconds'] == 60
    assert state['checklist'][0]['script_approved']
    assert not state['checklist'][0]['speech_valid']
    assert state['jobs'] == []


def test_stale_result_retains_artifact_reference(local):
    _, service, jobs = local
    project = service.create_project(ProjectInput(title='Short', topic='History'))
    scene = service.add_scene(project['id'], SceneInput(title='First'))
    snapshot = {'project': service.project(project['id']), 'scene': scene}
    with service.sessions() as session:
        job = Job(project_id=project['id'], scene_id=scene['id'], kind='keyframe', snapshot=snapshot, input_hash='x'*64)
        session.add(job)
        row = session.get(Scene, scene['id'])
        row.data = {**row.data, 'visual_prompt': 'Changed'}
        row.revision += 1
        session.commit()
    jobs.scene_result(scene['id'], job=job, keyframe_id='retained-artifact')
    assert service.require(Job, job.id).result['stale']
    assert 'keyframe_id' not in service.require(Scene, scene['id']).data


def test_chapters_checkpoint_and_validate_sources(local):
    _, service, jobs = local
    project = service.create_project(ProjectInput(title='Short', topic='History', duration_seconds=90))
    source = service.add_text(project['id'], TextSourceInput(title='Source', text='Original evidence'))
    with service.sessions() as session:
        row = session.get(Project, project['id'])
        row.data = {**row.data, 'outline_approved': True, 'outline': [
            {'title': 'One', 'source_ids': [source['id']]}, {'title': 'Two', 'source_ids': [source['id']]}]}
        session.commit()
        job = Job(project_id=project['id'], kind='script', input_hash='c'*64,
                  snapshot={'project': service.project(project['id']), 'scene': None})
        session.add(job)
        session.commit()
    calls = []
    async def language(job, prompt, images, log):
        calls.append(prompt)
        return {'scenes': [{'title': str(len(calls)), 'narration': 'Evidence',
                           'citations': [{'source_id': source['id'], 'quote': 'Original evidence'}]}]}
    jobs.backend = SimpleNamespace(language=language)
    asyncio.run(jobs.execute(job))
    assert len(calls) == 2
    result = service.require(Job, job.id).result
    assert len(result['chapters']) == 2
    assert result['timing']['elapsed_seconds'] >= 0
    assert len(service.project(project['id'])['scenes']) == 2
    assert all(not s['script_approved'] for s in service.project(project['id'])['scenes'])


def test_partial_chapter_recovery_does_not_reinfer(local):
    _, service, jobs = local
    project = service.create_project(ProjectInput(title='Short', topic='History', duration_seconds=60))
    source = service.add_text(project['id'], TextSourceInput(title='Source', text='Evidence'))
    snapshot = service.project(project['id'])
    snapshot['outline'] = [{'title': 'One', 'source_ids': [source['id']]}, {'title': 'Two', 'source_ids': [source['id']]}]
    value = SceneInput(title='Saved', narration='Evidence', citations=[{'source_id': source['id'], 'quote': 'Evidence'}]).model_dump()
    with service.sessions() as session:
        job = Job(project_id=project['id'], kind='script', input_hash='p'*64,
                  snapshot={'project': snapshot, 'scene': None}, result={
                      'chapters': {'0': {'scenes': [value], 'elapsed_seconds': 1}}, 'chapter_pending': 1})
        session.add(job)
        session.commit()
    async def forbidden(*args):
        pytest.fail('Ambiguous chapter must not be reinferred')
    jobs.backend = SimpleNamespace(language=forbidden)
    from studio.backend import ReconcileRequired
    with pytest.raises(ReconcileRequired):
        asyncio.run(jobs.execute(job))
    assert service.require(Job, job.id).result['chapters']['0']['scenes'] == [value]


def test_stage_skips_valid_assets_without_approving(local):
    client, service, jobs = local
    project = service.create_project(ProjectInput(title='Short', topic='History'))
    scene = service.add_scene(project['id'], SceneInput(title='One', review_note='Reviewed'))
    path = service.root / 'asset.bin'
    path.write_bytes(b'local-test')
    artifact = service.artifact(path, project['id'], 'asset.bin')
    with service.sessions() as session:
        row = session.get(Scene, scene['id'])
        row.data = {**row.data, 'script_approved': True, 'keyframe_id': artifact['id'], 'speech_id': artifact['id']}
        session.commit()
    result = jobs.start_production(project['id'], 'assets')
    assert result['job_ids'] == []
    assert not result['production']['checklist'][0]['keyframe_approved']
    path.write_bytes(b'corrupted')
    assert not jobs.production(project['id'])['checklist'][0]['speech_valid']


def test_export_stage_is_durable_and_idempotent(local):
    _, service, jobs = local
    project = service.create_project(ProjectInput(title='Short', topic='History'))
    scene = service.add_scene(project['id'], SceneInput(title='One', review_note='Reviewed'))
    path = service.root / 'media.bin'
    path.write_bytes(b'local')
    artifact = service.artifact(path, project['id'], 'media.bin')
    with service.sessions() as session:
        row = session.get(Scene, scene['id'])
        row.data = {**row.data, 'script_approved': True, 'clip_approved': True,
                    'speech_id': artifact['id'], 'clip_ids': [artifact['id']]}
        session.commit()
    first = jobs.start_production(project['id'], 'export')
    second = jobs.start_production(project['id'], 'export')
    assert first['job_ids'] == second['job_ids']
    saved = service.require(Job, first['job_ids'][0])
    assert saved.status == 'queued'
    assert saved.result['production']['stage'] == 'export'
    jobs.patch(saved.id, status='interrupted')
    with pytest.raises(ValueError, match='Tác vụ trước'):
        jobs.start_production(project['id'], 'export')


@pytest.mark.parametrize('failure_point', ['second_insert', 'metadata_flush'])
def test_stage_rolls_back_all_jobs_on_fault(local, monkeypatch, failure_point):
    _, service, jobs = local
    project = service.create_project(ProjectInput(title='Atomic', topic='History'))
    scene = service.add_scene(project['id'], SceneInput(title='One', review_note='Reviewed'))
    service.approve(scene['id'], scene['revision'], 'script', True)
    calls = []
    def fake_submit(project_id, request, transaction=None, **kwargs):
        assert transaction is not None
        row = transaction.query(Job).filter_by(input_hash=request.kind).first()
        if row is None:
            row = Job(project_id=project_id, scene_id=request.scene_id, kind=request.kind,
                      input_hash=request.kind, snapshot={})
            transaction.add(row)
            transaction.flush()
        calls.append(row.id)
        if failure_point == 'second_insert' and len(calls) == 2:
            raise RuntimeError('injected insert failure')
        return service.read(row)
    monkeypatch.setattr(jobs, 'submit', fake_submit)
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    def fail_metadata(session, context, instances):
        if failure_point == 'metadata_flush' and any(isinstance(r, Job) and r.result.get('production') for r in session.dirty):
            raise RuntimeError('injected metadata failure')
    event.listen(Session, 'before_flush', fail_metadata)
    try:
        with pytest.raises(RuntimeError, match='injected'):
            jobs.start_production(project['id'], 'assets')
    finally:
        event.remove(Session, 'before_flush', fail_metadata)
    assert jobs.list(project['id']) == []
    # The next explicit request commits the entire stage; repeating it duplicates nothing.
    failure_point = None
    first = jobs.start_production(project['id'], 'assets')
    second = jobs.start_production(project['id'], 'assets')
    assert first['job_ids'] == second['job_ids']
    assert len(jobs.list(project['id'])) == 2
    assert all(j['result']['production']['stage'] == 'assets' for j in jobs.list(project['id']))


def test_pause_finishes_current_shot_and_resumes_missing_only(local, monkeypatch):
    client, service, jobs = local
    project = service.create_project(ProjectInput(title='Pause', topic='History'))
    scene = service.add_scene(project['id'], SceneInput(title='One', narration='Text'))
    path = service.root / 'input.bin'
    path.write_bytes(b'input')
    artifact = service.artifact(path, project['id'], 'input.bin')['id']
    scene.update(keyframe_id=artifact, speech_id=artifact, keyframe_approved=True, script_approved=True)
    snapshot = {'project': service.project(project['id']), 'scene': scene}
    with service.sessions() as session:
        job = Job(project_id=project['id'], scene_id=scene['id'], kind='clip', status='running',
                  input_hash='pause', snapshot=snapshot)
        session.add(job)
        session.commit()
    monkeypatch.setattr('studio.jobs.probe', lambda path: {'duration': 8})
    monkeypatch.setattr('studio.jobs.shot_count', lambda duration: 2)
    calls = []
    async def generate(*args):
        calls.append(args[8])
        if len(calls) == 1:
            response = client.post(f'/api/studio/jobs/{job.id}/pause')
            assert response.status_code == 200
            assert response.json()['status'] == 'running'
        output = service.job_directory(job.id) / (args[8] + '.mp4')
        output.write_bytes(b'completed shot')
        return output
    jobs.backend = SimpleNamespace(generate=generate)
    from studio.jobs import PauseAtBoundary
    with pytest.raises(PauseAtBoundary):
        asyncio.run(jobs.execute(job))
    assert calls == ['clip-0']
    jobs.patch(job.id, status='paused')  # Durable loop catches PauseAtBoundary this way.
    assert client.post(f'/api/studio/jobs/{job.id}/resume').json()['status'] == 'queued'
    asyncio.run(jobs.execute(service.require(Job, job.id)))
    assert calls == ['clip-0', 'clip-1']


def test_uncertain_remote_cancel_keeps_reconciliation(local):
    client, service, jobs = local
    project = service.create_project(ProjectInput(title='Cancel', topic='History'))
    with service.sessions() as session:
        job = Job(project_id=project['id'], kind='clip', status='running', input_hash='cancel-unknown',
                  snapshot={}, result={'submissions': {'clip-0': {'prompt_id': 'persisted'}}})
        session.add(job)
        session.commit()
    async def uncertain(job):
        raise ConnectionError('lost acknowledgement')
    jobs.backend = SimpleNamespace(cancel=uncertain)
    assert client.post(f'/api/studio/jobs/{job.id}/cancel').status_code == 409
    row = service.require(Job, job.id)
    assert row.status == 'reconciling'
    assert row.result['submissions']['clip-0']['prompt_id'] == 'persisted'
    assert client.post(f'/api/studio/jobs/{job.id}/pause').status_code == 409


def test_queued_pause_and_cancel_are_local_only(local):
    client, service, jobs = local
    project = service.create_project(ProjectInput(title='Pause', topic='History'))
    with service.sessions() as session:
        job = Job(project_id=project['id'], kind='clip', input_hash='queued-pause', snapshot={})
        session.add(job)
        session.commit()
    assert client.post(f'/api/studio/jobs/{job.id}/pause').json()['status'] == 'paused'
    with pytest.raises(ValueError):
        service.assert_idle(project['id'])
    assert client.post(f'/api/studio/jobs/{job.id}/cancel').json()['status'] == 'cancelled'


def test_completed_asset_does_not_change_input_identity(local):
    _, service, jobs = local
    project = service.create_project(ProjectInput(title='Short', topic='History'))
    scene = service.add_scene(project['id'], SceneInput(title='One'))
    project = service.project(project['id'])
    before = jobs.input_identity(project, scene, 'speech')
    after = jobs.input_identity(project, {**scene, 'revision': 9, 'keyframe_id': 'new', 'keyframe_approved': True}, 'speech')
    assert before == after


def test_image_can_run_after_real_video_test_but_speech_stays_locked(local):
    client, service, jobs = local
    host = client.post('/api/hosts', json={'label': 'gpu', 'address': 'gpu.example.test', 'username': 'root', 'auth_kind': 'password', 'secret': 'secret'}).json()
    stored = jobs.hosts._require_host(host['id'])
    stored.pinned_fingerprint = 'SHA256:test'
    jobs.hosts._save(stored)
    project = client.post('/api/studio/projects', json={'title': 'Phim', 'topic': 'Chủ đề', 'host_id': host['id'], 'duration_seconds': 60}).json()
    scene = client.post(f"/api/studio/projects/{project['id']}/scenes", json={'title': 'Cảnh', 'narration': 'Lời đọc', 'review_note': 'đã kiểm chứng'}).json()
    assert client.post(f"/api/studio/scenes/{scene['id']}/approve", json={'revision': scene['revision'], 'target': 'script', 'approved': True}).status_code == 200
    speech = client.post(f"/api/studio/projects/{project['id']}/jobs", json={'kind': 'speech', 'scene_id': scene['id']})
    assert speech.status_code == 409 and 'Giọng đọc chưa sẵn sàng' in speech.json()['detail']
    options = jobs.hosts.options_for(host['id']).model_dump()
    snapshot = {'identity': identity(jobs.hosts._require_host(host['id'])), 'options': options,
                'lock': {'workflow_hashes': {name: canonical_hash(load_graph(name)) for name in ('qwen_image', 'qwen_edit', 'wan_i2v')}}}
    with service.sessions() as session:
        session.add(Job(host_id=host['id'], kind='video_test', status='completed', input_hash='proof', snapshot=snapshot,
                        result={'submissions': {'qwen_image': {'state': 'downloaded'}, 'wan_i2v': {'state': 'downloaded'}}}))
        session.commit()
    image = client.post(f"/api/studio/projects/{project['id']}/jobs", json={'kind': 'keyframe', 'scene_id': scene['id']})
    assert image.status_code == 201, image.text
    assert client.post(f"/api/studio/jobs/{image.json()['id']}/cancel").status_code == 200
    assert client.post(f"/api/studio/projects/{project['id']}/jobs", json={'kind': 'speech', 'scene_id': scene['id']}).status_code == 409
    with service.sessions() as session:
        session.add(Installation(host_id=host['id'], pack_id=PACK_ID, status='installed', data={'installed_snapshot': snapshot}))
        session.commit()
    ready = client.post(f"/api/studio/projects/{project['id']}/jobs", json={'kind': 'speech', 'scene_id': scene['id']})
    assert ready.status_code == 201, ready.text


def test_pending_speech_checks_remote_before_running_again(local):
    _, service, jobs = local
    project = service.create_project(ProjectInput(title='Voice', topic='History'))
    scene = service.add_scene(project['id'], SceneInput(title='One', narration='Xin chào', review_note='đã kiểm chứng'))
    full = service.project(project['id'])
    with service.sessions() as session:
        job = Job(project_id=project['id'], scene_id=scene['id'], kind='speech', status='running', input_hash='voice',
                  snapshot={'project': full, 'scene': full['scenes'][0]}, result={'speech_pending': True})
        session.add(job)
        session.commit()
        job_id = job.id
    audio = service.job_directory(job_id) / 'speech.wav'
    with wave.open(str(audio), 'w') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b'\x10\x00' * 24000)
    calls = []

    async def recover(job):
        calls.append('recover')
        return None

    async def speak(job, text, voice, log):
        calls.append('speech')
        return audio

    jobs.backend = SimpleNamespace(recover_speech=recover, speech=speak)
    asyncio.run(jobs._execute(service.require(Job, job_id)))
    assert calls == ['recover', 'speech']
    assert service.require(Job, job_id).result['speech_pending'] is False
