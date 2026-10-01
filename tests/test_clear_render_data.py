from sqlalchemy import select
import pytest
from fastapi.testclient import TestClient

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from studio.models import Artifact, Job, JobEvent, ProductionRun, Scene, Source
from studio.schemas import ProjectInput, SceneInput, TextSourceInput


def build(tmp_path):
    made = create_app(Settings(database_url=f'sqlite:///{tmp_path / "c.db"}', studio_root=tmp_path / 'data'),
                      SecretStore('k'), lambda h, s: FakeExecutor())
    service = made.state.studio
    project = service.create_project(ProjectInput(title='Phim', topic='t'))
    source = service.add_text(project['id'], TextSourceInput(title='Tư liệu', text='Quân Nguyên sang đánh.'))
    scene = service.add_scene(project['id'], SceneInput(title='Cảnh 1', narration='Lời đọc.', visual_prompt='A river'))

    def make(name, job_id=None, content=b'x'):
        path = service.job_directory(job_id) / name if job_id else service.root / 'uploads' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content * 10)
        return service.artifact(path, project['id'], name, job_id)['id']

    with service.sessions() as session:
        speech_job = Job(project_id=project['id'], scene_id=scene['id'], kind='speech', status='completed', input_hash='a', snapshot={})
        image_job = Job(project_id=project['id'], scene_id=scene['id'], kind='keyframe', status='completed', input_hash='b', snapshot={})
        session.add_all([speech_job, image_job])
        session.commit()
        speech_job_id, image_job_id = speech_job.id, image_job.id
        session.add(JobEvent(job_id=image_job_id, message='xong'))
        session.add(ProductionRun(project_id=project['id'], idempotency_key='k', status='failed', snapshot={}, consent={},
                                  checkpoint={'jobs': {'keyframe:1': image_job_id}}))
        session.commit()
    upload = make('reference.png')
    speech = make('speech.wav', speech_job_id)
    keyframe = make('keyframe.png', image_job_id)
    clip = make('clip-1.mp4', image_job_id)
    with service.sessions() as session:
        row = session.get(Scene, scene['id'])
        row.data = {**row.data, 'speech_id': speech, 'duration': 4.2, 'shot_count': 2, 'keyframe_id': keyframe,
                    'keyframe_approved': True, 'clip_ids': [clip], 'clip_approved': True, 'script_approved': True}
        session.commit()
    with service.sessions() as session:
        session.get(Source, source['id']).data = {**source, 'artifact_id': upload}
        session.commit()
    return made, service, project, scene, dict(upload=upload, speech=speech, keyframe=keyframe, clip=clip,
                                                speech_job=speech_job_id, image_job=image_job_id)


def counts(service):
    with service.sessions() as session:
        return (session.query(Artifact).count(), session.query(Job).count(), session.query(ProductionRun).count(),
                session.query(JobEvent).count())


def test_clear_everything_keeps_script_and_inputs_and_removes_generated_files(tmp_path):
    made, service, project, scene, ids = build(tmp_path)
    client = TestClient(made)
    result = client.post(f"/api/studio/projects/{project['id']}/clear-render-data", json={}).json()
    assert result['runs'] == 1 and result['jobs'] == 2 and result['artifacts'] == 3 and not result['kept_speech']
    assert counts(service) == (1, 0, 0, 0)  # only the uploaded reference remains
    assert service.artifact_path(ids['upload']).is_file()
    assert not (service.root / 'jobs' / ids['image_job']).exists() and not (service.root / 'jobs' / ids['speech_job']).exists()
    saved = service.project(project['id'])['scenes'][0]
    assert saved['narration'] == 'Lời đọc.' and saved['visual_prompt'] == 'A river' and saved['script_approved']
    for key in ('speech_id', 'duration', 'shot_count', 'keyframe_id'):
        assert key not in saved
    assert saved['clip_ids'] == [] and saved['clip_approved'] is False and saved['revision'] > scene['revision']
    assert service.project(project['id'])['sources'][0]['artifact_id'] == ids['upload']


def test_clear_can_keep_the_spoken_lines(tmp_path):
    made, service, project, scene, ids = build(tmp_path)
    result = TestClient(made).post(f"/api/studio/projects/{project['id']}/clear-render-data", json={'keep_speech': True}).json()
    assert result['artifacts'] == 2 and result['jobs'] == 1 and result['kept_speech']
    assert service.artifact_path(ids['speech']).is_file() and service.artifact_path(ids['upload']).is_file()
    saved = service.project(project['id'])['scenes'][0]
    assert saved['speech_id'] == ids['speech'] and saved['duration'] == 4.2 and 'keyframe_id' not in saved
    assert not (service.root / 'jobs' / ids['image_job']).exists()


def test_clear_refuses_while_work_is_running(tmp_path):
    made, service, project, scene, ids = build(tmp_path)
    with service.sessions() as session:
        session.get(Job, ids['image_job']).status = 'running'
        session.commit()
    with pytest.raises(ValueError, match='đang chạy'):
        service.clear_render_data(project['id'])
    assert counts(service)[0] == 4
    with service.sessions() as session:
        session.get(Job, ids['image_job']).status = 'completed'
        session.add(ProductionRun(project_id=project['id'], idempotency_key='live', status='running', snapshot={}, consent={}))
        session.commit()
    with pytest.raises(ValueError, match='Lượt sản xuất'):
        service.clear_render_data(project['id'])


def test_clear_needs_an_explicit_yes_for_abandoned_jobs_with_unknown_gpu_state(tmp_path):
    made, service, project, scene, ids = build(tmp_path)
    with service.sessions() as session:
        job = session.get(Job, ids['image_job'])
        job.status = 'abandoned'
        job.result = {'submissions': [{'clip': 1, 'state': 'submitted'}]}
        run = session.scalars(select(ProductionRun)).first()
        run.status = 'abandoned'
        session.commit()
    before = counts(service)
    with pytest.raises(ValueError, match='chưa đối chiếu'):
        service.clear_render_data(project['id'])
    assert counts(service) == before
    response = TestClient(made).post(f"/api/studio/projects/{project['id']}/clear-render-data", json={})
    assert response.status_code in {409, 422} and 'chưa đối chiếu' in response.json()['detail']
    result = TestClient(made).post(f"/api/studio/projects/{project['id']}/clear-render-data",
                                   json={'accept_unknown_remote': True}).json()
    assert result['unknown_remote_jobs'] == 1 and result['runs'] == 1
    assert counts(service) == (1, 0, 0, 0)


def test_clear_never_overrides_a_job_that_is_still_reconciling(tmp_path):
    made, service, project, scene, ids = build(tmp_path)
    with service.sessions() as session:
        job = session.get(Job, ids['image_job'])
        job.status = 'reconciling'
        job.result = {'submissions': [{'clip': 1, 'state': 'submitted'}]}
        session.commit()
    with pytest.raises(ValueError, match='đang chạy hoặc đang đối chiếu'):
        service.clear_render_data(project['id'], accept_unknown_remote=True)
