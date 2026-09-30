"""Offline regression coverage for paid-render safety and performance accounting."""
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import UUID, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.models import Host
from ghm.schemas import HostOptions
from ghm.security import SecretStore
from studio.backend import ReconcileRequired, RemoteBackend
from studio.benchmark import report
from studio.comfy_schema import normalize_graph, validate_graph
from studio.generation import (
    clip_config,
    clip_output_matches,
    identity_scene,
    shot_config,
    stage_steps,
)
from studio.metrics import estimate, execution_seconds
from studio.models import Artifact, Job, ProductionRun, Scene
from studio.packs import graph_for
from studio.production import dependency_identity
from studio.runtime import runtime_flags
from studio.schemas import JobInput, ProjectInput, SceneInput, SceneUpdate


@pytest.fixture
def local(tmp_path):
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path / "test.db"}', studio_root=tmp_path / 'data'),
                     SecretStore('offline'), lambda h, s: FakeExecutor())
    service, jobs = app.state.studio, app.state.studio_jobs
    project = service.create_project(ProjectInput(title='Film', topic='Test', hourly_usd=2))
    scene = service.add_scene(project['id'], SceneInput(title='Scene', narration='Narration', steps=20))
    image = service.root / 'input.png'
    image.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (32, 32), 'red').save(image)
    artifact = service.artifact(image, project['id'], 'reference.png')
    with service.sessions() as session:
        row = session.get(Scene, scene['id'])
        row.data = {**row.data, 'speech_id': artifact['id'], 'keyframe_id': artifact['id'],
                    'keyframe_approved': True, 'script_approved': True}
        session.commit()
    scene = service.read(service.require(Scene, scene['id']))
    with service.sessions() as session:
        job = Job(project_id=project['id'], scene_id=scene['id'], kind='clip', status='paused',
                  snapshot={'project': service.project(project['id']), 'scene': scene}, input_hash='old')
        session.add(job)
        session.commit()
    return TestClient(app), service, jobs, job, image


def test_steps_are_separate_and_legacy_identity_is_unchanged():
    old = {'steps': 20, 'narration': 'n'}
    assert stage_steps(old, 'clip') == 20
    new = {**old, 'keyframe_steps': 12, 'clip_steps': 4}
    assert stage_steps(new, 'clip') == 4
    assert stage_steps(new, 'keyframe') == 12
    assert identity_scene({**old, 'clip_steps': None, 'keyframe_steps': None}, 'clip') == old
    assert identity_scene(new, 'speech') == old
    for ratio in ('16:9', '9:16', '1:1', '4:5'):
        project = {'quality': 'final', 'aspect_ratio': ratio}
        c = clip_config(project, new)
        g = graph_for('wan_i2v', 'test', 42, 'final', ['ref.png'], 'test', c['steps'], project_settings=project)
        assert g['98']['inputs']['length'] == 81 and g['94']['inputs']['fps'] == 16
        assert g['86']['inputs']['end_at_step'] == g['85']['inputs']['start_at_step'] == 2
        assert g['85']['inputs']['end_at_step'] == 4
        assert (g['98']['inputs']['width'], g['98']['inputs']['height']) == (c['width'], c['height'])


def test_new_profiles_keep_wan_default_and_shorten_only_last_shot():
    project = {'quality': 'final', 'keyframe_profile': 'lightning'}
    scene = {'seed': 42, 'shorten_last_shot': True, 'clip_steps': 4}
    duration = 11.1
    first = clip_config(project, scene, index=0, duration=duration)
    last = clip_config(project, scene, index=2, duration=duration)
    assert first['frames'] == 81 and first['fps'] == 16
    assert last['frames'] == 33 and last['fps'] == 16
    assert last['frames'] % 4 == 1
    assert last['shot_seconds'] >= duration - 2 * 81/16
    graph = graph_for('wan_i2v', 'a scene', 42, 'final', ['keyframe.png'], 'clip',
                      4, project_settings=project, frames=last['frames'], generation_version=3)
    assert graph['98']['inputs']['length'] == 33
    assert graph['94']['inputs']['fps'] == 16
    assert graph['86']['inputs']['end_at_step'] == graph['85']['inputs']['start_at_step'] == 2
    assert stage_steps({}, 'keyframe', 'qwen_image', project, 3) == 4
    image = graph_for('qwen_image', 'historical scene', 42, 'final', [], 'image',
                      project_settings=project, generation_version=3)
    assert image['3']['inputs']['steps'] == 4
    assert image['67']['inputs']['lora_name'].startswith('Qwen-Image-fp8')
    assert graph_for('qwen_image', 'old', 42, 'final', [], 'image')['3']['inputs']['steps'] == 20


def test_pending_shot_shortening_respects_saved_intent(local, monkeypatch):
    _, service, jobs, job, _ = local
    monkeypatch.setattr('studio.jobs.probe', lambda path: {'duration': 11.1})
    with service.sessions() as session:
        row = session.get(Job, job.id)
        row.snapshot = {**row.snapshot, 'generation_version': 3}
        session.commit()
    current = service.require(Job, job.id)
    jobs.set_pending_clip_config(current, {'clip_steps': 4, 'shorten_last_shot': True})
    assert shot_config(current, 'clip-0', 11.1)['frames'] == 81
    assert shot_config(current, 'clip-2', 11.1)['frames'] == 33
    # A previously submitted shot keeps its checkpointed graph configuration.
    frozen = clip_config(current.snapshot['project'], current.snapshot['scene'],
                         index=2, duration=11.1)
    current.result['submissions'] = {'clip-2': {'state': 'submitted', 'config': frozen}}
    assert shot_config(current, 'clip-2', 11.1) == frozen


def test_rife_template_is_separate_from_wan():
    from studio.packs import load_graph
    graph = graph_for('rife_post', '', 0, 'final', ['original.mp4'], 'rife-0')
    assert graph['1']['inputs']['file'] == 'original.mp4'
    assert graph['3']['inputs']['model_name'] == 'rife_v4.26.safetensors'
    assert graph['4']['inputs']['multiplier'] == 3
    assert graph['5']['inputs']['fps'] == 48
    assert load_graph('wan_i2v')['98']['inputs']['length'] == 81
    assert estimate([{'state': 'downloaded', 'config': {'motion':'static'},
                      'timing': {'total_seconds': 1}}],
                    [({'motion':'static'}, {})], 2)['estimated_remaining_usd'] == 0


def test_stage_specific_production_dependencies():
    project = {'voice': 'v', 'characters': [], 'sources': [], 'quality': 'final'}
    scene = {'narration': 'n', 'steps': None}
    changed = {**scene, 'clip_steps': 8}
    for kind in ('speech', 'keyframe'):
        assert dependency_identity(project, scene, kind) == dependency_identity(project, changed, kind)
    assert dependency_identity(project, scene, 'clip') != dependency_identity(project, changed, 'clip')


def test_legacy_project_save_preserves_media_and_audio(local):
    _, service, _, job, _ = local
    with service.sessions() as session:
        session.get(Job, job.id).status = 'completed'
        session.commit()
    before = service.project(job.project_id)['scenes'][0]
    service.update_project(job.project_id, ProjectInput(title='Film', topic='Test', hourly_usd=2))
    after = service.project(job.project_id)['scenes'][0]
    assert after['keyframe_id'] == before['keyframe_id']
    assert after['speech_id'] == before['speech_id']
    assert after['revision'] == before['revision']


def test_switching_image_strategy_keeps_audio_and_clears_old_image_review(local):
    _, service, _, job, _ = local
    with service.sessions() as session:
        session.get(Job, job.id).status = 'completed'
        row = session.get(Scene, job.scene_id)
        row.data = {**row.data, 'image_strategy': 'shared',
                    'shot_keyframes': [row.data['keyframe_id']],
                    'shot_keyframes_approved': True, 'clip_ids': ['old']}
        session.commit()
    old = service.read(service.require(Scene, job.scene_id))
    value = {key: old[key] for key in SceneInput.model_fields if key in old}
    value['image_strategy'] = 'per_shot'
    edited = service.update_scene(job.scene_id, SceneUpdate(**value, revision=old['revision']))
    assert edited['speech_id'] == old['speech_id']
    assert edited['keyframe_id'] == old['keyframe_id']
    assert not edited.get('shot_keyframes_approved')
    assert not edited.get('shot_keyframes')
    assert not edited.get('clip_ids')


@pytest.mark.asyncio
async def test_static_clip_job_finishes_without_gpu_and_keeps_audio(local):
    from studio.media import probe, run_ffmpeg

    _, service, jobs, old_job, _ = local
    audio = service.root / 'speech.wav'
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.2', str(audio)])
    saved_audio = service.artifact(audio, old_job.project_id, 'speech.wav')
    with service.sessions() as session:
        session.get(Job, old_job.id).status = 'completed'
        row = session.get(Scene, old_job.scene_id)
        row.data = {**row.data, 'speech_id': saved_audio['id'], 'motion': 'static'}
        session.commit()
    submitted = jobs.submit(old_job.project_id, JobInput(kind='clip', scene_id=old_job.scene_id))
    assert submitted['host_id'] is None
    await jobs.execute(service.require(Job, submitted['id']))
    current = service.read(service.require(Scene, old_job.scene_id))
    assert current['speech_id'] == saved_audio['id']
    assert len(current['clip_ids']) == 1
    info = probe(service.artifact_path(current['clip_ids'][0]))
    assert info['duration'] >= 1.2 and info['fps'] == 24


def test_pending_override_preserves_snapshot_artifact_and_submissions(local, monkeypatch):
    client, service, jobs, job, _ = local
    monkeypatch.setattr('studio.jobs.probe', lambda p: {'duration': 12})
    snapshot = deepcopy(job.snapshot)
    path = service.job_directory(job.id) / 'clip-0.mp4'
    path.write_bytes(b'completed')
    artifact = service.artifact(path, job.project_id, path.name, job.id)
    jobs.checkpoint(job.id, 'clip-0', {'prompt_id': 'old-id', 'state': 'downloaded', 'artifact_id': artifact['id'], 'timing': {'comfy_execution_seconds': 99}})
    response = client.post(f'/api/studio/jobs/{job.id}/pending-clip-config', json={'clip_steps': 4})
    assert response.status_code == 200, response.text
    updated = service.require(Job, job.id)
    assert updated.snapshot == snapshot
    assert shot_config(updated, 'clip-0')['steps'] == 20
    assert shot_config(updated, 'clip-1')['steps'] == 4
    assert service.require(Artifact, artifact['id']).sha256 == artifact['sha256']
    jobs.checkpoint(job.id, 'clip-0', {'timing': {'download_seconds': 2}})
    record = service.require(Job, job.id).result['submissions']['clip-0']
    assert record['prompt_id'] == 'old-id' and record['artifact_id'] == artifact['id']
    assert record['timing'] == {'comfy_execution_seconds': 99, 'download_seconds': 2}


@pytest.mark.parametrize('state', ['submitting', 'submitted', 'remote_completed'])
def test_pending_override_rejects_uncertain_remote(local, state):
    client, _, jobs, job, _ = local
    jobs.checkpoint(job.id, 'clip-0', {'state': state, 'prompt_id': 'old'})
    response = client.post(f'/api/studio/jobs/{job.id}/pending-clip-config', json={'clip_steps': 4})
    assert response.status_code == 409


def test_run_override_preserves_frozen_run(local, monkeypatch):
    client, service, _jobs, job, _ = local
    monkeypatch.setattr('studio.jobs.probe', lambda p: {'duration': 12})
    with service.sessions() as session:
        run = ProductionRun(project_id=job.project_id, idempotency_key='r', status='paused', stage='clip',
                            snapshot=job.snapshot['project'], consent={}, checkpoint={'current_job_id': job.id, 'jobs': {}})
        session.add(run)
        session.flush()
        row = session.get(Job, job.id)
        row.snapshot = {**row.snapshot, 'production_run_id': run.id}
        session.commit()
    original = deepcopy(run.snapshot)
    assert client.post(f'/api/studio/jobs/{job.id}/pending-clip-config', json={'clip_steps': 4}).status_code == 409
    assert client.post(f'/api/studio/production-runs/{run.id}/pending-clip-config', json={'clip_steps': 4}).status_code == 200
    assert service.require(ProductionRun, run.id).snapshot == original
    assert service.require(ProductionRun, run.id).checkpoint['pending_clip_config'] == {'clip_steps': 4}


def test_dynamic_and_legacy_savevideo_schema():
    source = {'output': ['VIDEO'], 'input': {'required': {}}}
    graph = {'1': {'class_type': 'Video', 'inputs': {}},
             '2': {'class_type': 'SaveVideo', 'inputs': {'video': ['1', 0], 'format': 'mp4', 'codec': {'codec': 'h264'}}}}
    required = {'video': ['VIDEO'], 'format': [['mp4', 'webm']], 'codec': [['h264', 'av1']]}
    schema = {'Video': source, 'SaveVideo': {'input': {'required': required}}}
    assert normalize_graph(graph, schema)['2']['inputs']['codec'] == 'h264'
    required['format'] = ['COMFY_DYNAMICCOMBO_V3', {'options': [{'key': 'mp4', 'inputs': {
        'required': {'codec': ['COMFY_DYNAMICCOMBO_V3', {'options': [{'key': 'h264', 'inputs': {}}]}]}}}]}]
    required.pop('codec')
    fixed = normalize_graph(graph, schema)
    assert fixed['2']['inputs']['format.codec'] == 'h264' and 'codec' not in fixed['2']['inputs']
    fixed['2']['inputs'].pop('format.codec')
    with pytest.raises(ValueError, match='format.codec'):
        validate_graph(fixed, schema)


@pytest.mark.parametrize('value', [None, 0, 257, '16'])
def test_required_and_bounds_are_checked(value):
    inputs = {} if value is None else {'resolution_steps': value}
    schema = {'Scale': {'input': {'required': {'resolution_steps': ['INT', {'min': 1, 'max': 256}]}}}}
    with pytest.raises(ValueError):
        validate_graph({'93': {'class_type': 'Scale', 'inputs': inputs}}, schema)


def test_prompt_correlated_metrics_and_no_false_eta():
    item = {'status': {'messages': [['execution_start', {'prompt_id': 'p', 'timestamp': 1000}],
                                    ['execution_success', {'prompt_id': 'other', 'timestamp': 2000}]]}}
    assert execution_seconds(item, 'p') is None
    item['status']['messages'].append(['execution_success', {'prompt_id': 'p', 'timestamp': 31000}])
    assert execution_seconds(item, 'p') == 30
    config = clip_config({'quality': 'final'}, {})
    record = {'state': 'downloaded', 'config': config, 'runtime': {'gpu': 'a'}, 'timing': {'total_seconds': 60}}
    assert estimate([record], [(config, {'gpu': 'a'})], 2)['estimated_remaining_usd'] == pytest.approx(1/30)
    assert estimate([record], [(config, {'gpu': 'b'})], 2)['eta_seconds'] is None
    assert estimate([{**record, 'attempts': 2}], [(config, {'gpu': 'a'})])['eta_seconds'] is None


class ComfyMock:
    def __init__(self, local, monkeypatch):
        _, self.service, self.jobs, self.job, self.image = local
        self.calls, self.submitted = [], {}
        self.disconnect = False
        self.download_error = False
        self.forget = False
        self.outputs = None
        self.viewed = []
        self.busy = False
        self.schema_bad = False
        self.connections, self.closed = 0, 0
        self.backend = RemoteBackend(None, self.service)
        self.jobs.backend = self.backend
        self.schema = {'Test': {'input': {'required': {'steps': ['INT', {'min': 2, 'max': 50}]}}, 'output': ['VIDEO']}}
        monkeypatch.setattr('studio.backend.graph_for', lambda name, prompt, seed, quality, names, prefix, steps, shot, **kw:
                            {'1': {'class_type': 'Test', 'inputs': {'steps': steps}}})
        monkeypatch.setattr('studio.backend.probe', lambda p: {'duration': 81/16})
        async def sleep(seconds):
            pass
        monkeypatch.setattr('studio.backend.asyncio.sleep', sleep)
        @asynccontextmanager
        async def connection(host_id):
            self.connections += 1
            async with httpx.AsyncClient(base_url='http://comfy', transport=httpx.MockTransport(self.response)) as client:
                try:
                    yield None, client
                finally:
                    self.closed += 1
        self.backend.connection = connection

    def response(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        if path.startswith('/history/'):
            id = path.rsplit('/', 1)[-1]
            history = {'status': {'status_str': 'success', 'completed': True, 'messages': [
                ['execution_start', {'prompt_id': id, 'timestamp': 1000}],
                ['execution_success', {'prompt_id': id, 'timestamp': 61000}]]},
                'outputs': self.outputs or {'1': {'videos': [{'filename': 'shot.mp4', 'subfolder': '', 'type': 'output'}]}}}
            return httpx.Response(200, json={id: history} if id in self.submitted and not self.forget else {})
        if path == '/queue':
            return httpx.Response(200, json={'queue_running': [[0, 'someone-else']] if self.busy else [], 'queue_pending': []})
        if path == '/system_stats':
            return httpx.Response(200, json={'system': {'comfyui_version': 'test', 'argv': []}, 'devices': [{'name': 'A100', 'vram_total': 80*1024**3}]})
        if path == '/upload/image':
            return httpx.Response(200, json={'name': 'image.png'})
        if path == '/object_info':
            return httpx.Response(200, json={} if self.schema_bad else self.schema)
        if path == '/prompt':
            data = json.loads(request.content)
            # Prove durable checkpoint precedes the paid network request.
            saved = self.service.require(Job, self.job.id).result['submissions']
            assert any(s['prompt_id'] == data['prompt_id'] and s['state'] == 'submitting' and s['graph_hash'] for s in saved.values())
            self.submitted[data['prompt_id']] = data
            if self.disconnect:
                raise httpx.ReadError('response lost', request=request)
            return httpx.Response(200, json={'prompt_id': data['prompt_id']})
        if path == '/view':
            self.viewed.append(dict(request.url.params))
            return httpx.Response(503 if self.download_error else 200, content=b'video')
        raise AssertionError(path)

    async def generate(self, index=0):
        job = self.service.require(Job, self.job.id)
        return await self.backend.generate(job, 'wan_i2v', 'test', [self.image], 42, 'draft', lambda m: None,
            lambda stage, value: self.jobs.checkpoint(job.id, stage, value), f'clip-{index}', 4, index)


@pytest.mark.asyncio
async def test_connection_upload_schema_cache_and_reconnect(local, monkeypatch):
    mock = ComfyMock(local, monkeypatch)
    async with mock.backend.generation_session(mock.job):
        await mock.generate(0)
        await mock.generate(1)
        Image.new('RGB', (32, 32), 'blue').save(mock.image)
        await mock.generate(2)
    assert mock.connections == mock.closed == 1
    assert mock.calls.count(('POST', '/upload/image')) == 2
    assert mock.calls.count(('GET', '/object_info')) == 1
    await mock.generate(3)
    assert mock.connections == mock.closed == 2
    assert mock.calls.count(('POST', '/upload/image')) == 3
    assert mock.calls.count(('GET', '/object_info')) == 2


@pytest.mark.asyncio
async def test_post_disconnect_reconcile_never_reposts(local, monkeypatch):
    mock = ComfyMock(local, monkeypatch)
    mock.disconnect = True
    with pytest.raises(ReconcileRequired):
        await mock.generate()
    mock.disconnect = False
    await mock.generate()
    assert mock.calls.count(('POST', '/prompt')) == 1
    record = mock.service.require(Job, mock.job.id).result['submissions']['clip-0']
    assert record['state'] == 'downloaded' and record['attempts'] == 2
    assert record['timing']['comfy_execution_seconds'] == 60


@pytest.mark.asyncio
async def test_rife_post_keeps_prompt_id_and_does_not_repost(local, monkeypatch):
    mock = ComfyMock(local, monkeypatch)
    source = mock.service.job_directory(mock.job.id) / 'source.mp4'
    source.write_bytes(b'saved clip fixture')
    mock.schema = {'Test': {'input': {'required': {}}, 'output': ['VIDEO']}}
    monkeypatch.setattr('studio.backend.graph_for', lambda *args, **kwargs:
                        {'1': {'class_type': 'Test', 'inputs': {}}})
    job = mock.service.require(Job, mock.job.id)
    async def run():
        return await mock.backend.generate(mock.service.require(Job, job.id), 'rife_post', '',
            [source], 0, 'draft', lambda message: None,
            lambda stage, value: mock.jobs.checkpoint(job.id, stage, value), 'rife-0')
    mock.disconnect = True
    with pytest.raises(ReconcileRequired):
        await run()
    mock.disconnect = False
    await run()
    assert mock.calls.count(('POST', '/prompt')) == 1
    assert mock.calls.count(('POST', '/upload/image')) == 1
    record = mock.service.require(Job, job.id).result['submissions']['rife-0']
    assert record['state'] == 'downloaded' and record['graph_hash']


@pytest.mark.asyncio
async def test_missing_history_after_intent_refuses_post(local, monkeypatch):
    mock = ComfyMock(local, monkeypatch)
    mock.jobs.checkpoint(mock.job.id, 'clip-0', {'prompt_id': str(uuid5(UUID(mock.job.id), 'clip-0')), 'state': 'submitting'})
    with pytest.raises(ReconcileRequired):
        await mock.generate()
    assert not any(method == 'POST' for method, _ in mock.calls)


@pytest.mark.asyncio
async def test_reconcile_uses_persisted_prompt_id(local, monkeypatch):
    mock = ComfyMock(local, monkeypatch)
    mock.jobs.checkpoint(mock.job.id, 'clip-0', {'prompt_id': 'historical-id', 'state': 'submitted'})
    mock.submitted['historical-id'] = {}
    await mock.generate()
    assert ('GET', '/history/historical-id') in mock.calls
    assert not any(method == 'POST' for method, _ in mock.calls)
    assert mock.service.require(Job, mock.job.id).result['submissions']['clip-0']['prompt_id'] == 'historical-id'


@pytest.mark.asyncio
async def test_download_recovery_uses_saved_output_even_after_history_eviction(local, monkeypatch):
    mock = ComfyMock(local, monkeypatch)
    mock.download_error = True
    with pytest.raises(httpx.HTTPStatusError):
        await mock.generate()
    assert mock.service.require(Job, mock.job.id).result['submissions']['clip-0']['state'] == 'remote_completed'
    mock.download_error, mock.forget = False, True
    await mock.generate()
    assert mock.calls.count(('POST', '/prompt')) == 1
    assert mock.calls.count(('POST', '/upload/image')) == 1
    assert mock.service.require(Job, mock.job.id).result['submissions']['clip-0']['timing']['comfy_execution_seconds'] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['busy', 'schema_bad'])
async def test_external_queue_or_bad_schema_never_records_intent(local, monkeypatch, problem):
    mock = ComfyMock(local, monkeypatch)
    setattr(mock, problem, True)
    with pytest.raises(ValueError):
        await mock.generate()
    assert ('POST', '/prompt') not in mock.calls
    assert not mock.service.require(Job, mock.job.id).result.get('submissions')


def test_runtime_flags_and_adopt_rejection(local):
    _, service, jobs, _, _ = local
    assert runtime_flags({}) == []
    assert runtime_flags({'attention_backend': 'sage', 'memory_policy': 'highvram'}) == ['--use-sage-attention', '--highvram']
    with service.sessions() as session:
        host = Host(label='GPU', address='localhost', username='u', auth_kind='password', encrypted_secret='unused')
        session.add(host)
        session.commit()
    jobs.hosts.save_options(host.id, HostOptions(adopt_existing=True))
    with pytest.raises(ValueError, match='adopt'):
        jobs.runtime.start(host.id, {})


def test_benchmark_report_excludes_cold_and_uses_per_scene_rounding():
    values = [{'state': 'downloaded', 'cold_candidate': i == 0,
               'timing': {'total_seconds': 500 if i == 0 else 60, 'comfy_execution_seconds': 55}} for i in range(6)]
    result = report(values, [326 / 14] * 14, 2)
    assert result['warm_samples'] == 5
    assert result['median_hot_wall_seconds'] == 60
    assert result['scenarios'][1]['shots'] == 70
    assert result['scenarios'][1]['gpu_hours_estimate'] == pytest.approx(70/60)


@pytest.mark.asyncio
async def test_resume_completed_shots_uses_override_only_for_missing(local, monkeypatch):
    client, service, jobs, job, _ = local
    monkeypatch.setattr('studio.jobs.probe', lambda p: {'duration': 12})
    path = service.job_directory(job.id) / 'clip-0.mp4'
    path.write_bytes(b'old completed shot')
    artifact = service.artifact(path, job.project_id, path.name, job.id)
    jobs.checkpoint(job.id, 'clip-0', {'state': 'downloaded', 'prompt_id': 'old'})
    assert client.post(f'/api/studio/jobs/{job.id}/pending-clip-config', json={'clip_steps': 4}).status_code == 200
    mock = ComfyMock(local, monkeypatch)
    jobs.patch(job.id, status='running')
    await jobs.execute(service.require(Job, job.id))
    assert mock.calls.count(('POST', '/prompt')) == 2
    assert all(p['prompt']['1']['inputs']['steps'] == 4 for p in mock.submitted.values())
    assert service.require(Artifact, artifact['id']).sha256 == artifact['sha256']
    assert service.require(Scene, job.scene_id).data['clip_ids'][0] == artifact['id']
    assert service.require(Job, job.id).snapshot['scene']['steps'] == 20


@pytest.mark.asyncio
async def test_storyboard_image_video_prompt_and_resume(local, monkeypatch):
    from studio.schemas import ShotDesign
    _, service, jobs, old, image = local
    monkeypatch.setattr('studio.jobs.probe', lambda p: {'duration': 8})
    shots = [ShotDesign(subject='boat').model_dump(), ShotDesign(subject='boat', camera_angle='low').model_dump()]
    scene = {**old.snapshot['scene'], 'shot_list': shots, 'image_strategy': 'per_shot'}
    calls = []
    async def generate(job, graph, prompt, images, seed, quality, log, checkpoint, stage, steps, index):
        calls.append((graph, prompt, stage))
        path = service.job_directory(job.id) / (stage + '.fixture')
        path.write_bytes(image.read_bytes())
        return path
    monkeypatch.setattr(jobs.backend, 'generate', generate)
    def new_job(kind, value):
        with service.sessions() as session:
            job = Job(project_id=old.project_id, scene_id=old.scene_id, kind=kind, status='running',
                      snapshot={'project': old.snapshot['project'], 'scene': deepcopy(value)}, input_hash=kind)
            session.add(job)
            session.commit()
        return job
    keyframe = new_job('keyframe', scene)
    await jobs._execute(keyframe)
    with service.sessions() as session:
        images = list(session.scalars(__import__('sqlalchemy').select(Artifact).where(Artifact.job_id == keyframe.id).order_by(Artifact.name)))
    scene.update(shot_keyframes=[a.id for a in images], keyframe_id=images[0].id)
    clip = new_job('clip', scene)
    await jobs._execute(clip)
    assert calls[0][1] == calls[2][1]
    assert calls[1][1] == calls[3][1]
    assert 'camera_angle: low' in calls[3][1]
    assert service.require(Job, clip.id).result.get('stale')
    await jobs._execute(clip)
    assert len(calls) == 4  # persisted files resume without another submission


@pytest.mark.asyncio
async def test_process_restart_invalidates_cache(local, monkeypatch):
    mock = ComfyMock(local, monkeypatch)
    generations = iter(['pid:1', 'pid:2'])
    async def generation(*args):
        return next(generations)
    mock.backend.process_generation = generation
    async with mock.backend.generation_session(mock.job):
        await mock.generate(0)
        await mock.generate(1)
    assert mock.calls.count(('POST', '/upload/image')) == 2
    assert mock.calls.count(('GET', '/object_info')) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('queue_busy', [False, True])
async def test_managed_runtime_checks_before_restart(local, monkeypatch, queue_busy):
    from ghm.executors.base import CommandResult
    _, service, jobs, _, _ = local
    with service.sessions() as session:
        host = Host(label='GPU', address='localhost', username='u', auth_kind='password', encrypted_secret='unused')
        session.add(host)
        session.commit()
    operations = []
    class Executor:
        async def run(self, command, timeout):
            return CommandResult(0, '', '')

        async def run_input(self, command, payload, timeout):
            operations.append('probe')
            return CommandResult(0, json.dumps({'torch': '2.8', 'cuda': '12.8'}), '')
    async def manage(executor, options, action, args):
        operations.append(action)
        return json.dumps({'argv': ['/opt/ghm/venv/bin/python', '/opt/ghm/ComfyUI/main.py', '--listen', '127.0.0.1', '--port', '8188']})
    monkeypatch.setattr('studio.runtime.manage', manage)
    def response(request):
        return httpx.Response(200, json={'queue_running': [[0, 'foreign']] if queue_busy else [], 'queue_pending': []})
    @asynccontextmanager
    async def connection(host_id):
        async with httpx.AsyncClient(base_url='http://comfy', transport=httpx.MockTransport(response)) as client:
            yield Executor(), client
    jobs.backend.connection = connection
    async def metadata(client):
        return {'attention_backend': 'default', 'memory_policy': 'default'}
    jobs.backend.runtime_metadata = metadata
    record = jobs.runtime.start(host.id, {})
    if queue_busy:
        with pytest.raises(ValueError, match='queue'):
            await jobs.runtime.execute(service.require(Job, record['id']))
        assert operations == []
    else:
        await jobs.runtime.execute(service.require(Job, record['id']))
        assert operations == ['inspect', 'probe', 'stop', 'start']
        assert service.require(Job, record['id']).result['maintenance_pending'] is False


def test_runtime_pending_blocks_other_work_and_admits_explicit_recovery(local):
    _, service, jobs, _, _ = local
    with service.sessions() as session:
        host = Host(label='GPU', address='localhost', username='u', auth_kind='password', encrypted_secret='unused')
        session.add(host)
        session.flush()
        session.add(Job(host_id=host.id, kind='runtime', status='failed', input_hash='f', snapshot={}, result={'maintenance_pending': True}))
        session.commit()
    assert jobs.runtime.at_boundary(host.id) is False
    with pytest.raises(ValueError):
        jobs.runtime.start(host.id, {})
    recovery = jobs.runtime.start(host.id, {}, 'recover')
    assert jobs.runtime.at_boundary(host.id, recovery['id'], recover=True)


def test_overridden_output_is_not_mislabelled_as_original(local):
    _, service, jobs, job, _ = local
    jobs.patch(job.id, result={'pending_clip_configs': {'clip-1': {'clip_steps': 4}}, 'submissions': {
        'clip-0': {'state': 'downloaded', 'config': clip_config(job.snapshot['project'], job.snapshot['scene'])}}})
    updated = service.require(Job, job.id)
    assert not clip_output_matches(updated, job.snapshot['project'], job.snapshot['scene'])
    assert not clip_output_matches(updated, job.snapshot['project'], {**job.snapshot['scene'], 'clip_steps': 4})


@pytest.mark.asyncio
async def test_benchmark_produces_report_without_mutating_scene_and_resumes(local, monkeypatch):
    from studio.benchmark import execute
    _, service, jobs, job, _ = local
    original = deepcopy(service.require(Scene, job.scene_id).data)
    with service.sessions() as session:
        row = session.get(Job, job.id)
        row.kind, row.status = 'benchmark', 'running'
        row.snapshot = {**row.snapshot, 'benchmark': {'warm_shots': 5, 'max_wall_seconds': 300}}
        session.commit()
    monkeypatch.setattr('studio.benchmark.probe', lambda p: {'duration': 12})
    mock = ComfyMock(local, monkeypatch)
    await execute(jobs, service.require(Job, job.id))
    record = service.require(Job, job.id)
    assert record.result['benchmark_report']['warm_samples'] == 5
    assert mock.calls.count(('POST', '/prompt')) == 6
    assert len(record.result['artifact_ids']) == 7
    assert service.require(Scene, job.scene_id).data == original
    await execute(jobs, service.require(Job, job.id))
    assert mock.calls.count(('POST', '/prompt')) == 6


@pytest.mark.asyncio
async def test_benchmark_budget_stops_before_new_prompt(local, monkeypatch):
    from studio.benchmark import execute
    _, service, jobs, job, _ = local
    with service.sessions() as session:
        row = session.get(Job, job.id)
        row.kind, row.status = 'benchmark', 'running'
        row.snapshot = {**row.snapshot, 'benchmark': {'warm_shots': 5, 'max_wall_seconds': 300}}
        row.result = {'benchmark_elapsed_seconds': 301}
        session.commit()
    mock = ComfyMock(local, monkeypatch)
    with pytest.raises(ValueError, match='giới hạn benchmark'):
        await execute(jobs, service.require(Job, job.id))
    assert mock.calls == []


def test_runtime_flags_preserve_unrelated_launch_settings():
    from studio.runtime import merged_flags
    argv = ['/v/bin/python', '/v/ComfyUI/main.py', '--listen', '127.0.0.1', '--port', '8190',
            '--use-pytorch-cross-attention', '--lowvram', '--reserve-vram', '2', '--cache-classic']
    assert merged_flags(argv, {'attention_backend': 'sage', 'memory_policy': 'highvram'}) == [
        '--reserve-vram', '2', '--cache-classic', '--use-sage-attention', '--highvram']


def test_performance_uses_audio_rounding_and_keeps_unmeasured_eta_unknown(local, monkeypatch):
    from studio.performance import performance
    _, service, jobs, job, _ = local
    monkeypatch.setattr('studio.performance.probe', lambda p: {'duration': 12})
    config = clip_config(job.snapshot['project'], job.snapshot['scene'])
    path = service.job_directory(job.id) / 'clip-0.mp4'
    path.write_bytes(b'complete')
    service.artifact(path, job.project_id, path.name, job.id)
    jobs.checkpoint(job.id, 'clip-0', {'state': 'downloaded', 'config': config, 'runtime': {},
                                     'timing': {'total_seconds': 60}})
    result = performance(jobs, job.project_id)
    assert result['completed_shots'] == 1 and result['remaining_shots'] == 2
    assert result['eta_seconds'] == 120
    with service.sessions() as session:
        session.add(Scene(project_id=job.project_id, position=2, data={'title': 'No audio', 'steps': None}))
        session.commit()
    assert performance(jobs, job.project_id)['eta_seconds'] is None


def test_performance_does_not_display_old_jobs_after_scene_settings_change(local, monkeypatch):
    from studio.performance import performance
    _, service, jobs, job, _ = local
    monkeypatch.setattr('studio.performance.probe', lambda p: {'duration': 12})
    with service.sessions() as session:
        row = session.get(Scene, job.scene_id)
        row.data = {**row.data, 'clip_steps': 4}
        session.commit()
    result = performance(jobs, job.project_id)
    assert result['scenes'][0]['configs'][0]['steps'] == 4
    assert result['remaining_shots'] == 3


@pytest.mark.parametrize('cuda', ['12.8', None])
def test_benchmark_comparison_requires_complete_environment_evidence(cuda):
    from types import SimpleNamespace

    from studio.benchmark import compare
    runtime = {'devices': [{'name': 'A100', 'vram_total': 80 * 1024**3}],
               'comfy_version': 'pinned', 'torch_version': '2.8', 'cuda_version': cuda}
    reports = {}
    for id, seconds in [('baseline', 60), ('candidate', 30)]:
        result = {'benchmark_input_hash': 'same-input', 'warm_samples': 5,
                  'median_hot_wall_seconds': seconds, 'shots': {'shot': {'runtime': runtime}}}
        reports[id] = SimpleNamespace(kind='benchmark', status='completed', host_id='same-host',
                                      result={'benchmark_report': result})
    jobs = SimpleNamespace(service=SimpleNamespace(require=lambda model, id: reports[id]))
    if cuda:
        assert compare(jobs, 'baseline', 'candidate')['wall_speedup'] == 2
    else:
        with pytest.raises(ValueError, match='Comfy/Torch/CUDA'):
            compare(jobs, 'baseline', 'candidate')


def rife_mock(local, monkeypatch, *, fps):
    mock = ComfyMock(local, monkeypatch)
    source = mock.service.job_directory(mock.job.id) / 'source.mp4'
    source.write_bytes(b'saved clip fixture')
    mock.schema = {'Test': {'input': {'required': {}}, 'output': ['VIDEO']}}
    monkeypatch.setattr('studio.backend.graph_for', lambda *args, **kwargs: {'1': {'class_type': 'Test', 'inputs': {}}})
    monkeypatch.setattr('studio.backend.probe', lambda p: {'duration': 81 / 16, 'fps': fps})
    # ComfyUI lists the LoadVideo *input* as a preview before the SaveVideo result.
    mock.outputs = {'1': {'images': [{'filename': 'uploaded-source.mp4', 'subfolder': '', 'type': 'input'}], 'animated': [True]},
                    '6': {'images': [{'filename': 'rife_00001_.mp4', 'subfolder': 'studio', 'type': 'output'}], 'animated': [True]}}

    async def run():
        job = mock.service.require(Job, mock.job.id)
        return await mock.backend.generate(job, 'rife_post', '', [source], 0, 'draft', lambda message: None,
            lambda stage, value: mock.jobs.checkpoint(job.id, stage, value), 'rife-0')
    return mock, run


@pytest.mark.asyncio
async def test_rife_downloads_the_saved_result_not_the_loaded_input(local, monkeypatch):
    mock, run = rife_mock(local, monkeypatch, fps=48)
    await run()
    assert mock.viewed == [{'filename': 'rife_00001_.mp4', 'subfolder': 'studio', 'type': 'output'}]
    record = mock.service.require(Job, mock.job.id).result['submissions']['rife-0']
    assert record['output']['type'] == 'output' and record['state'] == 'downloaded'


@pytest.mark.asyncio
async def test_rife_result_that_is_not_interpolated_is_rejected(local, monkeypatch):
    mock, run = rife_mock(local, monkeypatch, fps=16)
    with pytest.raises(ValueError, match='48 fps'):
        await run()
    assert mock.service.require(Job, mock.job.id).result['submissions']['rife-0']['state'] != 'downloaded'


@pytest.mark.asyncio
async def test_only_input_previews_means_no_output(local, monkeypatch):
    mock, run = rife_mock(local, monkeypatch, fps=48)
    mock.outputs = {'1': {'images': [{'filename': 'uploaded-source.mp4', 'subfolder': '', 'type': 'input'}]}}
    with pytest.raises(ValueError, match='output đúng loại'):
        await run()
    assert mock.viewed == []
