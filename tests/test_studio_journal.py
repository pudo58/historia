import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from studio.models import Job, JobEvent, ProductionRun
from studio.schemas import ProjectInput, SceneInput


@pytest.fixture
def journal(tmp_path):
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path / "journal.db"}', studio_root=tmp_path / 'data'),
                     SecretStore('test'), lambda h, s: FakeExecutor())
    service, jobs = app.state.studio, app.state.studio_jobs
    project = service.create_project(ProjectInput(title='Film', topic='Test'))
    scene = service.add_scene(project['id'], SceneInput(title='Cảnh cũ'))
    with service.sessions() as session:
        job = Job(project_id=project['id'], scene_id=scene['id'], kind='clip', status='paused',
                  snapshot={'project': project, 'scene': scene}, input_hash='journal')
        session.add(job)
        session.flush()
        session.add(JobEvent(job_id=job.id, message='legacy event'))
        session.commit()
    return TestClient(app), service, jobs, project, scene, job


def test_journal_filters_cursor_and_legacy(journal):
    client, _, jobs, project, scene, job = journal
    jobs.event(job.id, 'upload completed', stage='clip-0')
    jobs.event(job.id, 'failed example', level='error', stage='clip-1')
    url = f'/api/studio/projects/{project["id"]}/events'
    response = client.get(url, params={'limit': 2})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page['has_more'] and len(page['items']) == 2
    old = client.get(url, params={'before': page['items'][0]['id']}).json()
    assert old['items'][0]['legacy'] and old['items'][0]['level'] is None
    assert old['items'][0]['scene_title'] == scene['title']
    assert client.get(url, params={'after': page['items'][-1]['id']}).json()['items'] == []
    assert len(client.get(url, params={'q': 'FAILED', 'level': 'error'}).json()['items']) == 1
    stage = client.get(url, params={'stage': 'clip-0'}).json()['items']
    assert [e['message'] for e in stage] == ['legacy event', 'upload completed']
    assert client.get(url, params={'after': 1, 'before': 2}).status_code == 422
    # Existing per-job API remains a list.
    assert len(client.get(f'/api/studio/jobs/{job.id}/events').json()) == 3


def test_export_includes_all_pages_without_mutation(journal):
    client, service, _, project, _, job = journal
    with service.sessions() as session:
        session.add_all([JobEvent(job_id=job.id, message=f'event {i}', context={'level': 'info'}) for i in range(1205)])
        session.commit()
    url = f'/api/studio/projects/{project["id"]}/events/export'
    response = client.get(url)
    assert response.status_code == 200
    records = [json.loads(line) for line in response.text.splitlines()]
    assert len(records) == 1206 and len({e['id'] for e in records}) == 1206
    assert service.require(Job, job.id).status == 'paused'
    filtered = client.get(url, params={'q': 'event 1204'}).text.splitlines()
    assert len(filtered) == 1


def test_project_run_scope_and_checkpoint_events(journal):
    client, service, jobs, project, _, job = journal
    with service.sessions() as session:
        run = ProductionRun(project_id=project['id'], idempotency_key='test', snapshot=project,
                            checkpoint={'jobs': {'clip:scene': job.id}}, consent={})
        session.add(run)
        session.commit()
    jobs.checkpoint(job.id, 'clip-0', {'state': 'submitted', 'prompt_id': 'same-id'})
    jobs.checkpoint(job.id, 'clip-0', {'state': 'submitted', 'timing': {'remote_wait_seconds': 3}})
    jobs.patch(job.id, error='Explicit failure', status='failed')
    records = client.get(f'/api/studio/projects/{project["id"]}/events', params={'run_id': run.id}).json()['items']
    assert len(records) == 3
    assert records[1]['stage'] == 'clip-0' and records[2]['level'] == 'error'
    other = service.create_project(ProjectInput(title='Other', topic='Other'))
    assert client.get(f'/api/studio/projects/{other["id"]}/events', params={'run_id': run.id}).status_code == 404
    assert client.get(f'/api/studio/projects/{other["id"]}/events', params={'job_id': job.id}).status_code == 404


def test_event_migration_backs_up_and_preserves_history(tmp_path):
    db = tmp_path / 'old.db'
    settings = Settings(database_url=f'sqlite:///{db}', studio_root=tmp_path / 'data')
    app = create_app(settings, SecretStore('test'), lambda h, s: FakeExecutor())
    with app.state.studio.sessions() as session:
        job = Job(kind='clip', status='paused', snapshot={}, input_hash='legacy')
        session.add(job)
        session.flush()
        session.add(JobEvent(job_id=job.id, message='preserve me'))
        session.commit()
    with sqlite3.connect(db) as connection:
        connection.execute('ALTER TABLE studio_job_events DROP COLUMN context')
    create_app(settings, SecretStore('test'), lambda h, s: FakeExecutor())
    backups = list((tmp_path / 'backups').glob('*.db'))
    assert len(backups) == 1
    for path in [db, backups[0]]:
        with sqlite3.connect(path) as connection:
            assert connection.execute('SELECT message FROM studio_job_events').fetchone() == ('preserve me',)
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT context FROM studio_job_events').fetchone() == (None,)
    create_app(settings, SecretStore('test'), lambda h, s: FakeExecutor())
    assert len(list((tmp_path / 'backups').glob('*.db'))) == 1
