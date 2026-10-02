"""Offline tests for rendering keyframes and Wan clips of different scenes on several Pods at once."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.models import Host
from ghm.security import SecretStore
from studio.models import Job, ProductionRun
from studio.schemas import ProductionRunInput, ProjectInput, SceneInput


def add_host(service, label, fingerprint='test'):
    with service.sessions() as session:
        host = Host(label=label, address='localhost', username='u', auth_kind='password',
                    encrypted_secret='unused', pinned_fingerprint=fingerprint)
        session.add(host)
        session.commit()
        return host.id


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'db'}", studio_root=tmp_path / 'data'),
                     SecretStore('parallel'), lambda h, s: FakeExecutor())
    service, jobs = app.state.studio, app.state.studio_jobs
    main, extra = add_host(service, 'main'), add_host(service, 'extra')
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda *a: True)
    monkeypatch.setattr('studio.production.probe', lambda p: {'duration': 60})
    project = service.create_project(ProjectInput(title='Film', topic='History', host_id=main, duration_seconds=180))
    scenes = [service.add_scene(project['id'], SceneInput(title=f'S{n}', narration=f'Read {n}', visual_prompt='A fort'))
              for n in range(3)]

    def request(parallel, key='one'):
        return ProductionRunInput(idempotency_key=key, scene_revisions={s['id']: s['revision'] for s in scenes},
                                  script_approved=True, gpu_consent=True, unsourced_consent=True,
                                  parallel_host_ids=parallel)
    return TestClient(app), service, jobs, project, scenes, main, extra, request


def complete(service, jobs, job_id, status='completed'):
    job = service.require(Job, job_id)
    suffix = {'speech': 'wav', 'keyframe': 'png'}.get(job.kind, 'mp4')
    path = service.job_directory(job.id) / f'output.{suffix}'
    path.write_bytes(b'validated fixture')
    artifact = service.artifact(path, job.project_id, path.name, job.id)['id']
    output = ({'speech_id': artifact, 'duration': 60, 'shot_count': 12} if job.kind == 'speech' else
              {'keyframe_id': artifact, 'keyframe_approved': False} if job.kind == 'keyframe' else
              {'clip_ids': [artifact], 'clip_approved': False} if job.kind == 'clip' else {'artifact_ids': [artifact]})
    if job.kind != 'export':
        jobs.scene_result(job.scene_id, job=job, **output)
    else:
        jobs.patch(job.id, result=output)
    jobs.patch(job.id, status=status)


def active(service, run_id):
    run = service.require(ProductionRun, run_id)
    return [service.require(Job, id) for id in run.checkpoint.get('active_job_ids', [])]


def through_keyframes(service, jobs, run_id):
    for _ in range(12):
        jobs.runs.tick()
        batch = active(service, run_id)
        if batch[0].kind not in {'speech', 'keyframe'}:
            return
        for job in batch:
            complete(service, jobs, job.id)
    raise AssertionError('keyframes never finished')


def test_clips_fan_out_across_pods_and_export_waits(setup):
    client, service, jobs, project, scenes, main, extra, request = setup
    response = client.post(f"/api/studio/projects/{project['id']}/production-runs", json=request([extra]).model_dump())
    assert response.status_code == 201, response.text
    run = response.json()
    assert run['parallel_host_ids'] == [extra]
    through_keyframes(service, jobs, run['id'])
    first = active(service, run['id'])
    assert [j.kind for j in first] == ['clip', 'clip']
    assert {j.host_id for j in first} == {main, extra}
    assert jobs.runs.get(run['id'])['active_scene_ids'] == [j.scene_id for j in first]
    # The Pod that finishes first takes the third scene; the other keeps rendering.
    complete(service, jobs, first[1].id)
    jobs.runs.tick()
    second = active(service, run['id'])
    assert next(j.id for j in second) == first[0].id
    assert second[1].scene_id == scenes[2]['id'] and second[1].host_id == first[1].host_id
    complete(service, jobs, first[0].id)
    jobs.runs.tick()
    assert [j.kind for j in active(service, run['id'])] == ['clip']  # no export while a clip renders
    complete(service, jobs, second[1].id)
    jobs.runs.tick()
    (export,) = active(service, run['id'])
    assert export.kind == 'export' and export.host_id is None
    complete(service, jobs, export.id)
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['status'] == 'completed'


def test_single_pod_behavior_unchanged(setup):
    _client, service, jobs, project, scenes, main, _extra, request = setup
    run = jobs.runs.create(project['id'], request([]))
    through_keyframes(service, jobs, run['id'])
    for scene in scenes:
        (job,) = active(service, run['id'])
        assert job.kind == 'clip' and job.host_id == main and job.scene_id == scene['id']
        complete(service, jobs, job.id)
        jobs.runs.tick()


def test_failure_stops_new_dispatch_but_lets_other_pod_finish(setup):
    _client, service, jobs, project, _scenes, _main, extra, request = setup
    run = jobs.runs.create(project['id'], request([extra]))
    through_keyframes(service, jobs, run['id'])
    first, second = active(service, run['id'])
    jobs.patch(first.id, status='failed', error='OOM')
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['status'] == 'running'
    assert len(active(service, run['id'])) == 2  # third scene not dispatched
    complete(service, jobs, second.id)
    jobs.runs.tick()
    state = jobs.runs.get(run['id'])
    assert state['status'] == 'failed' and state['error'] == 'OOM'
    assert state['active_job_ids'] == [first.id]
    jobs.runs.action(run['id'], 'resume')
    assert service.require(Job, first.id).status == 'queued'


def test_pause_waits_for_every_pod(setup):
    _client, service, jobs, project, _scenes, _main, extra, request = setup
    run = jobs.runs.create(project['id'], request([extra]))
    through_keyframes(service, jobs, run['id'])
    first, second = active(service, run['id'])
    jobs.patch(first.id, status='running')
    jobs.runs.action(run['id'], 'pause')
    assert service.require(Job, second.id).status == 'paused'
    assert service.require(Job, first.id).result['pause_requested']
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['status'] == 'pause_requested'
    jobs.patch(first.id, status='paused')
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['status'] == 'paused'
    jobs.runs.action(run['id'], 'resume')
    assert {service.require(Job, j).status for j in (first.id, second.id)} == {'queued'}


def test_local_ken_burns_scene_runs_beside_wan(setup):
    _client, service, jobs, project, scenes, main, _extra, request = setup
    from studio.models import Scene
    with service.sessions() as session:
        row = session.get(Scene, scenes[0]['id'])
        row.data = {**row.data, 'motion': 'kenburns'}
        session.commit()
    scene_revisions = {s['id']: s['revision'] for s in service.project(project['id'])['scenes']}
    run = jobs.runs.create(project['id'], request([]).model_copy(update={'scene_revisions': scene_revisions}))
    through_keyframes(service, jobs, run['id'])
    clips = active(service, run['id'])
    assert [j.host_id for j in clips] == [None, main]


def test_parallel_host_validation(setup, monkeypatch):
    _client, service, jobs, project, _scenes, main, extra, request = setup
    with pytest.raises(ValueError, match='Pod chính'):
        jobs.runs.create(project['id'], request([main]))
    unpinned = add_host(service, 'new', fingerprint=None)
    with pytest.raises(ValueError, match='fingerprint'):
        jobs.runs.create(project['id'], request([unpinned]))
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda host, needed: host != extra)
    with pytest.raises(ValueError, match='Wan chưa'):
        jobs.runs.create(project['id'], request([extra]))
    with pytest.raises(KeyError):
        jobs.runs.create(project['id'], request(['missing']))


def test_deleted_parallel_pod_moves_work_to_main(setup):
    _client, service, jobs, project, _scenes, main, extra, request = setup
    run = jobs.runs.create(project['id'], request([extra]))
    through_keyframes(service, jobs, run['id'])
    on_extra = next(j for j in active(service, run['id']) if j.host_id == extra)
    jobs.patch(on_extra.id, status='interrupted')
    on_main = next(j for j in active(service, run['id']) if j.host_id == main)
    complete(service, jobs, on_main.id)
    jobs.runs.tick()
    assert jobs.runs.get(run['id'])['status'] == 'reconciling'
    jobs.hosts.delete_host(extra)  # releases the job's host reference, like the UI delete
    state = jobs.runs.action(run['id'], 'resume')
    assert state['parallel_host_ids'] == []
    moved = service.require(Job, on_extra.id)
    assert moved.host_id == main and moved.status == 'queued'


def test_worker_runs_one_job_per_host_concurrently(setup, monkeypatch):
    _client, service, jobs, project, _scenes, main, extra, _request = setup
    started, release = [], asyncio.Event()

    async def fake_execute(job):
        started.append(job.id)
        await release.wait()
    monkeypatch.setattr(jobs, 'execute', fake_execute)
    ids = []
    with service.sessions() as session:
        for host in (main, main, extra):
            row = Job(project_id=project['id'], host_id=host, kind='clip', input_hash=f'{host}-{len(ids)}', snapshot={})
            session.add(row)
            session.commit()
            ids.append(row.id)

    async def scenario():
        assert jobs.dispatch() == 2
        await asyncio.sleep(0)
        assert set(started) == {ids[0], ids[2]}
        assert jobs.dispatch() == 0  # second main-Pod job waits for its lane
        release.set()
        await asyncio.gather(*jobs.active.values())
        assert jobs.dispatch() == 1
        await asyncio.gather(*jobs.active.values())
    asyncio.run(scenario())
    assert {service.require(Job, id).status for id in ids} == {'completed'}


def test_keyframes_fan_out_across_pods_and_wait_for_images_before_clips(setup):
    _client, service, jobs, project, scenes, main, extra, request = setup
    run = jobs.runs.create(project['id'], request([extra]))
    for _ in range(3):  # speech stays one job at a time on the main Pod
        jobs.runs.tick()
        (speech,) = active(service, run['id'])
        assert speech.kind == 'speech' and speech.host_id == main
        complete(service, jobs, speech.id)
    jobs.runs.tick()
    first = active(service, run['id'])
    assert [j.kind for j in first] == ['keyframe', 'keyframe']
    assert {j.host_id for j in first} == {main, extra}
    assert [j.scene_id for j in first] == [scenes[0]['id'], scenes[1]['id']]
    complete(service, jobs, first[1].id)
    jobs.runs.tick()
    second = active(service, run['id'])
    assert second[1].scene_id == scenes[2]['id'] and second[1].host_id == first[1].host_id
    complete(service, jobs, first[0].id)
    jobs.runs.tick()
    assert [j.kind for j in active(service, run['id'])] == ['keyframe']  # clips wait for every image
    complete(service, jobs, second[1].id)
    jobs.runs.tick()
    assert {j.kind for j in active(service, run['id'])} == {'clip'}


def test_an_extra_pod_without_image_models_is_not_given_keyframes(setup, monkeypatch):
    _client, service, jobs, project, scenes, main, extra, request = setup
    monkeypatch.setattr(jobs.installations, 'component_proven',
                        lambda host_id, names: host_id == main or names == ['wan_i2v'])
    run = jobs.runs.create(project['id'], request([extra]))
    for _ in range(3):
        jobs.runs.tick()
        (speech,) = active(service, run['id'])
        complete(service, jobs, speech.id)
    for scene in scenes:
        jobs.runs.tick()
        (job,) = active(service, run['id'])
        assert job.kind == 'keyframe' and job.host_id == main and job.scene_id == scene['id']
        complete(service, jobs, job.id)


def test_lost_contact_leaves_the_technical_reason_in_the_job_log(setup):
    from studio.jobs import ReconcileRequired
    _client, service, jobs, project, _scenes, _main, _extra, request = setup
    run = jobs.runs.create(project['id'], request([]))
    jobs.runs.tick()
    (job,) = active(service, run['id'])
    try:
        try:
            raise ConnectionResetError('proxy closed the session')
        except ConnectionResetError as cause:
            raise ReconcileRequired('Kết nối gián đoạn') from cause
    except ReconcileRequired as exc:
        detail = jobs.technical_detail(exc)
    assert detail.startswith('Chi tiết kỹ thuật: ReconcileRequired: Kết nối gián đoạn') and 'ConnectionResetError: proxy closed the session' in detail
    jobs.event(job.id, detail, level='warning')
    from studio.models import JobEvent
    with service.sessions() as session:
        assert any(e.message == detail for e in session.query(JobEvent).filter_by(job_id=job.id))


def pod_lanes(service, jobs, main, gpus):
    """The main host plus one more ComfyUI process per entry of ``gpus`` (the GPU it shares), all on one Pod."""
    import json
    ids = [main, *(add_host(service, f'lane{n}') for n in range(len(gpus)))]
    for i, hid in enumerate(ids):
        gpu = 0 if i == 0 else gpus[i-1]
        jobs.hosts.save_setting(f'host_lane:{hid}', json.dumps({'pod_id': 'pod', 'first': main, 'index': i, 'gpu': gpu,
                                                               'instance': 0 if gpu != 0 or i == 0 else i}))
    jobs.hosts.save_setting('runpod_pod_lanes:pod', json.dumps(ids))
    return ids


def test_images_use_every_process_of_the_pod_but_a_clip_takes_one_per_gpu(setup):
    _client, service, jobs, project, scenes, main, _extra, request = setup
    ids = pod_lanes(service, jobs, main, [0, 0])         # three processes sharing GPU 0, nothing ticked as parallel
    run = jobs.runs.create(project['id'], request([]))
    for _ in range(12):
        jobs.runs.tick()
        batch = active(service, run['id'])
        if batch[0].kind != 'speech':
            break
        for job in batch:
            complete(service, jobs, job.id)
    batch = active(service, run['id'])
    assert [j.kind for j in batch] == ['keyframe'] * 3 and {j.host_id for j in batch} == set(ids)
    through_keyframes(service, jobs, run['id'])
    (clip,) = active(service, run['id'])                  # one GPU: one Wan clip at a time, on the main process
    assert clip.kind == 'clip' and clip.host_id == main


def test_clips_still_use_each_physical_gpu_once(setup):
    _client, service, jobs, project, scenes, main, _extra, request = setup
    ids = pod_lanes(service, jobs, main, [0, 1])         # lane1 shares GPU 0, lane2 is GPU 1
    run = jobs.runs.create(project['id'], request([ids[1], ids[2]]))   # clips still need the extra hosts to be ticked
    through_keyframes(service, jobs, run['id'])
    clips = active(service, run['id'])
    assert [j.kind for j in clips] == ['clip', 'clip'] and {j.host_id for j in clips} == {ids[0], ids[2]}
