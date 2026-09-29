"""Offline production tests: never start a worker or contact a paid GPU."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from ghm.models import Host
from studio.models import Job, ProductionRun, Scene
from studio.schemas import ProjectInput, SceneInput, SceneUpdate, ProductionRunInput
from studio.production import dependency_identity


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'db'}", studio_root=tmp_path / 'data'),
                     SecretStore('runs'), lambda h, s: FakeExecutor())
    service, jobs = app.state.studio, app.state.studio_jobs
    with service.sessions() as session:
        host = Host(label='offline', address='localhost', username='u', auth_kind='password',
                    encrypted_secret='unused', pinned_fingerprint='test')
        session.add(host)
        session.commit()
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda *a: True)
    project = service.create_project(ProjectInput(title='Film', topic='History', host_id=host.id, duration_seconds=60))
    scene = service.add_scene(project['id'], SceneInput(title='One', narration='Read this', visual_prompt='A landscape'))
    request = ProductionRunInput(idempotency_key='one', scene_revisions={scene['id']: scene['revision']},
                                 script_approved=True, gpu_consent=True, unsourced_consent=True)
    monkeypatch.setattr('studio.production.probe', lambda p: {'duration': 60})
    return TestClient(app), service, jobs, project, scene, request


def complete(service, jobs, run_id, kind):
    run = service.require(ProductionRun, run_id)
    job = service.require(Job, run.checkpoint['current_job_id'])
    assert job.kind == kind
    path = service.job_directory(job.id) / ('output.' + ('wav' if kind == 'speech' else 'png' if kind == 'keyframe' else 'mp4'))
    path.write_bytes(b'validated fixture')
    artifact = service.artifact(path, run.project_id, path.name, job.id)['id']
    output = {'speech_id': artifact, 'duration': 60, 'shot_count': 12} if kind == 'speech' else {'keyframe_id': artifact, 'keyframe_approved': False} if kind == 'keyframe' else {'clip_ids': [artifact], 'clip_approved': False} if kind == 'clip' else {'artifact_ids': [artifact]}
    if kind != 'export':
        jobs.scene_result(job.scene_id, job=job, **output)
    else:
        jobs.patch(job.id, result=output)
    jobs.patch(job.id, status='completed')
    return job


def test_storyboard_selective_reuse_keeps_audio_and_files(setup, monkeypatch):
    from studio.schemas import ShotDesign
    from studio.storyboard import input_identity
    _, service, jobs, project, scene, _ = setup
    monkeypatch.setattr('studio.media.probe', lambda path: {'duration': 8})
    row = service.require(Scene, scene['id'])
    data = {**row.data, 'shot_list': [ShotDesign().model_dump(), ShotDesign().model_dump()],
            'image_strategy': 'per_shot', 'speech_id': 'audio', 'duration': 8}
    images, clips = [], []
    for kind, ids in [('keyframe', images), ('clip', clips)]:
        if kind == 'clip':
            data['shot_keyframes'] = images
        for index in range(2):
            path = service.root / f'{kind}-{index}.bin'
            path.write_bytes(f'{kind}-{index}'.encode())
            ids.append(service.artifact(path, project['id'], path.name, None,
                       {'shot_input_identity': input_identity(data, index, kind)})['id'])
    data.update(keyframe_id=images[0], shot_keyframes=images, clip_ids=clips)
    with service.sessions() as session:
        row.data = data
        session.add(row)
        session.commit()
    original_path = service.artifact_path
    monkeypatch.setattr(service, 'artifact_path', lambda id: service.root / 'audio' if id == 'audio' else original_path(id))
    shots = deepcopy(data['shot_list'])
    shots[0]['camera_angle'] = 'low'
    payload = {k: data[k] for k in SceneInput.model_fields if k in data}
    saved = service.update_scene(row.id, SceneUpdate(**{**payload, 'shot_list': shots}, revision=row.revision))
    assert saved['speech_id'] == 'audio'
    assert saved['clip_ids'] == clips
    assert saved['reuse_clip_ids'] == {'1': clips[1]}
    assert saved['reuse_shot_keyframes'] == {'1': images[1]}
    assert saved['affected_shots'] == [1]
    assert not saved['keyframe_approved']
    assert all(original_path(id).is_file() for id in images + clips)


def test_full_pipeline_and_idempotency(setup):
    client, service, jobs, project, scene, request = setup
    url = f"/api/studio/projects/{project['id']}/production-runs"
    result = client.post(url, json=request.model_dump())
    assert result.status_code == 201, result.text
    run = result.json()
    assert client.post(url, json=request.model_dump()).json()['id'] == run['id']
    original = deepcopy(run['snapshot'])
    for kind in ('speech', 'keyframe', 'clip', 'export'):
        jobs.runs.tick()
        complete(service, jobs, run['id'], kind)
    jobs.runs.tick()
    final = jobs.runs.get(run['id'])
    assert final['status'] == 'completed'
    assert final['snapshot'] == original
    assert len(final['job_ids']) == 4
    current = service.project(project['id'])['scenes'][0]
    assert current['script_approved']
    assert not current.get('keyframe_approved') and not current.get('clip_approved')
    assert final['checkpoint']['artifact_ids']


def test_consent_revision_and_models(setup, monkeypatch):
    _, service, jobs, project, scene, request = setup
    for field in ('script_approved', 'gpu_consent', 'unsourced_consent'):
        with pytest.raises(ValueError):
            jobs.runs.create(project['id'], request.model_copy(update={field: False}))
    with pytest.raises(ValueError, match='revision'):
        jobs.runs.create(project['id'], request.model_copy(update={'scene_revisions': {scene['id']: 99}}))
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda *a: False)
    with pytest.raises(ValueError, match='Model'):
        jobs.runs.create(project['id'], request)
    assert not jobs.list(project['id'])
    assert not service.require(Scene, scene['id']).data['script_approved']


def test_duration_gate_before_images_and_clips(setup, monkeypatch):
    _, service, jobs, project, scene, request = setup
    run = jobs.runs.create(project['id'], request)
    jobs.runs.tick()
    complete(service, jobs, run['id'], 'speech')
    monkeypatch.setattr('studio.production.probe', lambda p: {'duration': 30})
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['status'] == 'duration_review'
    assert len(jobs.list(project['id'])) == 1
    with pytest.raises(ValueError):
        jobs.runs.action(run['id'], 'resume')
    jobs.runs.action(run['id'], 'accept-duration')
    jobs.runs.tick()
    assert len(jobs.list(project['id'])) == 2


def test_pause_recovery_preserves_remote_submission(setup):
    _, service, jobs, project, scene, request = setup
    run = jobs.runs.create(project['id'], request)
    jobs.runs.tick()
    job_id = jobs.runs.get(run['id'])['checkpoint']['current_job_id']
    jobs.runs.action(run['id'], 'pause')
    assert service.require(Job, job_id).status == 'paused'
    jobs.runs.tick()
    assert len(jobs.list(project['id'])) == 1
    jobs.runs.action(run['id'], 'resume')
    jobs.patch(job_id, status='running', result={'speech_pending': True, 'remote_path': '/saved'})
    jobs.recover()
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['status'] == 'reconciling'
    jobs.runs.action(run['id'], 'resume')
    saved = service.require(Job, job_id)
    assert saved.status == 'queued' and saved.result['speech_pending']
    assert saved.result['remote_path'] == '/saved'


def test_corrupt_checkpoint_blocks_without_gpu_retry(setup):
    _, service, jobs, project, scene, request = setup
    run = jobs.runs.create(project['id'], request)
    jobs.runs.tick()
    job = complete(service, jobs, run['id'], 'speech')
    artifact = service.require(Job, job.id).result['production_output']['speech_id']
    service.artifact_path(artifact).write_bytes(b'corruption')
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['status'] == 'failed'
    assert len(jobs.list(project['id'])) == 1


def test_dependency_specific_identity(setup):
    _, service, jobs, project, scene, request = setup
    project = service.project(project['id'])
    original = deepcopy(project)
    project.update(output_resolution='1440p', transition='dissolve', title='New title')
    for kind in ('speech', 'keyframe', 'clip'):
        assert dependency_identity(project, scene, kind) == dependency_identity(original, scene, kind)
    project['aspect_ratio'] = '9:16'
    assert dependency_identity(project, scene, 'speech') == dependency_identity(original, scene, 'speech')
    assert dependency_identity(project, scene, 'keyframe') != dependency_identity(original, scene, 'keyframe')
    changed = {**scene, 'narration': 'Different text'}
    assert dependency_identity(original, changed, 'speech') != dependency_identity(original, scene, 'speech')
    assert dependency_identity(original, changed, 'keyframe') == dependency_identity(original, scene, 'keyframe')


def test_reuse_audio_after_visual_edit_and_supersede(setup):
    _, service, jobs, project, scene, request = setup
    run = jobs.runs.create(project['id'], request)
    jobs.runs.tick()
    speech = complete(service, jobs, run['id'], 'speech')
    jobs.runs.tick()  # keyframe queued; pause before GPU launch
    jobs.runs.action(run['id'], 'pause')
    live = service.project(project['id'])['scenes'][0]
    value = {k: live[k] for k in SceneInput.model_fields}
    value['visual_prompt'] = 'A different landscape'
    edited = service.update_scene(scene['id'], SceneUpdate(**value, revision=live['revision']))
    second = jobs.runs.create(project['id'], request.model_copy(update={
        'idempotency_key': 'two', 'scene_revisions': {scene['id']: edited['revision']}}))
    assert jobs.runs.get(run['id'])['status'] == 'superseded'
    jobs.runs.tick()
    assert jobs.runs.get(second['id'])['checkpoint']['current_job_id'] == speech.id
    jobs.runs.tick()
    current = service.require(Job, jobs.runs.get(second['id'])['checkpoint']['current_job_id'])
    assert current.kind == 'keyframe'
    assert current.snapshot['scene']['visual_prompt'] == 'A different landscape'
    assert len([j for j in jobs.list(project['id']) if j['kind'] == 'speech']) == 1


def test_active_run_blocks_edits_and_delete(setup):
    _, service, jobs, project, scene, request = setup
    jobs.runs.create(project['id'], request)
    with pytest.raises(ValueError):
        service.delete_project(project['id'])
    with pytest.raises(ValueError):
        service.update_project(project['id'], ProjectInput(title='Changed', topic='t'))


def test_failed_step_retry_is_explicit_and_bounded(setup):
    _, service, jobs, project, scene, request = setup
    run = jobs.runs.create(project['id'], request)
    jobs.runs.tick()
    job_id = jobs.runs.get(run['id'])['checkpoint']['current_job_id']
    for attempt in range(3):
        jobs.patch(job_id, status='failed', error='Known failure')
        jobs.runs.tick()
        assert jobs.runs.get(run['id'])['status'] == 'failed'
        jobs.runs.action(run['id'], 'resume')
        assert service.require(Job, job_id).result['production_retry_count'] == attempt + 1
    jobs.patch(job_id, status='failed')
    jobs.runs.tick()
    with pytest.raises(ValueError, match='3 lần'):
        jobs.runs.action(run['id'], 'resume')
    assert len(jobs.list(project['id'])) == 1


def test_legacy_media_reuse_requires_dependency_proof(setup):
    _, service, jobs, project, scene, request = setup
    snapshot = service.project(project['id'])
    with service.sessions() as session:
        legacy = Job(project_id=project['id'], scene_id=scene['id'], kind='speech', status='completed',
                     input_hash='legacy', snapshot={'project': snapshot, 'scene': scene})
        session.add(legacy)
        session.commit()
    path = service.job_directory(legacy.id) / 'speech.wav'
    path.write_bytes(b'legacy proof')
    artifact = service.artifact(path, project['id'], 'speech.wav', legacy.id)['id']
    with service.sessions() as session:
        row = session.get(Scene, scene['id'])
        row.data = {**row.data, 'speech_id': artifact, 'duration': 60}
        session.commit()
    run = jobs.runs.create(project['id'], request)
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['checkpoint']['current_job_id'] == legacy.id
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['stage'] == 'keyframe'


def test_settings_validation():
    assert ProjectInput(title='t', topic='t').render_profile is None
    with pytest.raises(ValueError):
        ProjectInput(title='t', topic='t', upscale_method='ai')
    for ratio in ('16:9', '9:16', '1:1', '4:5'):
        assert ProjectInput(title='t', topic='t', aspect_ratio=ratio).aspect_ratio == ratio
