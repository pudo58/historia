import asyncio
import copy
import json
import shlex
import time

import pytest
from fastapi.testclient import TestClient

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.base import CommandResult
from ghm.executors.fake import FakeExecutor
from ghm.models import PreflightSnapshot
from ghm.schemas import HostOptions
from ghm.security import SecretStore
from studio.install_checks import summarize
from studio.installer import InstallExecutor, install_failure, private_python
from studio.installer import install as actual_install
from studio.models import Installation, Job
from studio.packs import PACK_ID, load_graph
from studio.service import canonical_hash


def sample_lock():
    return {'models': [], 'snapshots': [{'name': 'codec', 'repo': 'test/codec',
            'revision': 'a' * 40, 'size_bytes': 2,
            'files': [{'filename': 'config.json', 'size_bytes': 2,
                       'algorithm': 'sha256', 'digest': 'b' * 64}]}],
            'environments': {}, 'download_bytes': 2,
            'workflow_hashes': {n: canonical_hash(load_graph(n)) for n in ('qwen_image', 'qwen_edit', 'wan_i2v')}}


def test_access_checks_every_pinned_file(monkeypatch):
    from studio.packs import check_model_access
    calls = []
    monkeypatch.setattr('studio.packs.get_hf_file_metadata', lambda url, **kw: calls.append((url, kw)))
    lock = {'models': [{'name': 'image', 'repo': 'org/image', 'revision': 'abc', 'filename': 'a.bin'}],
            'snapshots': [{'name': 'codec', 'repo': 'org/codec', 'revision': 'def',
                           'files': [{'filename': 'model.onnx'}, {'filename': 'config.json'}]}]}
    check_model_access(lock)
    assert len(calls) == 3
    assert '/resolve/def/model.onnx' in calls[1][0]
    assert all(kw['token'] is False for _, kw in calls)


@pytest.mark.parametrize('status', [401, 403, 404, 500])
def test_access_error_is_actionable_and_redacted(monkeypatch, status):
    from types import SimpleNamespace

    from studio.packs import check_model_access
    def fail(*args, **kwargs):
        error = RuntimeError('hf_SECRET https://signed.example/private')
        error.response = SimpleNamespace(status_code=status)
        raise error
    monkeypatch.setattr('studio.packs.get_hf_file_metadata', fail)
    lock = {'models': [], 'snapshots': [{'name': 'codec', 'repo': 'org/codec',
            'revision': 'abc', 'files': [{'filename': 'model.onnx'}]}]}
    with pytest.raises(ValueError) as exc:
        check_model_access(lock, 'hf_SECRET')
    text = str(exc.value)
    assert 'org/codec/model.onnx' in text
    assert 'hf_SECRET' not in text and 'signed.example' not in text
    if status in (401, 403):
        assert 'token Hugging Face' in text


def test_prepare_access_failure_does_not_create_job(install_app, monkeypatch):
    client, app, hid, *_ = install_app
    def denied(*args):
        raise ValueError('Không có quyền tải codec/model.onnx')
    monkeypatch.setattr('studio.installations.check_model_access', denied)
    response = client.post(f'/api/studio/hosts/{hid}/installation/prepare',
                           json={'root': '/workspace/historia'})
    assert response.status_code == 409
    assert 'codec/model.onnx' in response.json()['detail']
    assert app.state.studio_jobs.list() == []


def sample_inventory():
    return {'files': [], 'free_bytes': 200*1024**3, 'filesystem_path': '/workspace',
            'writable': True, 'comfy_exists': False, 'managed': False, 'root_nonempty': False,
            'port_busy': False, 'python': [3, 12, 3], 'architecture': 'x86_64',
            'tools': {'flock': True, 'git': True}}


class PreviewExecutor(FakeExecutor):
    installed = False
    access_status = 200

    async def run_input(self, command, data, timeout=60, on_output=None):
        self.commands.append(command)
        if data == 'STUDIO_EXEC_OK':
            return CommandResult(0, data, '')
        payload = json.loads(data)
        if 'urls' in payload:
            assert len(payload['urls']) == 1
            assert "method='HEAD'" in shlex.split(command)[-1]
            return CommandResult(0, json.dumps({'ok': self.access_status == 200,
                'status': self.access_status, 'checked_files': len(payload['urls'])}), '')
        report = sample_inventory()
        report['comfy_exists'] = self.installed
        report['files'] = [{'state': 'valid' if self.installed else 'missing', 'size_bytes': 2}]
        return CommandResult(0, json.dumps(report), '')


@pytest.fixture
def install_app(tmp_path, monkeypatch):
    monkeypatch.setattr('studio.installations.resolve_pack', lambda token: sample_lock())
    # Metadata endpoint is mocked, while both local and Pod check orchestration remain exercised.
    monkeypatch.setattr('studio.packs.get_hf_file_metadata', lambda *args, **kwargs: None)
    executor = PreviewExecutor()
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path / "test.db"}', studio_root=tmp_path/'data'),
                     SecretStore('test'), lambda h, s: executor)
    async def fake_install(hosts, job, lock, log):
        log('Test installer completed; not a real GPU installation.')
        executor.installed = True
        return {'verified': False}
    monkeypatch.setattr('studio.installer.install', fake_install)
    with TestClient(app) as client:
        host = client.post('/api/hosts', json={'label':'test', 'address':'gpu.example.test', 'username':'root',
                                             'auth_kind':'password', 'secret':'secret'}).json()
        hid = host['id']
        hosts = app.state.host_service
        h = hosts._require_host(hid)
        h.pinned_fingerprint = 'SHA256:test'
        hosts._save(h)
        with hosts._sessions() as session:
            session.add(PreflightSnapshot(host_id=hid, status='pass', payload=json.dumps({
                'status':'pass', 'ssh_mode':'exec', 'gpu':{'name':'TEST GPU', 'vram_gb':32, 'driver_version':'580.1'},
                'checks':[]})))
            session.commit()
        yield client, app, hid, executor


def prepare(client, hid):
    result = client.post(f'/api/studio/hosts/{hid}/installation/prepare', json={
        'root':'/workspace/historia', 'remote_port':8190})
    assert result.status_code == 200, result.text
    return result.json()


def start(client, hid, plan):
    return client.post(f'/api/studio/hosts/{hid}/installation/start', json={
        'plan_id':plan['plan_id'], 'license_accepted':True})


def wait_job(client, jid):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        job = next(j for j in client.get('/api/studio/jobs').json() if j['id'] == jid)
        if job['status'] not in {'queued', 'running'}:
            return job
        time.sleep(.03)
    pytest.fail('worker did not finish')


def test_prepare_is_readonly_and_persisted(install_app):
    client, app, hid, executor = install_app
    before = app.state.host_service.options_for(hid)
    plan = prepare(client, hid)
    assert plan['required_bytes'] == 35*1024**3 + 2
    assert plan['remote_access'] == {'status': 'accessible', 'method': 'HEAD', 'origin': 'pod', 'checked_files': 1}
    assert app.state.host_service.options_for(hid) == before
    assert client.get('/api/studio/jobs').json() == []
    assert client.get(f'/api/studio/hosts/{hid}/installation').json()['plan'] == plan
    assert all('pip install' not in command and 'mkdir' not in command for command in executor.commands)


def test_pod_denied_access_does_not_create_plan_or_mutate(install_app):
    client, app, hid, executor = install_app
    executor.access_status = 401
    result = client.post(f'/api/studio/hosts/{hid}/installation/prepare', json={})
    assert result.status_code == 409
    assert '401/403' in result.json()['detail']
    assert app.state.studio_jobs.list() == []
    assert 'plan' not in client.get(f'/api/studio/hosts/{hid}/installation').json()
    assert all('pip install' not in command and 'mkdir' not in command for command in executor.commands)


def test_basic_ssh_can_prepare_without_full_ssh(install_app):
    client, app, hid, executor = install_app
    h = app.state.host_service._require_host(hid)
    h.address = 'ssh.runpod.io'
    app.state.host_service._save(h)
    result = client.post(f'/api/studio/hosts/{hid}/installation/prepare', json={})
    assert result.status_code == 200, result.text
    assert executor.commands
    assert client.get('/api/studio/jobs').json() == []


def test_consent_plan_binding_and_idempotent_submit(install_app):
    client, app, hid, _ = install_app
    plan = prepare(client, hid)
    url = f'/api/studio/hosts/{hid}/installation/start'
    assert client.post(url, json={'plan_id':plan['plan_id']}).status_code == 409
    assert client.post(url, json={'plan_id':'f'*64, 'license_accepted':True}).status_code == 409
    first = start(client, hid, plan)
    assert first.status_code == 200, first.text
    assert start(client, hid, plan).json()['id'] == first.json()['id']
    assert wait_job(client, first.json()['id'])['status'] == 'completed'
    state = client.get(f'/api/studio/hosts/{hid}/installation').json()
    assert state['status'] == 'installed'
    assert not client.get('/api/studio/status').json()['ai_ready']
    assert app.state.host_service.options_for(hid).root == '/workspace/historia'


def test_changed_identity_and_expired_preview_rejected(install_app):
    client, app, hid, _ = install_app
    plan = prepare(client, hid)
    h = app.state.host_service._require_host(hid)
    h.pinned_fingerprint = 'SHA256:changed'
    app.state.host_service._save(h)
    assert start(client, hid, plan).status_code == 409
    h.pinned_fingerprint = 'SHA256:test'
    app.state.host_service._save(h)
    with app.state.studio.sessions() as session:
        row = session.get(Installation, (hid, PACK_ID))
        data = copy.deepcopy(row.data)
        data['plan']['prepared_at'] = 1
        row.data = data
        session.commit()
    assert start(client, hid, plan).status_code == 409


@pytest.mark.parametrize('change', [
    {'free_bytes':0}, {'writable':False}, {'architecture':'aarch64'}, {'python':[3,10,0]},
    {'root_nonempty':True}, {'port_busy':True}, {'tools':{}},
    {'files':[{'state':'conflict','size_bytes':5}]},
])
def test_inventory_blockers(change):
    report = {**sample_inventory(), **change}
    assert summarize(report, HostOptions())['blockers']


def test_inventory_counts_only_missing_and_checks_adopt():
    report = sample_inventory()
    report['files'] = [{'state':'valid','size_bytes':100}, {'state':'missing','size_bytes':50}]
    result = summarize(report, HostOptions())
    assert result['missing_bytes'] == 50 and result['valid_files'] == 1
    assert summarize(report, HostOptions(adopt_existing=True))['blockers']
    report.update(comfy_exists=True,port_busy=True)
    assert not summarize(report, HostOptions(adopt_existing=True))['blockers']


def test_separate_model_volume_space_is_checked():
    report = sample_inventory()
    report['volumes'] = [
        {'path':'/workspace', 'free_bytes':200*1024**3, 'missing_bytes':0, 'reserve_bytes':35*1024**3, 'writable':True},
        {'path':'/ComfyUI/models', 'free_bytes':10, 'missing_bytes':100, 'reserve_bytes':512*1024**2, 'writable':True},
    ]
    result = summarize(report, HostOptions())
    assert '/ComfyUI/models' in result['blockers'][0]
    assert result['reserve_bytes'] == 35.5*1024**3


def test_safe_download_progress_filters_secrets_and_handles_chunks():
    class Executor(FakeExecutor):
        async def run(self, command, timeout=None, on_output=None):
            on_output('stdout', 'secret-token\nDownloaded by')
            on_output('stdout', 'tes: 268435456\nhttps://example.test?token=secret\n')
            return CommandResult(0, 'not persisted', '')
    async def check():
        logs = []
        await InstallExecutor(Executor(), '/workspace/historia').run('download', on_output=lambda *v: logs.append(v))
        assert logs == [('stdout', 'Đã tải 0.25 GiB của model hiện tại.')]
    asyncio.run(check())


def test_events_tail_returns_latest_not_first_500(install_app):
    from studio.models import JobEvent
    client, app, hid, _ = install_app
    with app.state.studio.sessions() as session:
        job = Job(host_id=hid, kind='install', status='interrupted', input_hash='test', snapshot={})
        session.add(job)
        session.flush()
        jid = job.id
        session.add_all([JobEvent(job_id=jid, message=str(i)) for i in range(510)])
        session.commit()
    events = client.get(f'/api/studio/jobs/{jid}/events?tail=true').json()
    assert len(events)==500 and events[0]['message']=='10' and events[-1]['message']=='509'


def test_arbitrary_workflow_blocked(install_app):
    client, _, hid, _ = install_app
    result = client.post(f'/api/studio/hosts/{hid}/installation/prepare', json={
        'workflow': {'1':{'class_type':'Unknown', 'inputs':{}}}})
    assert result.status_code == 422


def test_install_failure_never_becomes_verified(install_app, monkeypatch):
    client, _, hid, _ = install_app
    async def fail(*args):
        raise ValueError('Disk full')
    monkeypatch.setattr('studio.installer.install', fail)
    job = start(client, hid, prepare(client, hid)).json()
    assert wait_job(client, job['id'])['status'] == 'failed'
    assert client.get(f'/api/studio/hosts/{hid}/installation').json()['status'] == 'install_failed'
    assert client.post(f'/api/studio/hosts/{hid}/installation/verify', json={}).status_code == 409


def test_failed_install_can_retry_original_approved_snapshot(install_app, monkeypatch):
    client, app, hid, _ = install_app
    attempts = []
    async def fail_once(hosts, job, lock, log):
        attempts.append(job.snapshot)
        if len(attempts)==1:
            raise ValueError('test dependency conflict')
        return {'verified':False}
    monkeypatch.setattr('studio.installer.install', fail_once)
    job = start(client, hid, prepare(client, hid)).json()
    assert wait_job(client, job['id'])['status']=='failed'
    assert client.post(f"/api/studio/jobs/{job['id']}/resume", json={}).status_code==200
    assert wait_job(client, job['id'])['status']=='completed'
    assert attempts[0]==attempts[1]
    assert app.state.studio.require(Job,job['id']).snapshot==attempts[0]


def test_private_pip_ignores_template_constraint_without_host_changes():
    command = private_python('/workspace/historia/llm-venv/bin/python')
    assert '-u PIP_CONSTRAINT' in command and '-u PIP_BUILD_CONSTRAINT' in command
    assert 'PIP_CONFIG_FILE=/dev/null' in command
    assert 'rm ' not in command and 'unset ' not in command
    assert 'ResolutionImpossible' in install_failure(CommandResult(1,'ERROR: ResolutionImpossible hf_secret',''))
    assert 'hf_secret' not in install_failure(CommandResult(1,'ERROR: ResolutionImpossible hf_secret',''))


def test_recovery_preserves_snapshot(install_app):
    client, app, hid, _ = install_app
    plan = prepare(client, hid)
    with app.state.studio.sessions() as session:
        job = Job(host_id=hid, kind='install', status='running', input_hash=plan['plan_id'], snapshot=plan)
        session.add(job)
        session.commit()
        jid = job.id
    app.state.studio_jobs.recover()
    recovered = app.state.studio.require(Job, jid)
    assert recovered.status == 'interrupted'
    assert recovered.snapshot == plan
    assert client.get(f'/api/studio/hosts/{hid}/installation').json()['status'] == 'interrupted'


def test_remote_lock_blocks_overlap_and_drops_raw_logs():
    class Executor(FakeExecutor):
        async def run(self, command, timeout=None, on_output=None):
            assert command.startswith('flock -n -E 73 ')
            assert on_output is not None
            on_output('stdout', 'private-token\nhttps://example.test/?token=private-token\n')
            return CommandResult(73, 'private-token', '')
    async def check():
        executor = InstallExecutor(Executor(), '/workspace/historia')
        logs = []
        with pytest.raises(ValueError, match='khóa'):
            await executor.run('pip install something', on_output=lambda *v: logs.append(v))
        assert logs == []
    asyncio.run(check())


def test_cancel_install_retains_explicit_resume(install_app, monkeypatch):
    client, _, hid, _ = install_app
    async def pending(*args):
        await asyncio.sleep(60)
    monkeypatch.setattr('studio.installer.install', pending)
    job = start(client, hid, prepare(client, hid)).json()
    deadline = time.monotonic()+3
    while time.monotonic() < deadline:
        current = next(j for j in client.get('/api/studio/jobs').json() if j['id']==job['id'])
        if current['status']=='running':
            break
        time.sleep(.02)
    result = client.post(f"/api/studio/jobs/{job['id']}/cancel", json={})
    assert result.status_code == 200
    assert result.json()['status'] == 'interrupted'
    assert client.get(f'/api/studio/hosts/{hid}/installation').json()['status']=='interrupted'


def test_verification_requires_all_real_readable_outputs(install_app, tmp_path):
    import wave

    from PIL import Image

    from studio.media import run_ffmpeg
    client, app, hid, _ = install_app
    job = start(client, hid, prepare(client, hid)).json()
    wait_job(client, job['id'])
    tmp_path = app.state.studio.root
    image = tmp_path/'test.png'
    Image.new('RGB', (64, 64), 'blue').save(image)
    video = tmp_path/'test.mp4'
    run_ffmpeg(['-f','lavfi','-i','color=c=blue:s=64x64:d=0.3',str(video)])
    audio = tmp_path/'test.wav'
    with wave.open(str(audio), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        # Synthetic audible signal tests plumbing only, never actual TTS acceptance.
        import math
        import struct
        out.writeframes(b''.join(struct.pack('<h', int(4000 * math.sin(i / 10))) for i in range(24000 * 12)))
    from studio.remote_worker import inspect_wav
    measured = inspect_wav(audio)
    audio.with_suffix('.segments.json').write_text(json.dumps({
        'audio': measured, 'segments': [{'text': 'Synthetic test signal.', 'start': 0, 'end': 12}],
        'timestamp_kind': 'measured_tts_segments', 'word_aligned': False,
        'vieneu_version': 'test-only', 'device': 'cpu', 'elapsed_seconds': 0.1}), encoding='utf-8')

    class Backend:
        fail = False

        def __init__(self):
            self.calls = []

        async def generate(self, job, name, *args):
            self.calls.append(name)
            return video if name=='wan_i2v' else image

        async def speech(self, *args):
            self.calls.append('speech')
            return audio

        async def language(self, *args):
            self.calls.append('language')
            return {'ok':not self.fail}

    backend = Backend()
    app.state.studio_jobs.backend = backend
    result = client.post(f'/api/studio/hosts/{hid}/installation/verify', json={})
    assert result.status_code==200, result.text
    completed = wait_job(client, result.json()['id'])
    assert completed['status']=='completed', completed
    assert backend.calls==['qwen_image','qwen_edit','wan_i2v','speech','language']
    state = client.get(f'/api/studio/hosts/{hid}/installation').json()
    assert state['status']=='verified'
    assert len(state['verification']['artifact_ids'])==5
    assert state['all_components_verified']
    assert set(state['components']) == {'qwen_image', 'qwen_edit', 'wan_i2v', 'tts', 'llm'}
    assert state['components']['tts']['audio']['non_silent']
    assert state['components']['tts']['audio']['duration'] == 12
    assert state['components']['tts']['listening_approved'] is False
    for aid in state['verification']['artifact_ids']:
        assert client.get(f'/api/studio/artifacts/{aid}/file').status_code==200
    backend.fail = True
    result = client.post(f'/api/studio/hosts/{hid}/installation/verify', json={})
    assert wait_job(client, result.json()['id'])['status']=='failed'
    assert client.get(f'/api/studio/hosts/{hid}/installation').json()['status']=='verify_failed'
    assert not client.get('/api/studio/status').json()['ai_ready']


def test_disconnect_is_reconciling_not_success(install_app, monkeypatch):
    import asyncssh
    client, _, hid, _ = install_app
    async def lost(*args):
        raise asyncssh.ConnectionLost('test disconnect')
    monkeypatch.setattr('studio.installer.install', lost)
    job = start(client, hid, prepare(client, hid)).json()
    assert wait_job(client, job['id'])['status']=='reconciling'
    assert client.get(f'/api/studio/hosts/{hid}/installation').json()['status']=='interrupted'


def test_actual_installer_orchestrates_isolated_environments(install_app, monkeypatch):
    import shlex
    from types import SimpleNamespace

    _client, app, hid, _ = install_app
    hosts = app.state.host_service
    commands = []

    class Executor(FakeExecutor):
        async def run(self, command, timeout=None, on_output=None):
            command = shlex.split(command)[-1] if command.startswith('flock ') else command
            commands.append(command)
            if command.startswith(('test -x ', 'test -f ')):
                return CommandResult(1, '', '')
            return CommandResult(0, '', '')

        async def run_input(self, command, data, timeout=60, on_output=None):
            payload = json.loads(data)
            if 'urls' in payload:
                command = shlex.split(command)[-1] if command.startswith('flock ') else command
                commands.append(command)
                assert "method='HEAD'" in shlex.split(command)[-1]
                return CommandResult(0, json.dumps({'ok': True, 'checked_files': len(payload['urls'])}), '')
            return await self.run(command, timeout, on_output)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, path):
            return SimpleNamespace(is_success=False)

    async def check(*args):
        return 'test-comfy-version'

    async def inspect(*args):
        return sample_inventory()

    # Exercise the actual orchestration, but never execute shell commands or download models.
    executor = Executor()
    monkeypatch.setattr(hosts, 'executor_for', lambda h: executor)
    monkeypatch.setattr('studio.installer.inventory', inspect)
    monkeypatch.setattr('studio.installer.httpx.AsyncClient', Client)
    monkeypatch.setattr('studio.installer.check_backend', check)
    options = HostOptions(root='/workspace/historia', remote_port=8190)
    job = SimpleNamespace(host_id=hid, snapshot={'options':options.model_dump()})
    lock = sample_lock()
    lock['environments'] = {'llm':'transformers==5.17.0','tts':'vieneu==3.8.3'}
    result = asyncio.run(actual_install(hosts, job, lock, lambda line: None))
    assert not result['verified']
    assert any('/llm-venv' in c and 'python3 -m venv' in c for c in commands)
    assert any('/tts-venv' in c and 'python3 -m venv' in c for c in commands)
    assert any('torch==2.11.0' in c for c in commands)
    assert not any('kill ' in c or 'nvidia-driver' in c for c in commands)


def test_24gb_cards_reported_below_24_gib_are_accepted():
    from studio.installer import MIN_VRAM_GIB
    assert 23.89 >= MIN_VRAM_GIB and 23.99 >= MIN_VRAM_GIB and 16 < MIN_VRAM_GIB


def test_install_failure_shows_redacted_tail():
    from ghm.executors.base import CommandResult
    from studio.installer import install_failure
    out = 'Collecting x\nfrom https://hf.co/x?sig=abc token=hf_ABCDEFGHIJKL\nError: something broke badly\n'
    msg = install_failure(CommandResult(1, out, ''))
    assert 'something broke badly' in msg and 'hf_ABCDEFGHIJKL' not in msg and 'sig=abc' not in msg
    assert 'ensurepip' in install_failure(CommandResult(1, 'Error: ensurepip is not available', ''))


def run_real_install(install_app, monkeypatch, *, parallel, fail=None):
    """Actual install() orchestration with a scripted executor; no shell, no network."""
    from types import SimpleNamespace

    from studio.installer import PARALLEL_PROBE

    _client, app, hid, _ = install_app
    hosts = app.state.host_service
    events, locks = [], {}

    class Executor(FakeExecutor):
        async def run(self, command, timeout=None, on_output=None):
            if command == PARALLEL_PROBE:
                await asyncio.sleep(0.05)
                return CommandResult(0 if parallel else 1, 'historia-channel-ok' if parallel else '', '')
            inner = shlex.split(command)[-1] if command.startswith('flock ') else command
            if inner.startswith(('test -x ', 'test -f ')):
                return CommandResult(1, '', '')
            if 'git -C' in inner and 'checkout --detach' in inner:
                events.append('git')
            if 'torch==2.11.0' in inner:
                events.append('torch-start')
                await asyncio.sleep(0.05)
                events.append('torch-end')
                if fail == 'pip':
                    return CommandResult(1, 'ERROR: ResolutionImpossible', '')
            return CommandResult(0, '', '')

        async def run_input(self, command, data, timeout=60, on_output=None):
            payload = json.loads(data)
            if 'urls' in payload:
                return CommandResult(0, json.dumps({'ok': True, 'checked_files': len(payload['urls'])}), '')
            if 'items' in payload:
                locks['models'] = shlex.split(command)[4]
                events.append('download-start')
                try:
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    events.append('download-cancelled')
                    raise
                events.append('download-end')
                if on_output:
                    on_output('stdout', 'Downloaded bytes: 2 of 2\nModel ready: codec/config.json\nhf_secret\n')
                if fail == 'download':
                    return CommandResult(1, 'Model failed: codec/config.json (Checksum mismatch)', '')
                return CommandResult(0, '', '')
            return await self.run(command, timeout, on_output)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, path):
            return SimpleNamespace(is_success=False)

    async def check(*args):
        return 'test-comfy-version'

    async def inspect(*args):
        return sample_inventory()

    executor = Executor()
    monkeypatch.setattr(hosts, 'executor_for', lambda h: executor)
    monkeypatch.setattr('studio.installer.inventory', inspect)
    monkeypatch.setattr('studio.installer.httpx.AsyncClient', Client)
    monkeypatch.setattr('studio.installer.check_backend', check)
    options = HostOptions(root='/workspace/historia', remote_port=8190)
    job = SimpleNamespace(host_id=hid, snapshot={'options': options.model_dump()})
    logs = []
    try:
        result = asyncio.run(actual_install(hosts, job, sample_lock(), logs.append))
    except ValueError as exc:
        result = exc
    return result, events, locks, logs


def test_models_download_while_dependencies_install(install_app, monkeypatch):
    result, events, locks, logs = run_real_install(install_app, monkeypatch, parallel=True)
    assert not isinstance(result, Exception) and not result['verified']
    # Checkout happens first (it needs an empty directory), then both run at the same time.
    assert events[0] == 'git'
    assert events.index('torch-start') < events.index('download-end')
    assert events.index('download-start') < events.index('torch-end')
    assert locks['models'].endswith('-models.lock')
    assert 'Tải model song song với cài dependency.' in logs
    assert 'Đã kiểm tra checksum: codec/config.json' in logs
    assert not any('hf_secret' in line for line in logs)


def test_sequential_fallback_when_channels_do_not_overlap(install_app, monkeypatch):
    result, events, _, logs = run_real_install(install_app, monkeypatch, parallel=False)
    assert not isinstance(result, Exception)
    assert events == ['git', 'download-start', 'download-end', 'torch-start', 'torch-end']
    assert any('tải model xong rồi mới cài dependency' in line for line in logs)


def test_dependency_failure_cancels_download_and_reports_stage(install_app, monkeypatch):
    result, events, _, _ = run_real_install(install_app, monkeypatch, parallel=True, fail='pip')
    assert isinstance(result, ValueError) and 'ResolutionImpossible' in str(result)
    assert 'download-cancelled' in events and 'download-end' not in events


def test_download_failure_names_file_without_secrets(install_app, monkeypatch):
    result, _, _, _ = run_real_install(install_app, monkeypatch, parallel=False, fail='download')
    assert isinstance(result, ValueError)
    assert str(result).startswith('codec/config.json: ')


def test_parallel_probe_rejects_serialized_channels():
    from studio.installer import PARALLEL_PROBE, parallel_channels

    class Serial(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.lock = asyncio.Lock()

        async def run(self, command, timeout=None, on_output=None):
            assert command == PARALLEL_PROBE
            async with self.lock:
                await asyncio.sleep(2)
            return CommandResult(0, 'historia-channel-ok', '')

    class Broken(FakeExecutor):
        async def run(self, command, timeout=None, on_output=None):
            raise ConnectionError('gateway refused a second channel')

    assert asyncio.run(parallel_channels(Serial())) is False
    assert asyncio.run(parallel_channels(Broken())) is False


def test_batch_progress_reaches_log_through_run_input():
    class Executor(FakeExecutor):
        async def run_input(self, command, data, timeout=60, on_output=None):
            on_output('stdout', 'Downloaded bytes: 268435456 of 1073741824\nModel ready: wan-high\n'
                                'Model ready: bad name; rm -rf /\nhttps://x?token=secret\n')
            return CommandResult(0, '', '')
    async def check():
        logs = []
        await InstallExecutor(Executor(), '/workspace/historia', scope='models').run_input(
            'download', '{}', on_output=lambda *v: logs.append(v[1]))
        assert logs == ['Đã tải 0.25 / 1.00 GiB model.', 'Đã kiểm tra checksum: wan-high']
    asyncio.run(check())
