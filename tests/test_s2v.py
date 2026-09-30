"""Wan2.2 S2V (the character speaks): model group, workflow graph, trial job. Offline, no GPU."""
import json
import subprocess
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from studio.backend import RemoteBackend
from studio.comfy_schema import normalize_graph
from studio.media import ffmpeg, probe
from studio.models import Artifact, Job, Scene
from studio.packs import (
    OPTIONAL_MODEL_FILES,
    OPTIONAL_MODEL_GROUPS,
    OPTIONAL_SHA256,
    S2V_AUDIO_ENCODER,
    S2V_LORA,
    S2V_MODEL,
    graph_for,
    load_graph,
    optional_members,
    optional_names,
    s2v_chunks,
)
from studio.schemas import ProjectInput, SceneInput

FIXTURE = Path(__file__).parent / 'fixtures' / 'comfy_s2v_object_info.json'
DRAFT = {'render_profile': 'draft', 'aspect_ratio': '16:9', 'output_resolution': '720p'}


def s2v(chunks, **kwargs):
    return graph_for('wan_s2v', kwargs.pop('prompt', 'A speaker.'), kwargs.pop('seed', 7), 'draft',
                     ['keyframe.png', 'speech.wav'], 'studio/job/dialogue-0', project_settings=DRAFT,
                     chunks=chunks, **kwargs)


def test_chunks_cover_the_audio_and_never_undercount():
    # Each chunk is 5 s of audio but the decoded film is 2 frames (0.125 s) short of that.
    assert [s2v_chunks(x) for x in (0.5, 4.8, 4.9, 5, 9.8, 10, 14.8, 14.9)] == [1, 1, 2, 2, 2, 3, 3, 4]
    with pytest.raises(ValueError):
        s2v_chunks(0)


def test_graph_passes_the_real_comfyui_schema_for_every_chunk_count():
    # tests/fixtures/comfy_s2v_object_info.json is /object_info of ComfyUI ee71d5c (the pinned commit).
    schema = json.loads(FIXTURE.read_text())
    for chunks in (1, 2, 3):
        graph = normalize_graph(s2v(chunks), schema)
        assert graph['65']['class_type'] == 'SaveVideo' and graph['65']['inputs']['format.codec'] == 'h264'


def test_extensions_chain_the_accumulated_latent_and_the_tail_decodes_it_once():
    graph = s2v(3)
    assert graph['510']['inputs']['video_latent'] == ['41', 0]
    assert graph['512']['inputs'] == {'samples1': ['41', 0], 'samples2': ['511', 0], 'dim': 't'}
    assert graph['520']['inputs']['video_latent'] == ['512', 0]
    assert graph['522']['inputs']['samples1'] == ['512', 0] and graph['522']['inputs']['samples2'] == ['521', 0]
    # The tail prepends the first latent frame and drops the 3 frames it adds, like the official template.
    assert graph['60']['inputs']['samples'] == ['522', 0] and graph['61']['inputs']['samples2'] == ['522', 0]
    assert graph['63']['inputs']['batch_index'] == 3
    seeds = [graph[k]['inputs']['seed'] for k in ('41', '511', '521')]
    assert len(set(seeds)) == 3
    for extend in ('510', '520'):
        inputs = graph[extend]['inputs']
        assert inputs['audio_encoder_output'] == ['33', 0] and inputs['ref_image'] == ['34', 0] and inputs['length'] == 77
    # A single chunk has no extension and the tail reads the first sampler.
    single = s2v(1)
    assert not any(k.startswith('5') for k in single)
    assert single['60']['inputs']['samples'] == ['41', 0] and single['61']['inputs']['samples2'] == ['41', 0]


def test_inputs_are_applied_and_the_packaged_graph_is_not_mutated():
    before = json.dumps(load_graph('wan_s2v'), sort_keys=True)
    graph = s2v(2, prompt='Trần Hưng Đạo nói.')
    assert graph['21']['inputs']['text'] == 'Trần Hưng Đạo nói.'
    assert graph['34']['inputs']['image'] == 'keyframe.png' and graph['32']['inputs']['audio'] == 'speech.wav'
    assert (graph['40']['inputs']['width'], graph['40']['inputs']['height']) == (768, 432)
    assert graph['65']['inputs']['filename_prefix'] == 'studio/job/dialogue-0'
    assert json.dumps(load_graph('wan_s2v'), sort_keys=True) == before


def test_bad_requests_are_refused_before_any_gpu_time():
    for chunks in (0, 4):
        with pytest.raises(ValueError, match='đoạn'):
            s2v(chunks)
    with pytest.raises(ValueError, match='ảnh keyframe'):
        graph_for('wan_s2v', 'x', 1, 'draft', ['only-image.png'], 'p', project_settings=DRAFT)
    with pytest.raises(ValueError, match='16'):
        from studio.packs import s2v_graph
        s2v_graph(load_graph('wan_s2v'), 'x', 1, 'a.png', 'a.wav', 'p', 770, 432, 1)


def test_graph_model_names_are_exactly_the_files_the_group_installs():
    graph = load_graph('wan_s2v')
    names = {Path(OPTIONAL_MODEL_FILES[m][1]).name for m in OPTIONAL_MODEL_GROUPS['wan-s2v']}
    used = {graph['10']['inputs']['unet_name'], graph['11']['inputs']['lora_name'],
            graph['31']['inputs']['audio_encoder_name']}
    assert used == names == {S2V_MODEL, S2V_LORA, S2V_AUDIO_ENCODER}
    # The text encoder and VAE are the ones the base install already has.
    from studio.packs import MODEL_FILES
    base = {Path(f).name for _, _, f, _ in MODEL_FILES}
    assert {graph['20']['inputs']['clip_name'], graph['30']['inputs']['vae_name']} <= base
    with pytest.raises(ValueError):
        load_graph('not_allowed')


def test_every_optional_file_is_pinned_to_a_reviewed_checksum():
    assert set(OPTIONAL_MODEL_GROUPS['wan-s2v']) <= set(OPTIONAL_SHA256)
    assert all(len(v) == 64 for v in OPTIONAL_SHA256.values())
    assert optional_members('wan-s2v') == OPTIONAL_MODEL_GROUPS['wan-s2v']
    assert optional_members('rife-v4.26') == ('rife-v4.26',)
    # A member is installed through its group only, so the UI never offers 3 loose files.
    with pytest.raises(ValueError):
        optional_members('s2v-model')
    assert 'wan-s2v' in optional_names() and 's2v-model' not in optional_names()
    assert {'qwen-image-lightning-fp8', 'rife-v4.26'} <= set(optional_names())


# ---------------------------------------------------------------- the group install job

def add_host(client, jobs, label='main'):
    host = client.post('/api/hosts', json={'label': label, 'address': 'gpu.example.test', 'username': 'root',
                                           'auth_kind': 'password', 'secret': 'secret'}).json()
    stored = jobs.hosts._require_host(host['id'])
    stored.pinned_fingerprint = 'SHA256:test'
    jobs.hosts._save(stored)
    return host['id']


@pytest.fixture
def studio(tmp_path, monkeypatch):
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path / "s2v.db"}', studio_root=tmp_path / 'data'),
                     SecretStore('s2v'), lambda h, s: FakeExecutor())
    service, jobs = app.state.studio, app.state.studio_jobs
    client = TestClient(app)
    host_id = add_host(client, jobs)
    monkeypatch.setattr(jobs.installations, 'state', lambda host: {'status': 'installed', 'components': {}})
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda *a: True)
    return client, service, jobs, host_id


def fake_resolve(monkeypatch):
    def resolve(name, token=None):
        repo, filename, destination = OPTIONAL_MODEL_FILES[name]
        return {'name': name, 'repo': repo, 'filename': filename, 'destination': destination, 'revision': 'a' * 40,
                'sha256': OPTIONAL_SHA256.get(name, 'b' * 64), 'size_bytes': 1_000_000_000, 'gated': False,
                'enabled': True, 'license_accepted': False, 'profiles': [], 'notes': '', 'url': None}
    monkeypatch.setattr('studio.packs.resolve_optional_model', resolve)


def test_group_is_one_job_with_all_three_files_and_ready_only_when_all_are_verified(studio, monkeypatch):
    client, service, jobs, host = studio
    fake_resolve(monkeypatch)
    assert jobs.optional_ready(host, 'wan-s2v') is False
    listing = client.get(f'/api/studio/hosts/{host}/optional-models').json()
    assert listing['wan-s2v'] is False and 's2v-model' not in listing and 'rife-v4.26' in listing
    created = client.post(f'/api/studio/hosts/{host}/optional-models/wan-s2v')
    assert created.status_code == 200, created.text
    job = service.require(Job, created.json()['id'])
    assert job.kind == 'optional_model'
    assert [a['name'] for a in job.snapshot['assets']] == list(OPTIONAL_MODEL_GROUPS['wan-s2v'])
    assert client.post(f'/api/studio/hosts/{host}/optional-models/s2v-model').status_code == 409

    downloads = []
    async def download_many(executor, items, roots, token, timeout, **kwargs):
        downloads.append((items, roots, kwargs))
        return ''
    monkeypatch.setattr('ghm.model_download.download_many', download_many)
    monkeypatch.setattr('studio.packs.check_model_access', lambda lock, token=None: downloads.append(('access', lock)))
    import asyncio
    asyncio.run(jobs.execute_optional(job))
    items = downloads[1][0]
    assert [i['name'] for i in items] == list(OPTIONAL_MODEL_GROUPS['wan-s2v'])
    assert {i['target'].rsplit('/', 2)[-2] for i in items} == {'diffusion_models', 'audio_encoders', 'loras'}
    assert {i['digest'] for i in items} == {OPTIONAL_SHA256[m] for m in OPTIONAL_MODEL_GROUPS['wan-s2v']}
    assert downloads[1][2].get('workers') == 1        # multi-GB files: one at a time
    assert len(downloads[0][1]['models']) == 3       # access is checked for every file first
    assert jobs.optional_ready(host, 'wan-s2v') is True
    assert client.get(f'/api/studio/hosts/{host}/optional-models').json()['wan-s2v'] is True
    # Losing one member (e.g. the settings entry of the LoRA) makes the whole group not ready.
    jobs.hosts.save_setting(f'studio_optional:{host}:s2v-lightning-lora', '')
    assert jobs.optional_ready(host, 'wan-s2v') is False


def test_download_failure_marks_nothing_ready(studio, monkeypatch):
    _, _, jobs, host = studio
    fake_resolve(monkeypatch)
    job = jobs.service.require(Job, jobs.install_optional(host, 'wan-s2v')['id'])
    async def failing(*args, **kwargs):
        raise ValueError('Insufficient free space on the model volume for the missing files; nothing was downloaded.')
    monkeypatch.setattr('ghm.model_download.download_many', failing)
    monkeypatch.setattr('studio.packs.check_model_access', lambda lock, token=None: None)
    import asyncio
    with pytest.raises(ValueError, match='free space'):
        asyncio.run(jobs.execute_optional(job))
    assert jobs.optional_ready(host, 'wan-s2v') is False


def test_single_file_optional_jobs_still_work(studio, monkeypatch):
    _, _, jobs, host = studio
    fake_resolve(monkeypatch)
    job = jobs.service.require(Job, jobs.install_optional(host, 'rife-v4.26')['id'])
    assert len(job.snapshot['assets']) == 1 and job.snapshot['asset']['name'] == 'rife-v4.26'
    legacy = dict(job.snapshot)
    legacy.pop('assets')
    legacy.pop('group')
    seen = []
    async def download_many(executor, items, *args, **kwargs):
        seen.append([i['name'] for i in items])
    monkeypatch.setattr('ghm.model_download.download_many', download_many)
    monkeypatch.setattr('studio.packs.check_model_access', lambda lock, token=None: None)
    import asyncio
    old = type('J', (), {'id': job.id, 'host_id': host, 'snapshot': legacy})()   # a job queued by an older version
    asyncio.run(jobs.execute_optional(old))
    assert seen == [['rife-v4.26']] and jobs.optional_ready(host, 'rife-v4.26')


# ---------------------------------------------------------------- the one-line trial

def make_scene(service, host_id, *, keyframe=True, project_extra=None):
    project = service.create_project(ProjectInput(title='Phim', topic='Bạch Đằng', host_id=host_id, voice='Voice A',
                                                  **(project_extra or {})))
    scene = service.add_scene(project['id'], SceneInput(title='Cảnh 1', narration='Lời dẫn.', visual_prompt='A general on a ship.'))
    image = service.root / 'key.png'
    image.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (64, 36), 'navy').save(image)
    artifact = service.artifact(image, project['id'], 'keyframe.png')
    if keyframe:
        # A production run reviews keyframes in the run itself, so the scene keeps keyframe_approved=False.
        with service.sessions() as session:
            row = session.get(Scene, scene['id'])
            row.data = {**row.data, 'keyframe_id': artifact['id'], 'keyframe_approved': False, 'script_approved': True}
            session.commit()
    return project['id'], scene['id'], artifact['id']


def test_trial_is_refused_until_everything_it_needs_exists(studio, monkeypatch):
    client, service, jobs, host = studio
    pid, sid, _ = make_scene(service, host)
    body = {'scene_id': sid, 'text': 'Ta đã thắng.'}
    no_models = client.post(f'/api/studio/projects/{pid}/dialogue-test', json=body)
    assert no_models.status_code == 409 and 'S2V' in no_models.json()['detail']
    monkeypatch.setattr(jobs, 'optional_ready', lambda host_id, name: name == 'wan-s2v')
    assert client.post(f'/api/studio/projects/{pid}/dialogue-test', json={**body, 'text': 'a'}).status_code == 422
    assert client.post(f'/api/studio/projects/{pid}/dialogue-test', json={**body, 'scene_id': 'nope'}).status_code == 409
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda *a: False)
    assert 'Giọng đọc chưa sẵn sàng' in client.post(f'/api/studio/projects/{pid}/dialogue-test', json=body).json()['detail']
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda *a: True)
    ok = client.post(f'/api/studio/projects/{pid}/dialogue-test', json=body)
    assert ok.status_code == 201, ok.text
    job = service.require(Job, ok.json()['id'])
    assert (job.kind, job.host_id, job.scene_id) == ('dialogue_test', host, sid)
    assert job.snapshot['project']['render_profile'] == 'draft' and job.snapshot['project']['tts_device'] == 'cuda'
    assert job.snapshot['request'] == {'text': 'Ta đã thắng.', 'voice': 'Voice A'}
    # A double click does not pay twice while the first trial is still queued or running.
    again = client.post(f'/api/studio/projects/{pid}/dialogue-test', json=body)
    assert again.status_code == 409 and 'đang được thử' in again.json()['detail']


def test_trial_needs_a_keyframe_and_a_gpu_but_not_an_approved_keyframe(studio, monkeypatch):
    client, service, jobs, host = studio
    monkeypatch.setattr(jobs, 'optional_ready', lambda *a: True)
    pid, sid, _ = make_scene(service, host, keyframe=False)
    assert 'đã có ảnh' in client.post(f'/api/studio/projects/{pid}/dialogue-test',
                                     json={'scene_id': sid, 'text': 'Xin chào.'}).json()['detail']
    pid, sid, _ = make_scene(service, host)          # keyframe generated by a production run: not approved on the scene
    assert client.post(f'/api/studio/projects/{pid}/dialogue-test',
                       json={'scene_id': sid, 'text': 'Xin chào.'}).status_code == 201
    pid, sid, _ = make_scene(service, None)
    assert 'Chọn GPU' in client.post(f'/api/studio/projects/{pid}/dialogue-test',
                                    json={'scene_id': sid, 'text': 'Xin chào.'}).json()['detail']


def wav(path, seconds, rate=24000):
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b'\x00\x01' * int(rate * seconds))
    return path


def talking_video(path, seconds, fps=16):
    subprocess.run([ffmpeg(), '-nostdin', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                    f'testsrc=size=64x36:rate={fps}:duration={seconds}', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)],
                   check=True)
    return path


def queue_trial(monkeypatch, studio, text='Ta đã thắng.'):
    client, service, jobs, host = studio
    monkeypatch.setattr(jobs, 'optional_ready', lambda *a: True)
    pid, sid, key = make_scene(service, host)
    job_id = client.post(f'/api/studio/projects/{pid}/dialogue-test', json={'scene_id': sid, 'text': text}).json()['id']
    return service, jobs, service.require(Job, job_id), key


@pytest.mark.asyncio
async def test_trial_makes_the_voice_then_the_talking_clip_and_puts_the_voice_under_it(studio, monkeypatch):
    service, jobs, job, key = queue_trial(monkeypatch, studio)
    calls = []
    async def speech(current, text, voice, log):
        calls.append(('speech', text, voice))
        return wav(service.job_directory(current.id) / 'speech.wav', 6.0)
    async def generate(current, name, prompt, images, seed, quality, log, checkpoint, stage, *a):
        calls.append(('generate', name, [Path(p).name for p in images], quality, stage))
        assert 'lip movements' in prompt and 'A general on a ship.' in prompt
        return talking_video(service.job_directory(current.id) / 'dialogue-0.mp4', 9.9)   # two 5 s chunks minus 2 frames
    monkeypatch.setattr(jobs.backend, 'speech', speech)
    monkeypatch.setattr(jobs.backend, 'generate', generate)
    await jobs.execute_dialogue_test(job)
    assert calls[0] == ('speech', 'Ta đã thắng.', 'Voice A')
    kind, name, files, quality, stage = calls[1]
    assert (kind, name, quality, stage) == ('generate', 'wan_s2v', 'draft', 'dialogue-0')
    assert files[1] == 'speech.wav' and files[0].endswith('.png')
    saved = service.require(Job, job.id).result
    assert len(saved['artifact_ids']) == 1
    out = service.artifact_path(saved['artifact_ids'][0])
    info = probe(out)
    assert info['audio'] and abs(info['duration'] - 6.0) < .15          # cut to the speech, not the 9.9 s of chunks
    assert service.require(Artifact, saved['artifact_ids'][0]).data['text'] == 'Ta đã thắng.'
    # Only the trial was recorded: the scene keeps its own narration, speech and clips.
    scene = service.project(job.project_id)['scenes'][0]
    assert not scene.get('clip_ids') and not scene.get('speech_id')


@pytest.mark.asyncio
async def test_line_too_long_for_three_chunks_stops_before_the_expensive_step(studio, monkeypatch):
    service, jobs, job, _ = queue_trial(monkeypatch, studio, 'Một câu rất dài. ' * 20)
    async def speech(current, text, voice, log):
        return wav(service.job_directory(current.id) / 'speech.wav', 16.0)
    async def generate(*args, **kwargs):
        raise AssertionError('S2V must not run for audio it cannot cover')
    monkeypatch.setattr(jobs.backend, 'speech', speech)
    monkeypatch.setattr(jobs.backend, 'generate', generate)
    with pytest.raises(ValueError, match='tối đa 14'):
        await jobs.execute_dialogue_test(job)
    assert service.require(Job, job.id).result.get('speech_pending') is False


@pytest.mark.asyncio
async def test_retry_reuses_the_voice_and_the_finished_clip(studio, monkeypatch):
    service, jobs, job, _ = queue_trial(monkeypatch, studio)
    counts = {'speech': 0, 'generate': 0}
    async def speech(current, text, voice, log):
        counts['speech'] += 1
        return wav(service.job_directory(current.id) / 'speech.wav', 4.0)
    async def generate(current, *args, **kwargs):
        counts['generate'] += 1
        if counts['generate'] == 1:
            raise ValueError('ComfyUI xử lý thất bại.')
        return talking_video(service.job_directory(current.id) / 'dialogue-0.mp4', 4.875)
    monkeypatch.setattr(jobs.backend, 'speech', speech)
    monkeypatch.setattr(jobs.backend, 'generate', generate)
    with pytest.raises(ValueError, match='ComfyUI'):
        await jobs.execute_dialogue_test(job)
    await jobs.execute_dialogue_test(service.require(Job, job.id))
    assert counts == {'speech': 1, 'generate': 2}           # the TTS was not paid for again
    await jobs.execute_dialogue_test(service.require(Job, job.id))
    assert counts == {'speech': 1, 'generate': 2}           # a finished trial is not rendered twice


@pytest.mark.asyncio
async def test_a_clip_shorter_than_the_voice_is_never_muxed(studio, monkeypatch):
    service, jobs, job, _ = queue_trial(monkeypatch, studio)
    async def speech(current, text, voice, log):
        return wav(service.job_directory(current.id) / 'speech.wav', 6.0)
    async def generate(current, *args, **kwargs):
        return talking_video(service.job_directory(current.id) / 'dialogue-0.mp4', 3.0)
    monkeypatch.setattr(jobs.backend, 'speech', speech)
    monkeypatch.setattr(jobs.backend, 'generate', generate)
    with pytest.raises(ValueError, match='ngắn hơn giọng'):
        await jobs.execute_dialogue_test(job)


# ---------------------------------------------------------------- the ComfyUI transport

class Comfy:
    """Just enough ComfyUI for one S2V submission."""
    def __init__(self, service, job, monkeypatch, *, video_seconds=9.875, fps=16):
        self.service, self.job = service, job
        self.uploads, self.posted, self.schema_reads = [], [], 0
        self.backend = RemoteBackend(None, service)
        self.video = (video_seconds, fps)
        self.submitted = {}
        monkeypatch.setattr('studio.backend.asyncio.sleep', self.no_sleep)
        real = probe

        def fake_probe(path):
            if Path(path).suffix == '.wav':
                return {'duration': 9.6}
            if Path(path).suffix == '.mp4':
                seconds, rate = self.video
                return {'duration': seconds, 'video_duration': seconds, 'fps': rate, 'width': 768, 'height': 432}
            return real(path)
        monkeypatch.setattr('studio.backend.probe', fake_probe)
        monkeypatch.setattr('studio.packs.probe', fake_probe, raising=False)

        @asynccontextmanager
        async def connection(host_id):
            async with httpx.AsyncClient(base_url='http://comfy', transport=httpx.MockTransport(self.handle)) as client:
                yield None, client
        self.backend.connection = connection

    @staticmethod
    async def no_sleep(seconds):
        return None

    def handle(self, request):
        path = request.url.path
        if path.startswith('/history/'):
            id = path.rsplit('/', 1)[-1]
            history = {'status': {'status_str': 'success', 'completed': True, 'messages': []},
                       'outputs': {'65': {'images': [{'filename': 'dialogue_00001_.mp4', 'subfolder': 'studio', 'type': 'output'}]}}}
            return httpx.Response(200, json={id: history} if id in self.submitted else {})
        if path == '/queue':
            return httpx.Response(200, json={'queue_running': [], 'queue_pending': []})
        if path == '/system_stats':
            return httpx.Response(200, json={'system': {'comfyui_version': 'test', 'argv': []}, 'devices': []})
        if path == '/upload/image':
            self.uploads.append(request.content)
            return httpx.Response(200, json={'name': f'upload-{len(self.uploads)}', 'subfolder': ''})
        if path == '/object_info':
            self.schema_reads += 1
            return httpx.Response(200, json=json.loads(FIXTURE.read_text()) | {
                'LoadImage': {'input': {'required': {'image': ['COMBO', {'options': [f'upload-{n}' for n in range(1, 9)]}]}}, 'output': ['IMAGE', 'MASK']},
                'LoadAudio': {'input': {'required': {'audio': ['COMBO', {'options': [f'upload-{n}' for n in range(1, 9)]}]}}, 'output': ['AUDIO']}})
        if path == '/prompt':
            data = json.loads(request.content)
            self.posted.append(data['prompt'])
            self.submitted[data['prompt_id']] = data
            return httpx.Response(200, json={'prompt_id': data['prompt_id']})
        if path == '/view':
            return httpx.Response(200, content=b'video')
        raise AssertionError(path)

    async def run(self, jobs, keyframe, audio, stage='dialogue-0'):
        job = self.service.require(Job, self.job.id)
        return await self.backend.generate(job, 'wan_s2v', 'A speaker.', [keyframe, audio], 42, 'draft',
                                           lambda message: None, lambda name, value: jobs.checkpoint(job.id, name, value), stage)


@pytest.fixture
def transport(studio, monkeypatch):
    service, jobs, job, key = queue_trial(monkeypatch, studio)
    voice = service.root / 'voice.wav'
    wav(voice, .2)
    return service, jobs, job, service.artifact_path(key), voice


@pytest.mark.asyncio
async def test_submission_uploads_the_image_padded_and_the_voice_unchanged(transport, monkeypatch):
    service, jobs, job, keyframe, voice = transport
    comfy = Comfy(service, job, monkeypatch)
    out = await comfy.run(jobs, keyframe, voice)
    assert out.suffix == '.mp4' and out.read_bytes() == b'video'
    assert len(comfy.uploads) == 2
    assert b'RIFF' in comfy.uploads[1] and voice.read_bytes() in comfy.uploads[1]       # the WAV went up as is
    assert b'PNG' in comfy.uploads[0] and keyframe.read_bytes() not in comfy.uploads[0]  # the image was fitted first
    graph = comfy.posted[0]
    assert graph['34']['inputs']['image'] == 'upload-1' and graph['32']['inputs']['audio'] == 'upload-2'
    assert '510' in graph and '520' not in graph        # 9.6 s of speech -> 2 chunks (one extension)
    assert (graph['40']['inputs']['width'], graph['40']['inputs']['height']) == (768, 432)
    assert comfy.schema_reads == 1                      # read after the uploads so LoadAudio/LoadImage list them
    record = service.require(Job, job.id).result['submissions']['dialogue-0']
    assert record['state'] == 'downloaded' and record['graph_hash']


@pytest.mark.asyncio
async def test_clip_that_does_not_cover_the_voice_or_has_wrong_fps_is_rejected(transport, monkeypatch):
    service, jobs, job, keyframe, voice = transport
    for video, message in (((5.0, 16), 'ngắn hơn giọng nói'), ((9.9, 24), 'FPS clip S2V')):
        comfy = Comfy(service, job, monkeypatch, video_seconds=video[0], fps=video[1])
        with pytest.raises(ValueError, match=message):
            await comfy.run(jobs, keyframe, voice, f'dialogue-{video[1]}')


@pytest.mark.asyncio
async def test_voice_audition_saves_one_playable_artifact_per_voice(studio, monkeypatch):
    client, service, jobs, host = studio
    pid, _sid, _key = make_scene(service, host)
    response = client.post(f'/api/studio/projects/{pid}/voice-audition', json={'text': 'Các khanh bình thân.'})
    assert response.status_code == 201
    assert client.post(f'/api/studio/projects/{pid}/voice-audition', json={'text': 'Câu khác.'}).status_code == 409   # one at a time
    job = service.require(Job, response.json()['id'])
    seen = []
    async def audition(current, text, log):
        seen.append(text)
        return [{'voice': name, 'label': name, 'duration': 1.0,
                 'path': str(wav(service.job_directory(current.id) / f'audition-{name}.wav', 1.0))} for name in ('Binh', 'Ly')]
    monkeypatch.setattr(jobs.backend, 'audition', audition)
    await jobs.execute_voice_audition(job)
    voices = service.require(Job, job.id).result['voices']
    assert seen == ['Các khanh bình thân.'] and [v['voice'] for v in voices] == ['Binh', 'Ly']
    assert service.artifact_path(voices[1]['artifact_id']).name == 'audition-Ly.wav'
    await jobs.execute_voice_audition(job)      # a retry after success does not synthesize again
    assert len(seen) == 1


def test_voice_audition_needs_a_gpu_and_a_real_line(studio):
    client, service, _jobs, host = studio
    pid, _sid, _key = make_scene(service, host)
    assert client.post(f'/api/studio/projects/{pid}/voice-audition', json={'text': ' '}).status_code in (400, 422)
    bare = service.create_project(ProjectInput(title='Không GPU', topic='x'))
    assert client.post(f'/api/studio/projects/{bare["id"]}/voice-audition', json={'text': 'Xin chào.'}).status_code == 409


def test_worker_auditions_every_preset_voice_the_model_lists(tmp_path):
    from studio.remote_worker import audition_voices
    class Model:
        def list_preset_voices(self):
            return [('Bình', 'Binh'), ('Ly', 'Ly')]
        def get_preset_voice(self, name):
            return name
        def infer(self, text, voice):
            return voice
        def save(self, audio, path):
            wav(Path(path), 1.0 if audio == 'Binh' else 1.5)
    result = audition_voices(Model(), 'Xin chào', str(tmp_path / 'speech.wav'))['audition']
    assert [(v['voice'], v['duration']) for v in result] == [('Binh', 1.0), ('Ly', 1.5)]
    assert all(Path(v['file']).name.startswith('audition-') for v in result)
