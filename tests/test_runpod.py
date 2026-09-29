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
