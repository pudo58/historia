from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ghm import runpod
from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.models import Host
from ghm.security import SecretStore

POD = {'id': 'abc', 'name': 'wan', 'desiredStatus': 'RUNNING', 'costPerHr': 1.5, 'lastStartedAt': '2026-09-29 08:00:00.000 +0000 UTC',
       'publicIp': '1.2.3.4', 'portMappings': {'22': 40022}, 'gpu': {'count': 1, 'displayName': 'A100'}}
STOPPED = {'id': 'def', 'name': 'old', 'desiredStatus': 'EXITED', 'costPerHr': 2.0}


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'db'}", studio_root=tmp_path / 'data'),
                     SecretStore('runpod'), lambda h, s: FakeExecutor())
    with app.state.studio.sessions() as session:
        session.add(Host(label='A100', address='1.2.3.4', port=40022, username='root', auth_kind='password', encrypted_secret='x'))
        session.commit()
    return TestClient(app)


def test_unconfigured_then_configured(client, monkeypatch):
    assert client.get('/api/runpod/pods').json() == {'configured': False, 'pods': []}
    assert client.post('/api/settings/runpod-key', json={'token': 'short'}).status_code == 422
    assert client.post('/api/settings/runpod-key', json={'token': 'rpa_' + 'x' * 20}).json() == {'runpod_configured': True}
    seen = {}

    async def fake(key):
        seen['key'] = key
        return [POD, STOPPED]
    monkeypatch.setattr(runpod, 'fetch_pods', fake)
    data = client.get('/api/runpod/pods').json()
    assert seen['key'] == 'rpa_' + 'x' * 20 and 'rpa_' not in str(data)
    assert data['running_count'] == 1 and data['running_cost_per_hr'] == 1.5
    running = next(p for p in data['pods'] if p['id'] == 'abc')
    assert running['host_label'] == 'A100' and running['gpu'] == 'A100'
    assert client.post('/api/settings/runpod-key', json={'token': ''}).json() == {'runpod_configured': False}
    assert client.get('/api/runpod/pods').json()['configured'] is False


def test_api_error_is_reported_without_key(client, monkeypatch):
    client.post('/api/settings/runpod-key', json={'token': 'rpa_' + 'y' * 20})

    async def fail(key):
        raise runpod.RunPodError('RunPod từ chối API key.')
    monkeypatch.setattr(runpod, 'fetch_pods', fail)
    data = client.get('/api/runpod/pods').json()
    assert data['error'] == 'RunPod từ chối API key.' and data['pods'] == []


def test_uptime_and_session_cost():
    now = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    result = runpod.summarize([POD, STOPPED], [], now)
    pod = result['pods'][0]
    assert pod['uptime_seconds'] == 7200 and pod['session_cost'] == pytest.approx(3.0)
    assert result['pods'][1]['uptime_seconds'] is None and result['pods'][0]['host_id'] is None


def _configure(client, monkeypatch, tmp_path, pods):
    key = tmp_path / 'id_ed25519'
    key.write_text('fake')

    async def fake(_key):
        return pods
    monkeypatch.setattr(runpod, 'fetch_pods', fake)
    client.post('/api/settings/runpod-key', json={'token': 'rpa_' + 'k' * 20})
    return key


def test_connect_creates_host_then_repoints_after_restart(client, monkeypatch, tmp_path):
    fresh = {**POD, 'id': 'new1', 'name': 'wan-b', 'publicIp': '9.9.9.9', 'portMappings': {'22': 41000}}
    key = _configure(client, monkeypatch, tmp_path, [fresh])
    assert client.post('/api/runpod/pods/new1/connect').status_code == 409  # no SSH key chosen yet
    assert client.post('/api/settings/runpod-ssh-key', json={'token': str(tmp_path / 'missing')}).status_code == 422
    assert client.post('/api/settings/runpod-ssh-key', json={'token': str(key)}).status_code == 200
    created = client.post('/api/runpod/pods/new1/connect').json()
    assert created['action'] == 'created' and created['host']['address'] == '9.9.9.9' and created['host']['port'] == 41000
    assert created['host']['pinned_fingerprint'] is None  # trust still needs the fingerprint step
    assert client.post('/api/runpod/pods/new1/connect').json()['action'] == 'already_connected'
    # Pod restarted with a new ip/port: the linked host is re-pointed instead of duplicated.
    moved = {**fresh, 'publicIp': '8.8.8.8', 'portMappings': {'22': 42000}}

    async def fake2(_key):
        return [moved]
    monkeypatch.setattr(runpod, 'fetch_pods', fake2)
    listing = client.get('/api/runpod/pods').json()['pods'][0]
    assert listing['host_id'] is None and listing['linked_host_id'] == created['host']['id']
    updated = client.post('/api/runpod/pods/new1/connect').json()
    assert updated['action'] == 'address_updated' and updated['host']['id'] == created['host']['id']
    assert updated['host']['address'] == '8.8.8.8'
    assert len(client.get('/api/hosts').json()) == 2  # fixture host + one Pod host


def test_connect_rejects_pod_without_ssh(client, monkeypatch, tmp_path):
    key = _configure(client, monkeypatch, tmp_path, [{**POD, 'id': 'nossh', 'portMappings': {}}, STOPPED])
    client.post('/api/settings/runpod-ssh-key', json={'token': str(key)})
    assert client.post('/api/runpod/pods/nossh/connect').status_code == 409
    assert client.post('/api/runpod/pods/def/connect').status_code == 409
    assert client.post('/api/runpod/pods/none/connect').status_code == 404


PROXY_POD = {'id': '89m3wc8ffwy6na', 'name': 'guilty_amaranth_bee', 'desiredStatus': 'RUNNING', 'costPerHr': 0.28,
             'publicIp': '', 'portMappings': {}}
COMMAND = 'ssh 89m3wc8ffwy6na-64411bc2@ssh.runpod.io -i ~/.ssh/id_ed25519'


def test_proxy_connect_from_pasted_command(client, monkeypatch, tmp_path):
    key = _configure(client, monkeypatch, tmp_path, [PROXY_POD])
    client.post('/api/settings/runpod-ssh-key', json={'token': str(key)})
    body = client.post('/api/runpod/pods/89m3wc8ffwy6na/connect')
    assert body.status_code == 409 and 'lệnh SSH' in body.json()['detail']
    wrong = client.post('/api/runpod/pods/89m3wc8ffwy6na/connect', json={'ssh_command': 'ssh otherpod-abc123@ssh.runpod.io'})
    assert wrong.status_code == 409
    assert client.post('/api/runpod/pods/89m3wc8ffwy6na/connect', json={'mode': 'direct'}).status_code == 409
    created = client.post('/api/runpod/pods/89m3wc8ffwy6na/connect', json={'ssh_command': COMMAND}).json()
    host = created['host']
    assert created['action'] == 'created' and (host['address'], host['port'], host['username']) == ('ssh.runpod.io', 22, '89m3wc8ffwy6na-64411bc2')
    assert host['pinned_fingerprint'] is None
    listing = client.get('/api/runpod/pods').json()['pods'][0]
    assert listing['host_id'] == host['id'] and listing['host_label'] == 'guilty_amaranth_bee'
    assert client.post('/api/runpod/pods/89m3wc8ffwy6na/connect', json={'ssh_command': COMMAND}).json()['action'] == 'already_connected'


def test_proxy_username_from_api_and_direct_preferred(client, monkeypatch, tmp_path):
    exposed = {**PROXY_POD, 'machine': {'podHostId': '89m3wc8ffwy6na-aaaa1111'}}
    key = _configure(client, monkeypatch, tmp_path, [exposed])
    client.post('/api/settings/runpod-ssh-key', json={'token': str(key)})
    assert client.get('/api/runpod/pods').json()['pods'][0]['proxy_username'] == '89m3wc8ffwy6na-aaaa1111'
    assert client.post('/api/runpod/pods/89m3wc8ffwy6na/connect').json()['host']['username'] == '89m3wc8ffwy6na-aaaa1111'
    # A Pod that also exposes TCP 22 connects directly in auto mode.
    both = {**POD, 'id': 'dual1', 'publicIp': '5.5.5.5', 'portMappings': {'22': 43000}, 'machine': {'podHostId': 'dual1-bbbb2222'}}

    async def fake(_key):
        return [both]
    monkeypatch.setattr(runpod, 'fetch_pods', fake)
    direct = client.post('/api/runpod/pods/dual1/connect').json()['host']
    assert (direct['address'], direct['port'], direct['username']) == ('5.5.5.5', 43000, 'root')


def test_connection_errors_are_described():
    import socket

    from ghm.services.hosts import describe_connection_error as d
    assert 'chặn' in d(TimeoutError())
    assert 'tên miền' in d(socket.gaierror(-2, 'x'))
    assert 'từ chối' in d(ConnectionRefusedError())
    assert 'ValueError' in d(ValueError('boom'))


def test_ssh_errors_name_the_failing_stage():
    import asyncssh

    from ghm.services.hosts import describe_ssh_error as d
    assert 'SSH Keys' in d(asyncssh.PermissionDenied('denied'))
    assert 'hết thời gian' in d(TimeoutError())
