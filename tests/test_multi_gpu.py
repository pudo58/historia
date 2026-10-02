"""One ComfyUI per GPU on a multi-GPU Pod ("lanes"): process supervision, connect, stop guard, install options."""
import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ghm import runpod
from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.models import PreflightSnapshot
from ghm.preflight import parse_nvidia_smi
from ghm.remote_service import SCRIPT
from ghm.schemas import HostOptions
from ghm.security import SecretStore

MAIN = '''import os, pathlib, sys, time
pathlib.Path(__file__).parent.parent.joinpath("seen-" + sys.argv[sys.argv.index("--port") + 1] + ".txt").write_text(
    os.environ.get("CUDA_VISIBLE_DEVICES", "-") + "|" + " ".join(sys.argv[1:]))
time.sleep(60)
'''


def service(root, action, port, gpu=None, args=(), instance=0):
    result = subprocess.run([sys.executable, '-c', SCRIPT], input=json.dumps(
        {'root': str(root), 'port': port, 'args': list(args), 'action': action, 'gpu_index': gpu, 'instance': instance}),
        capture_output=True, text=True, timeout=30)
    return result


@pytest.fixture
def root(tmp_path):
    comfy = tmp_path / 'ComfyUI'
    comfy.mkdir()
    (comfy / 'main.py').write_text(MAIN)
    (tmp_path / 'venv/bin').mkdir(parents=True)
    os.symlink(sys.executable, tmp_path / 'venv/bin/python')
    yield tmp_path
    for gpu in (None, 0, 1):
        service(tmp_path, 'stop', 0, gpu)
    service(tmp_path, 'stop', 0, 0, instance=1)


def test_each_gpu_gets_its_own_process_marker_log_and_device(root):
    assert service(root, 'start', 8190).returncode == 0
    assert service(root, 'start', 8191, gpu=1, args=['--highvram']).returncode == 0
    time.sleep(1)
    assert (root / '.ghm-process.json').exists() and (root / '.ghm-process-gpu1.json').exists()
    assert (root / 'comfyui.log').exists() and (root / 'comfyui-gpu1.log').exists()
    plain = (root / 'seen-8190.txt').read_text()
    pinned = (root / 'seen-8191.txt').read_text()
    assert plain.startswith('-|') and '--cuda-device' not in plain            # classic single process: untouched
    assert pinned.startswith('1|') and '--cuda-device 1' in pinned and '--highvram' in pinned
    # Stopping one lane leaves the other running.
    assert service(root, 'stop', 8191, gpu=1).returncode == 0
    assert service(root, 'inspect', 8190).returncode == 0
    assert service(root, 'inspect', 8191, gpu=1).returncode != 0


def test_restart_flags_never_duplicate_cuda_device(root):
    assert service(root, 'start', 8191, gpu=1, args=['--cuda-device', '0', '--lowvram']).returncode == 0
    time.sleep(1)
    seen = (root / 'seen-8191.txt').read_text()
    assert seen.count('--cuda-device') == 1 and '--cuda-device 1' in seen and '--lowvram' in seen


def test_gpu_index_validation_and_gpu_count():
    assert HostOptions().gpu_index is None and HostOptions(gpu_index=1).gpu_index == 1
    with pytest.raises(ValidationError):
        HostOptions(gpu_index=-1)
    listing = ('NVIDIA A100 80GB PCIe, 81920, 570.86, 8.0\nNVIDIA A100 80GB PCIe, 81920, 570.86, 8.0\n'
               'GPU 0: NVIDIA A100 80GB PCIe (UUID: GPU-a)\nGPU 1: NVIDIA A100 80GB PCIe (UUID: GPU-b)\n')
    assert parse_nvidia_smi(listing).count == 2
    assert parse_nvidia_smi('NVIDIA A100, 81920, 570.86, 8.0\nGPU 0: NVIDIA A100 (UUID: GPU-a)\n').count == 1
    assert parse_nvidia_smi('NVIDIA A100, 81920, 570.86, 8.0\n').count is None   # older/unlisted: unknown, not "1"


TWO = {'id': 'duo', 'name': 'two-a100', 'desiredStatus': 'RUNNING', 'costPerHr': 3.21,
       'lastStartedAt': '2026-09-30 08:00:00.000 +0000 UTC', 'publicIp': '5.6.7.8', 'portMappings': {'22': 43000},
       'gpu': {'count': 2, 'displayName': 'A100 PCIe'}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'db'}", studio_root=tmp_path / 'data'),
                     SecretStore('gpu'), lambda h, s: FakeExecutor())
    key = tmp_path / 'id_ed25519'
    key.write_text('fake')

    async def pods(_key):
        return [TWO]
    monkeypatch.setattr(runpod, 'fetch_pods', pods)
    calls = []

    async def act(_key, pod_id, action):
        calls.append((pod_id, action))
    monkeypatch.setattr(runpod, 'pod_action', act)
    test = TestClient(app)
    test.post('/api/settings/runpod-key', json={'token': 'rpa_' + 'k' * 20})
    test.post('/api/settings/runpod-ssh-key', json={'token': str(key)})
    test.calls = calls
    return test


def test_connect_all_gpus_creates_one_host_per_gpu_and_is_idempotent(client):
    single = client.post('/api/runpod/pods/duo/connect').json()
    assert single['action'] == 'created' and len(single['lanes']) == 1
    both = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True}).json()
    assert both['action'] == 'already_connected' and [h['label'] for h in both['lanes']] == ['two-a100', 'two-a100 · GPU 1']
    assert {(h['address'], h['port']) for h in both['lanes']} == {('5.6.7.8', 43000)}
    assert len(client.get('/api/hosts').json()) == 2
    again = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True}).json()
    assert len(client.get('/api/hosts').json()) == 2 and [h['id'] for h in again['lanes']] == [h['id'] for h in both['lanes']]
    pod = client.get('/api/runpod/pods').json()['pods'][0]
    assert pod['gpu_count'] == 2 and len(pod['host_ids']) == 2 and [x['index'] for x in pod['lanes']] == [0, 1]


def test_pod_restart_repoints_every_lane(client, monkeypatch):
    client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True})
    moved = {**TWO, 'publicIp': '9.9.9.9', 'portMappings': {'22': 44000}}

    async def pods(_key):
        return [moved]
    monkeypatch.setattr(runpod, 'fetch_pods', pods)
    client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True})
    hosts = client.get('/api/hosts').json()
    assert len(hosts) == 2 and {(h['address'], h['port']) for h in hosts} == {('9.9.9.9', 44000)}


def test_stop_is_blocked_when_only_the_second_gpu_is_busy(client):
    from studio.models import Job
    from studio.schemas import ProjectInput
    lanes = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True}).json()['lanes']
    studio = client.app.state.studio
    project = studio.create_project(ProjectInput(title='F', topic='T'))
    with studio.sessions() as session:
        session.add(Job(project_id=project['id'], host_id=lanes[1]['id'], kind='clip', status='running', input_hash='h', snapshot={}))
        session.commit()
    blocked = client.post('/api/runpod/pods/duo/action', json={'action': 'stop'})
    assert blocked.status_code == 409 and client.calls == []
    assert client.post('/api/runpod/pods/duo/action', json={'action': 'stop', 'force': True}).status_code == 200
    assert client.calls == [('duo', 'stop')]


def test_extra_gpu_install_options_follow_gpu_zero(client, monkeypatch):
    lanes = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True}).json()['lanes']
    first, second = lanes[0]['id'], lanes[1]['id']
    app = client.app
    installs = app.state.studio_jobs.installations
    hosts = app.state.host_service
    for host_id in (first, second):
        host = hosts._require_host(host_id)
        host.pinned_fingerprint = 'SHA256:x'
        hosts._save(host)
        with hosts._sessions() as session:
            session.add(PreflightSnapshot(host_id=host_id, status='pass', payload=json.dumps({
                'status': 'pass', 'ssh_mode': 'exec', 'checks': [],
                'gpu': {'name': 'A100', 'vram_gb': 80, 'driver_version': '580.1', 'count': 2}})))
            session.commit()

    async def no_preflight(_id):
        return None
    monkeypatch.setattr(hosts, 'preflight', no_preflight)
    import asyncio
    with pytest.raises(ValueError, match='GPU 0'):
        asyncio.run(installs.discover(second))                                   # GPU 0 not installed yet
    hosts.save_options(first, HostOptions(root='/workspace/historia', remote_port=8190))
    from studio.models import Installation
    from studio.packs import PACK_ID
    with app.state.studio.sessions() as session:
        session.add(Installation(host_id=first, pack_id=PACK_ID, status='not_installed', data={}))
        session.commit()
    installs.patch(first, 'verified')
    suggested = asyncio.run(installs.discover(second))['suggested_options']
    assert (suggested['root'], suggested['remote_port'], suggested['gpu_index'], suggested['adopt_existing']) == ('/workspace/historia', 8191, 1, False)
    hosts.save_options(first, HostOptions(root='/workspace/historia', remote_port=8188, adopt_existing=True, comfy_path='/ComfyUI'))
    with pytest.raises(ValueError, match='adopt'):
        asyncio.run(installs.discover(second))


def test_pod_idle_waits_for_every_gpu(client):
    from studio.models import Job
    from studio.schemas import ProjectInput
    lanes = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True}).json()['lanes']
    studio = client.app.state.studio
    project = studio.create_project(ProjectInput(title='F', topic='T', host_id=lanes[0]['id']))
    with studio.sessions() as session:
        session.add(Job(project_id=project['id'], host_id=lanes[1]['id'], kind='clip', status='running', input_hash='h', snapshot={}))
        session.commit()
    advice = client.get(f"/api/studio/projects/{project['id']}/pod-idle").json()
    assert advice['state'] == 'busy', advice


def test_gpu_count_comes_from_the_machine_when_runpod_omits_it(client, monkeypatch):
    """RunPod sometimes returns no GPU block; connect must ask the machine (nvidia-smi) instead of assuming one GPU."""
    bare = {k: v for k, v in TWO.items() if k != 'gpu'}

    async def pods(_key):
        return [bare]
    monkeypatch.setattr(runpod, 'fetch_pods', pods)
    first = client.post('/api/runpod/pods/duo/connect').json()['host']
    hosts = client.app.state.host_service
    host = hosts._require_host(first['id'])
    host.pinned_fingerprint = 'SHA256:x'
    hosts._save(host)
    listing = client.get('/api/runpod/pods').json()['pods'][0]
    assert listing['gpu_count_known'] is False and listing['gpu_count'] is None

    async def preflight(host_id):
        with hosts._sessions() as session:
            session.add(PreflightSnapshot(host_id=host_id, status='pass', payload=json.dumps({
                'status': 'pass', 'ssh_mode': 'exec', 'checks': [],
                'gpu': {'name': 'A40', 'vram_gb': 45, 'driver_version': '570.1', 'count': 2}})))
            session.commit()
    monkeypatch.setattr(hosts, 'preflight', preflight)
    result = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True}).json()
    assert [h['label'] for h in result['lanes']] == ['two-a100', 'two-a100 · GPU 1']
    listing = client.get('/api/runpod/pods').json()['pods'][0]
    assert listing['gpu_count_known'] is True and listing['gpu_count'] == 2 and listing['gpu'] == 'A40'


def test_second_process_on_one_gpu_has_its_own_marker_log_and_port(root):
    assert service(root, 'start', 8191, gpu=0).returncode == 0
    assert service(root, 'start', 8192, gpu=0, instance=1).returncode == 0
    time.sleep(1)
    assert (root / '.ghm-process-gpu0.json').exists() and (root / '.ghm-process-gpu0-i1.json').exists()
    assert (root / 'comfyui-gpu0.log').exists() and (root / 'comfyui-gpu0-i1.log').exists()
    assert (root / 'seen-8191.txt').read_text().startswith('0|') and (root / 'seen-8192.txt').read_text().startswith('0|')
    assert service(root, 'stop', 8192, gpu=0, instance=1).returncode == 0
    assert service(root, 'inspect', 8191, gpu=0).returncode == 0           # the first process keeps running


def _preflight(client, host_id, vram):
    hosts = client.app.state.host_service
    host = hosts._require_host(host_id)
    host.pinned_fingerprint = 'SHA256:x'
    hosts._save(host)
    with hosts._sessions() as session:
        session.add(PreflightSnapshot(host_id=host_id, status='pass', payload=json.dumps({
            'status': 'pass', 'ssh_mode': 'exec', 'checks': [],
            'gpu': {'name': 'A100', 'vram_gb': vram, 'driver_version': '580.1', 'count': 2}})))
        session.commit()


def test_several_processes_per_gpu_need_known_and_sufficient_vram(client):
    first = client.post('/api/runpod/pods/duo/connect').json()['host']['id']
    unknown = client.post('/api/runpod/pods/duo/connect', json={'per_gpu': 2})
    assert unknown.status_code == 409 and 'Kiểm tra máy' in unknown.json()['detail']
    _preflight(client, first, 40)
    too_many = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True, 'per_gpu': 2})
    assert too_many.status_code == 422 and '1 tiến trình' in too_many.json()['detail']
    assert len(client.get('/api/hosts').json()) == 1


def test_processes_sharing_a_gpu_become_lanes_pinned_to_that_gpu(client, monkeypatch):
    first = client.post('/api/runpod/pods/duo/connect').json()['host']['id']
    _preflight(client, first, 140)
    over = client.post('/api/runpod/pods/duo/connect', json={'per_gpu': 3})
    assert over.status_code == 422 and '2 tiến trình' in over.json()['detail']
    lanes = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True, 'per_gpu': 2}).json()['lanes']
    assert [h['label'] for h in lanes] == ['two-a100', 'two-a100 · GPU 1', 'two-a100 · GPU 0 · tiến trình 2',
                                           'two-a100 · GPU 1 · tiến trình 2']
    again = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True, 'per_gpu': 2}).json()['lanes']
    assert [h['id'] for h in again] == [h['id'] for h in lanes] and len(client.get('/api/hosts').json()) == 4
    studio = client.app.state.studio_jobs.installations
    assert [studio.lane(h['id'])['gpu'] for h in lanes] == [0, 1, 0, 1]
    assert [studio.lane(h['id'])['instance'] for h in lanes] == [0, 0, 1, 1]
    # A later connect without the flags keeps what was decided.
    client.post('/api/runpod/pods/duo/connect')
    assert studio.lane(lanes[2]['id'])['instance'] == 1


def test_install_options_of_a_second_process_on_gpu_zero(client, monkeypatch):
    import asyncio

    from studio.models import Installation
    from studio.packs import PACK_ID
    first = client.post('/api/runpod/pods/duo/connect').json()['host']['id']
    _preflight(client, first, 140)
    lanes = client.post('/api/runpod/pods/duo/connect', json={'all_gpus': True, 'per_gpu': 2}).json()['lanes']
    hosts, installs = client.app.state.host_service, client.app.state.studio_jobs.installations
    for lane in lanes[1:]:
        _preflight(client, lane['id'], 80)

    async def no_preflight(_id):
        return None
    monkeypatch.setattr(hosts, 'preflight', no_preflight)
    hosts.save_options(first, HostOptions(root='/workspace/historia', remote_port=8190))
    with client.app.state.studio.sessions() as session:
        session.add(Installation(host_id=first, pack_id=PACK_ID, status='not_installed', data={}))
        session.commit()
    installs.patch(first, 'verified')
    suggested = asyncio.run(installs.discover(lanes[2]['id']))['suggested_options']
    assert (suggested['gpu_index'], suggested['instance'], suggested['remote_port']) == (0, 1, 8192)


def test_options_saved_before_shared_gpus_still_match():
    """Install snapshots from before the 'instance' option must keep comparing equal, or proven installs look unproven."""
    options = HostOptions(root='/workspace/historia', remote_port=8190, gpu_index=1)
    old_snapshot_options = {k: v for k, v in options.model_dump().items() if k != 'instance'}
    assert options.model_dump() == old_snapshot_options
    assert HostOptions(root='/workspace/historia', gpu_index=0, instance=1).model_dump()['instance'] == 1
    assert HostOptions.model_validate_json(options.model_dump_json()).model_dump() == options.model_dump()
