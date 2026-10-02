"""Synthetic driver/Comfy responses only: these tests never rent or render a GPU."""
import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import test_wan_performance as wan_tests

from studio.backend import ReconcileRequired, RemoteBackend
from studio.benchmark import compare_cost, execute_profile, report
from studio.cost_policy import (
    capacity_graph_hash,
    fingerprint,
    measured_capacity,
    remote_settled,
    result_settled,
)
from studio.generation import clip_config
from studio.gpu_telemetry import TelemetryPool, Window
from studio.jobs import PauseAtBoundary
from studio.models import Job, ProductionRun, Scene
from studio.packs import load_graph
from studio.service import canonical_hash

GIB = 1024**3
RUNTIME = {'comfy_version': 'pinned', 'torch_version': '2.8', 'cuda_version': '12.8',
           'attention_backend': 'default', 'memory_policy': 'default', 'compute_flags': []}
CONFIG = {'version': 2, 'width': 1280, 'height': 720, 'frames': 81, 'fps': 16, 'steps': 4, 'shot_seconds': 81/16}
local = wan_tests.local
ComfyMock = wan_tests.ComfyMock


def records(peak=36, gpu='GPU-a', lanes=1, hot=10):
    telemetry = {'gpu_uuid': gpu, 'gpu_name': 'Test96', 'gpu_total_bytes': 96*GIB,
                 'process_peak_vram_bytes': peak*GIB, 'process_memory_samples': 20}
    identity = fingerprint(CONFIG, RUNTIME, telemetry)
    return {f'shot-{i}': {'state': 'downloaded', 'cold_candidate': i < lanes, 'prompt_id': str(i),
        'capacity_fingerprint': identity, 'runtime': RUNTIME, 'telemetry': deepcopy(telemetry),
        'attempts': 1, 'config': CONFIG, 'timing': {'total_seconds': 60}} for i in range(hot+lanes)}


def benchmark_result(*, lanes=1, wall=700, throughput=60, gpu='GPU-a', rate=2, residency='job'):
    shots = records(gpu=gpu, lanes=lanes)
    return {'benchmark_input_hash': 'same inputs', 'shots': shots, 'warm_samples': 10,
        'measured_stage_wall_seconds': wall, 'clips_per_gpu_hour': throughput,
        'measured_processing_usd': wall/3600*rate,
        'config': CONFIG, 'workflow_hash': canonical_hash(load_graph('wan_i2v')),
        'policy': {'model_residency': residency, 'wan_concurrency': lanes}}


def test_capacity_requires_measurement_and_percentage_reserves():
    assert measured_capacity({'shots': records(36)})['fits_two']
    assert not measured_capacity({'shots': records(40)})['fits_two']  # 88 > 86.4 GiB
    assert not measured_capacity({'shots': records(48)})['fits_two']
    missing = records()
    missing['shot-5']['telemetry']['process_peak_vram_bytes'] = None
    with pytest.raises(ValueError, match='Thiếu số đo'):
        measured_capacity({'shots': missing})
    changed = records()
    changed['shot-5']['capacity_fingerprint'] = 'different runtime'
    with pytest.raises(ValueError, match='thay đổi'):
        measured_capacity({'shots': changed})


def test_capacity_identity_changes_for_hardware_workflow_runtime_and_resolution():
    telemetry = records()['shot-0']['telemetry']
    identity = fingerprint(CONFIG, RUNTIME, telemetry)
    assert identity != fingerprint({**CONFIG, 'frames': 65}, RUNTIME, telemetry)
    assert identity != fingerprint(CONFIG, {**RUNTIME, 'attention_backend': 'sage'}, telemetry)
    assert identity != fingerprint(CONFIG, RUNTIME, {**telemetry, 'gpu_uuid': 'GPU-b'})
    assert identity != fingerprint(CONFIG, RUNTIME, telemetry, 'different graph')
    assert fingerprint(CONFIG, {**RUNTIME, 'cuda_version': None}, telemetry) is None
    a = {'1': {'class_type': 'KSampler', 'inputs': {'seed': 1, 'steps': 4}},
         '2': {'class_type': 'TextEncode', 'inputs': {'text': 'old'}}}
    b = deepcopy(a)
    b['1']['inputs']['seed'], b['2']['inputs']['text'] = 99, 'new'
    assert capacity_graph_hash(a) == capacity_graph_hash(b)
    b['1']['inputs']['steps'] = 8
    assert capacity_graph_hash(a) != capacity_graph_hash(b)


def test_throughput_counts_physical_elapsed_not_overlapping_clip_seconds():
    submissions = list(records(lanes=2).values())
    for i, item in enumerate(submissions):
        item.update(benchmark_started_at=100+i//2*60, benchmark_finished_at=160+i//2*60)
    result = report(submissions, [326/14]*14, 2)
    assert result['warm_samples'] == 10
    assert result['hot_wall_total_seconds'] == 600
    assert result['hot_stage_wall_seconds'] == 300
    assert result['clips_per_gpu_hour'] == 120
    assert result['scenarios'][1]['gpu_hours_estimate'] == pytest.approx(70/120)


@pytest.mark.parametrize('lanes,wall,throughput,gpu,rate,eligible', [
    (2, 500, 75, 'GPU-a', 2, True), (2, 500, 70, 'GPU-a', 2, False),
    (2, 750, 90, 'GPU-a', 2, False), (1, 700, 60, 'GPU-b', 1.5, True),
    (1, 701, 60, 'GPU-b', .8, False), (1, 700, 60, 'GPU-b', 1.8, False)])
def test_cost_acceptance_preserves_speed_and_twenty_percent_rule(lanes, wall, throughput, gpu, rate, eligible):
    rows = {'a': SimpleNamespace(kind='benchmark', status='completed', result={'benchmark_report': benchmark_result()}),
            'b': SimpleNamespace(kind='benchmark', status='completed', result={'benchmark_report': benchmark_result(
                lanes=lanes, wall=wall, throughput=throughput, gpu=gpu, rate=rate)})}
    jobs = SimpleNamespace(service=SimpleNamespace(require=lambda cls, id: rows[id]))
    result = compare_cost(jobs, 'a', 'b')
    assert result['candidate_eligible'] is eligible and result['auto_enable'] is False


def test_window_uses_pid_and_gpu_uuid_not_total_allocator_and_does_not_invent_unknown():
    window = Window(11, 'prompt-a')
    sample = {'timestamp': 1, 'gpus': [{'index': 0, 'uuid': 'GPU-a', 'name': 'Test96',
              'total_mib': 96*1024, 'used_mib': 80*1024, 'utilization': None}],
              'processes': [{'uuid': 'GPU-a', 'pid': 11, 'used_mib': 36*1024},
                            {'uuid': 'GPU-a', 'pid': 22, 'used_mib': 40*1024}], 'host_ram_used_bytes': 100*GIB}
    window.add(sample, 0, ['prompt-a', 'prompt-b'])
    assert window.summary['process_peak_vram_bytes'] == 36*GIB
    assert window.summary['gpu_peak_used_bytes'] == 80*GIB
    assert window.summary['gpu_utilization_mean'] is None and window.summary['model_load_seconds'] is None
    assert window.summary['active_prompt_ids'] == ['prompt-a', 'prompt-b']
    window.add({**sample, 'gpus': [{**sample['gpus'][0], 'uuid': 'GPU-b'}]}, 0, [])
    assert window.summary['identity_changed']


@pytest.mark.asyncio
async def test_two_lanes_share_one_sampler_and_close_on_disconnect():
    calls, started = [], asyncio.Event()
    hosts = SimpleNamespace(setting=lambda k: json.dumps({'pod_id': 'pod', 'index': 0, 'gpu': 0}))
    @asynccontextmanager
    async def connection(host):
        calls.append(host)
        yield object(), None
    pool = TelemetryPool(SimpleNamespace(hosts=hosts, connection=connection))
    async def snapshot(executor):
        started.set()
        return {'gpus': [{'index': 0, 'uuid': 'GPU-a', 'total_mib': 96*1024, 'used_mib': 1, 'utilization': 95}], 'processes': []}
    pool.snapshot = snapshot
    a = await pool.watch(SimpleNamespace(host_id='a'), '11:100', 'p1')
    b = await pool.watch(SimpleNamespace(host_id='b'), '22:101', 'p2')
    await started.wait()
    assert len(pool.monitors) == len(calls) == 1
    assert (await pool.unwatch(a))['active_prompt_ids'] == ['p1', 'p2']
    assert len(pool.monitors) == 1
    await pool.unwatch(b)
    assert pool.monitors == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['complete', 'pause', 'unknown'])
async def test_stage_residency_reuses_connection_but_never_frees_ambiguous_prompt(boundary):
    calls, opened, closed = [], [], []
    backend = RemoteBackend(None, None)
    ready = {'yes': True}
    backend.jobs = SimpleNamespace(closing=False, cost=SimpleNamespace(
        config=lambda j: {'model_residency': 'stage', 'wan_concurrency': 1}, next_ready=lambda j: ready['yes']))
    def response(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/queue':
            return httpx.Response(200, json={'queue_running': [], 'queue_pending': []})
        return httpx.Response(200, json={'devices': [{'torch_vram_total': 0}]})
    async with httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(response)) as client:
        @asynccontextmanager
        async def connection(host):
            opened.append(host)
            try:
                yield None, client
            finally:
                closed.append(host)
        backend.connection = connection
        one = SimpleNamespace(id=str(uuid4()), host_id='a', kind='clip')
        two = SimpleNamespace(id=str(uuid4()), host_id='a', kind='clip')
        async with backend.generation_session(one), backend.generation_connection(one) as (_, state):
            state.update(remote_settled=True, shots=5, process_generation='11:100')
        assert ('POST', '/free') not in calls and len(backend._resident) == 1
        ready['yes'] = False
        try:
            async with backend.generation_session(two), backend.generation_connection(two) as (_, state):
                assert state['shots'] == 5 and state['process_generation'] == '11:100'
                state['remote_settled'] = boundary != 'unknown'
                if boundary == 'pause':
                    raise PauseAtBoundary()
                if boundary == 'unknown':
                    raise ReconcileRequired('disconnect after intent')
        except (PauseAtBoundary, ReconcileRequired):
            pass
        assert opened == closed == ['a']
        assert calls.count(('POST', '/free')) == (0 if boundary == 'unknown' else 1)
        assert backend._resident == {}
    await backend.close()


@pytest.mark.asyncio
async def test_stage_idle_timer_rechecks_queue_before_free():
    backend = RemoteBackend(None, None)
    free = []
    async def cleanup(client, log=None):
        free.append(client)
    backend.release_idle_gpu = cleanup
    stack = SimpleNamespace(aclose=lambda: asyncio.sleep(0))
    backend._resident['a'] = {'client': 'comfy', 'stack': stack}
    # The real timer has a hard-coded 60-second bound; execute just its release
    # path here, with queue safety covered by transport regressions.
    await backend.release_resident('a')
    assert free == ['comfy'] and not backend._resident


@pytest.mark.asyncio
async def test_profile_benchmark_preserves_scenes_and_resumes_without_duplicate_post(local, monkeypatch):
    _, service, jobs, old, _ = local
    original = deepcopy(service.require(Scene, old.scene_id).data)
    with service.sessions() as session:
        row = session.get(Job, old.id)
        scene = row.snapshot['scene']
        row.kind, row.status = 'benchmark', 'running'
        row.snapshot = {**row.snapshot, 'generation_version': 2, 'benchmark_scenes': [scene],
            'benchmark_hosts': [None], 'benchmark': {'warm_shots': 10, 'max_wall_seconds': 300,
            'model_residency': 'job', 'wan_concurrency': 1}}
        session.commit()
    monkeypatch.setattr('studio.benchmark.probe', lambda p: {'duration': 12})
    mock = ComfyMock(local, monkeypatch)
    await execute_profile(jobs, service.require(Job, old.id))
    saved = service.require(Job, old.id)
    assert saved.result['benchmark_report']['warm_samples'] == 10
    assert mock.calls.count(('POST', '/prompt')) == 11
    assert len(saved.result['artifact_ids']) == 12
    from studio.benchmark import execute
    await execute(jobs, saved)
    assert mock.calls.count(('POST', '/prompt')) == 11
    assert service.require(Scene, old.scene_id).data == original


@pytest.fixture
def lanes(local, monkeypatch):
    from ghm.models import Host, PreflightSnapshot
    from ghm.schemas import HostOptions
    client, service, jobs, old, image = local
    ids = []
    with service.sessions() as session:
        for i in range(2):
            h = Host(label=f'Lane {i}', address='test', username='u', auth_kind='password', encrypted_secret='unused')
            session.add(h)
            session.flush()
            ids.append(h.id)
            session.add(PreflightSnapshot(host_id=h.id, status='pass', payload=json.dumps({
                'status': 'pass', 'checks': [], 'gpu': {'name': 'Test96', 'vram_gb': 96, 'driver_version': '580'}})))
        session.commit()
    for i, h in enumerate(ids):
        jobs.hosts.save_setting('host_lane:' + h, json.dumps({'pod_id': 'p', 'index': i, 'gpu': 0, 'first': ids[0]}))
        jobs.hosts.save_options(h, HostOptions(remote_port=8188+i, gpu_index=0))
    jobs.hosts.save_setting('runpod_pod_lanes:p', json.dumps(ids))
    monkeypatch.setattr(jobs.installations, 'component_proven', lambda *args: True)
    return client, service, jobs, old, image, ids


def save_profile_reports(service, old, host_id):
    baseline, candidate = benchmark_result(), benchmark_result(lanes=2, wall=500, throughput=75)
    config = clip_config(old.snapshot['project'], old.snapshot['scene'])
    for result in (baseline, candidate):
        result['config'] = config
        for s in result['shots'].values():
            s['config'] = config
            s['capacity_fingerprint'] = fingerprint(config, s['runtime'], s['telemetry'])
    with service.sessions() as session:
        rows = [Job(project_id=old.project_id, host_id=host_id, kind='benchmark', status='completed',
                    input_hash=str(i), snapshot={}, result={'benchmark_report': result})
                for i, result in enumerate((baseline, candidate))]
        session.add_all(rows)
        session.commit()
    return rows[0].id, rows[1].id


def test_profile_requires_quality_review_boundary_and_retains_old_artifacts(lanes):
    client, service, jobs, old, _image, ids = lanes
    baseline, candidate = save_profile_reports(service, old, ids[0])
    url = f'/api/studio/hosts/{ids[0]}/cost-policy'
    value = {'wan_concurrency': 2, 'model_residency': 'job', 'baseline_id': baseline, 'candidate_id': candidate}
    assert client.post(url, json=value).status_code == 409
    path = service.job_directory(old.id) / 'clip-0.mp4'
    path.write_bytes(b'preserved paid render')
    artifact = service.artifact(path, old.project_id, path.name, old.id)
    jobs.checkpoint(old.id, 'clip-0', {'prompt_id': 'old-paid', 'state': 'downloaded', 'artifact_id': artifact['id']})
    result = client.post(url, json={**value, 'quality_review_passed': True})
    assert result.status_code == 200
    assert jobs.cost.state(ids[1])['config']['wan_concurrency'] == 2  # shared physical GPU
    assert jobs.cost.compatible_job(ids[1], old.snapshot['project'], old.snapshot['scene'])
    assert not jobs.cost.compatible_job(ids[1], old.snapshot['project'], {**old.snapshot['scene'], 'clip_steps': 8})
    assert service.artifact_path(artifact['id']).read_bytes() == b'preserved paid render'
    assert service.require(Job, old.id).result['submissions']['clip-0']['prompt_id'] == 'old-paid'
    with service.sessions() as session:
        session.add(Job(host_id=ids[1], kind='clip', status='reconciling', snapshot={}, input_hash='pending',
                        result={'submissions': {'clip-1': {'state': 'submitted', 'prompt_id': 'uncertain'}}}))
        session.commit()
    assert client.post(url, json={'wan_concurrency': 1}).status_code == 409


@pytest.mark.asyncio
async def test_dispatch_two_wan_requires_profile_and_blocks_gpu_after_failure(lanes, monkeypatch):
    _, service, jobs, old, _image, hosts = lanes
    baseline, candidate = save_profile_reports(service, old, hosts[0])
    jobs.cost.apply(hosts[0], {'wan_concurrency': 2, 'baseline_id': baseline, 'candidate_id': candidate, 'quality_review_passed': True})
    added = []
    with service.sessions() as session:
        for host in hosts:
            row = Job(project_id=old.project_id, host_id=host, kind='clip', status='queued', snapshot=old.snapshot, input_hash=host)
            session.add(row)
            session.flush()
            added.append(row.id)
        session.commit()
    release = asyncio.Event()
    async def execute(job):
        await release.wait()
    monkeypatch.setattr(jobs, 'execute', execute)
    assert jobs.dispatch() == 2
    await asyncio.sleep(0)
    assert len(jobs.active) == 2
    jobs.cost.fault(hosts[0], added[0])
    release.set()
    await asyncio.gather(*jobs.active.values())
    with service.sessions() as session:
        extra = Job(host_id=hosts[1], kind='clip', status='queued', snapshot=old.snapshot, input_hash='extra')
        session.add(extra)
        session.commit()
    assert jobs.dispatch() == 0
    assert jobs.cost.state(hosts[1])['fault']['job_id'] == added[0]


def test_two_lanes_cannot_reserve_same_comfy_pid_or_overwrite_checkpoint(lanes):
    _, service, jobs, _old, _image, hosts = lanes
    with service.sessions() as session:
        rows = [Job(host_id=h, kind='clip', status='running', snapshot={}, input_hash=h) for h in hosts]
        session.add_all(rows)
        session.commit()
    intent = {'state': 'submitting', 'process_generation': '111:100', 'cost_policy': {'wan_concurrency': 2}, 'prompt_id': 'p1'}
    jobs.checkpoint(rows[0].id, 'clip-0', intent)
    with pytest.raises(ValueError, match='cùng tiến trình'):
        jobs.checkpoint(rows[1].id, 'clip-0', {**intent, 'prompt_id': 'p2'})
    assert not service.require(Job, rows[1].id).result.get('submissions')
    jobs.checkpoint(rows[0].id, 'clip-0', {'remote_terminal_prompt_id': 'p1', 'remote_terminal_state': 'error'})
    jobs.checkpoint(rows[1].id, 'clip-0', {**intent, 'prompt_id': 'p2'})
    assert service.require(Job, rows[1].id).result['submissions']['clip-0']['prompt_id'] == 'p2'


@pytest.mark.asyncio
async def test_gpu_fault_clear_reads_every_prompt_on_its_original_lane(lanes):
    _, service, jobs, _old, _image, hosts = lanes
    with service.sessions() as session:
        row = Job(host_id=hosts[0], kind='benchmark', status='failed', snapshot={'benchmark_hosts': hosts}, input_hash='failed',
            result={'submissions': {'a': {'state': 'submitted', 'prompt_id': 'pa', 'host_id': hosts[0]},
                                   'b': {'state': 'submitted', 'prompt_id': 'pb', 'host_id': hosts[1]}}})
        session.add(row)
        session.commit()
    jobs.cost.fault(hosts[0], row.id)
    calls = []
    missing = {'yes': True}
    @asynccontextmanager
    async def connection(host):
        def response(request):
            calls.append((host, request.method, request.url.path))
            if request.url.path == '/queue':
                return httpx.Response(200, json={'queue_running': [], 'queue_pending': []})
            pid = request.url.path.rsplit('/', 1)[-1]
            expected = 'pa' if host == hosts[0] else 'pb'
            assert pid == expected
            return httpx.Response(200, json={} if missing['yes'] and pid == 'pb' else {pid: {'status': {'status_str': 'error'}}})
        async with httpx.AsyncClient(base_url='http://comfy', transport=httpx.MockTransport(response)) as client:
            yield None, client
    jobs.backend.connection = connection
    with pytest.raises(ValueError, match='history chưa rõ'):
        await jobs.cost.clear_fault(hosts[0])
    assert jobs.cost.state(hosts[0])['fault']
    missing['yes'] = False
    await jobs.cost.clear_fault(hosts[0])
    assert jobs.cost.state(hosts[0])['fault'] is None
    assert service.require(Job, row.id).status == 'interrupted'
    assert jobs.resume(row.id)['status'] == 'queued'
    assert all(method == 'GET' for _, method, _ in calls)


@pytest.mark.asyncio
async def test_dual_benchmark_settles_peer_and_resumes_original_prompt_ids(lanes, monkeypatch):
    _, service, jobs, old, _image, hosts = lanes
    posts, reconciled = [], []
    disconnected = {'once': True}
    with service.sessions() as session:
        row = session.get(Job, old.id)
        row.kind, row.status, row.host_id = 'benchmark', 'running', hosts[0]
        row.snapshot = {**row.snapshot, 'benchmark_scenes': [row.snapshot['scene']],
            'benchmark_hosts': hosts, 'benchmark': {'warm_shots': 10, 'max_wall_seconds': 300,
                'wan_concurrency': 2, 'model_residency': 'job'}}
        session.commit()
    monkeypatch.setattr('studio.benchmark.probe', lambda p: {'duration': 12})
    class Dual:
        @asynccontextmanager
        async def connection(self, host):
            yield host, None
        async def process_generation(self, executor, host):
            return host + ':100'
        @asynccontextmanager
        async def generation_session(self, job):
            yield
        async def generate(self, job, name, prompt, images, seed, quality, log, checkpoint, stage, steps, shot):
            prior = job.result.get('submissions', {}).get(stage)
            if prior:
                assert prior['host_id'] == job.host_id
                reconciled.append(prior['prompt_id'])
            else:
                checkpoint(stage, {'state': 'submitting', 'prompt_id': stage, 'host_id': job.host_id})
                assert service.require(Job, job.id).result['submissions'][stage]['prompt_id'] == stage
                posts.append((job.host_id, stage))
            await asyncio.sleep(.001)
            if stage == 'benchmark-hot-2' and disconnected['once']:
                disconnected['once'] = False
                raise ReconcileRequired('Response lost after POST')
            path = service.job_directory(job.id) / (stage + '.mp4')
            path.write_bytes(b'synthetic video')
            sample = records()['shot-0']
            checkpoint(stage, {**sample, 'prompt_id': stage, 'host_id': job.host_id, 'cold_candidate': 'cold' in stage})
            return path
    jobs.backend = Dual()
    with pytest.raises(ReconcileRequired):
        await execute_profile(jobs, service.require(Job, old.id))
    assert not any(stage == 'benchmark-hot-4' for _, stage in posts)
    saved = service.require(Job, old.id)
    completed = {s['artifact_id'] for s in saved.result['submissions'].values() if s.get('artifact_id')}
    checksums = {id: service.artifact_path(id).read_bytes() for id in completed}
    await execute_profile(jobs, saved)
    assert 'benchmark-hot-2' in reconciled
    assert len(posts) == len(set(posts)) == 12  # 10 hot + 2 startup, each POST once
    assert all(service.artifact_path(id).read_bytes() == checksums[id] for id in completed)
    assert service.require(Job, old.id).result['benchmark_report']['warm_samples'] == 10


@pytest.mark.asyncio
async def test_uncertain_intent_before_history_read_never_unloads_retained_weights(local, monkeypatch):
    _, _service, jobs, old, _image = local
    mock = ComfyMock(local, monkeypatch)
    prior = {'state': 'submitted', 'prompt_id': 'uncertain'}
    jobs.checkpoint(old.id, 'clip-0', prior)
    calls = []
    def disconnected(request):
        calls.append((request.method, request.url.path))
        raise httpx.ReadError('disconnect', request=request)
    async with httpx.AsyncClient(base_url='http://comfy', transport=httpx.MockTransport(disconnected)) as client:
        @asynccontextmanager
        async def connection(host):
            yield None, client
        mock.backend.connection = connection
        with pytest.raises(httpx.ReadError):
            async with mock.backend.generation_session(old), mock.backend.generation_connection(old) as (_, state):
                state['remote_settled'] = True  # previous scene had settled
                await mock.generate(0)
    assert all(method == 'GET' for method, _ in calls)


@pytest.mark.parametrize('proof', [
    {'cancel_confirmed_prompt_id': 'old'},
    {'remote_terminal_prompt_id': 'old', 'remote_terminal_state': 'error'},
])
def test_terminal_evidence_only_settles_its_original_prompt(proof):
    old = {'state': 'submitted', 'prompt_id': 'old', **proof}
    assert remote_settled(old) and result_settled({'submissions': {'clip-0': old}})
    assert not remote_settled({**old, 'prompt_id': 'replacement'})
    assert not result_settled({'speech_pending': True, 'submissions': {'clip-0': old}})


def test_pause_releases_stage_residency_even_with_a_queued_scene(lanes):
    _, service, jobs, old, _, hosts = lanes
    with service.sessions() as session:
        run = ProductionRun(project_id=old.project_id, idempotency_key='pause-residency',
            status='running', stage='clip', snapshot=old.snapshot['project'], consent={}, checkpoint={})
        session.add(run)
        session.flush()
        row = session.get(Job, old.id)
        row.host_id = hosts[0]
        row.snapshot = {**row.snapshot, 'production_run_id': run.id}
        session.add(Job(project_id=old.project_id, host_id=hosts[0], kind='clip',
            status='queued', input_hash='ready', snapshot={}))
        session.commit()
    assert jobs.cost.next_ready(service.require(Job, old.id))
    with service.sessions() as session:
        session.get(ProductionRun, run.id).status = 'pause_requested'
        session.commit()
    assert not jobs.cost.next_ready(service.require(Job, old.id))


def test_benchmark_accepts_paused_film_snapshot_and_rejects_uncertain_prompt(lanes):
    client, service, jobs, old, image, hosts = lanes
    with service.sessions() as session:
        row = session.get(Job, old.id)
        row.host_id = hosts[0]
        project = deepcopy(row.snapshot['project'])
        project['host_id'] = hosts[0]
        scene = project['scenes'][0]
        approved = {'keyframe_id': scene['keyframe_id'], 'keyframe_approved': True}
        scene['keyframe_id'], scene['keyframe_approved'] = None, False
        run = ProductionRun(project_id=old.project_id, idempotency_key='paused-benchmark',
            status='paused', stage='clip', snapshot=project, consent={},
            checkpoint={'media': {scene['id']: approved}})
        session.add(run)
        session.commit()
    url = f'/api/studio/projects/{old.project_id}/benchmarks'
    request = {'scene_id': old.scene_id, 'warm_shots': 5, 'max_wall_seconds': 600, 'max_cost_usd': 1}
    assert client.post(url, json={**request, 'max_cost_usd': None}).status_code == 409
    original = deepcopy(service.require(ProductionRun, run.id).snapshot)
    accepted = client.post(url, json=request)
    assert accepted.status_code == 201, accepted.text
    benchmark = service.require(Job, accepted.json()['id'])
    assert benchmark.snapshot['scene']['keyframe_id'] == approved['keyframe_id']
    assert service.require(ProductionRun, run.id).snapshot == original
    assert image.exists()
    with service.sessions() as session:
        session.get(Job, benchmark.id).status = 'cancelled'
        session.commit()
    alternative = client.post(url, json={**request, 'host_id': hosts[1], 'hourly_usd': .84})
    assert alternative.status_code == 201, alternative.text
    alt_job = service.require(Job, alternative.json()['id'])
    assert alt_job.host_id == hosts[1] and alt_job.snapshot['project']['hourly_usd'] == .84
    assert service.require(ProductionRun, run.id).snapshot == original
    with service.sessions() as session:
        session.get(Job, alt_job.id).status = 'cancelled'
        session.commit()
    jobs.checkpoint(old.id, 'clip-0', {'prompt_id': 'uncertain', 'state': 'submitted'})
    rejected = client.post(url, json=request)
    assert rejected.status_code == 409 and 'đối chiếu' in rejected.text


@pytest.mark.asyncio
async def test_known_failed_retries_get_distinct_ids_and_respect_benchmark_budget(local, monkeypatch):
    from studio.benchmark import BenchmarkBudgetReached
    mock = ComfyMock(local, monkeypatch)
    failed = set()
    response = mock.response
    def terminal(request):
        pid = request.url.path.rsplit('/', 1)[-1]
        if request.url.path.startswith('/history/') and pid in failed:
            mock.calls.append((request.method, request.url.path))
            return httpx.Response(200, json={pid: {'status': {'status_str': 'error'}, 'outputs': {}}})
        return response(request)
    mock.response = terminal
    await mock.generate()
    for _ in range(2):
        prior = mock.service.require(Job, mock.job.id).result['submissions']['clip-0']
        failed.add(prior['prompt_id'])
        mock.jobs.checkpoint(mock.job.id, 'clip-0', {'state': 'submitted'})
        await mock.generate()
    assert len(mock.submitted) == 3
    prior = mock.service.require(Job, mock.job.id).result['submissions']['clip-0']
    assert prior['retry_ordinal'] == 2 and len(prior['failed_prompt_ids']) == 2
    failed.add(prior['prompt_id'])
    mock.jobs.checkpoint(mock.job.id, 'clip-0', {'state': 'submitted'})
    with mock.service.sessions() as session:
        row = session.get(Job, mock.job.id)
        row.kind = 'benchmark'
        row.snapshot = {**row.snapshot, 'benchmark_remaining_wall_seconds': 0}
        session.commit()
    with pytest.raises(BenchmarkBudgetReached):
        await mock.generate()
    assert len(mock.submitted) == 3
    assert remote_settled(mock.service.require(Job, mock.job.id).result['submissions']['clip-0'])


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['generate', 'cancel'])
async def test_invalid_queue_never_submits_or_confirms_cancellation(local, monkeypatch, operation):
    mock = ComfyMock(local, monkeypatch)
    jobs, backend = mock.jobs, mock.backend
    backend.jobs = jobs
    jobs.checkpoint(mock.job.id, 'clip-0', {'state': 'submitted', 'prompt_id': 'uncertain'})
    response = mock.response
    def malformed(request):
        if request.url.path == '/queue':
            mock.calls.append((request.method, request.url.path))
            return httpx.Response(200, json={})
        return response(request)
    mock.response = malformed
    with pytest.raises(ReconcileRequired, match='chưa xác minh'):
        if operation == 'generate':
            await mock.generate()
        else:
            await backend.cancel(mock.service.require(Job, mock.job.id))
    assert not any(method == 'POST' for method, _ in mock.calls)
    assert 'cancel_confirmed_prompt_id' not in mock.service.require(Job, mock.job.id).result['submissions']['clip-0']


@pytest.mark.asyncio
async def test_dual_transport_checks_foreign_memory_before_intent_or_post(lanes, monkeypatch):
    _, service, jobs, old, image, hosts = lanes
    mock = ComfyMock((None, service, jobs, old, image), monkeypatch)
    backend = mock.backend
    backend.hosts, backend.jobs = jobs.hosts, jobs
    runtime = {**RUNTIME, 'comfy_version': 'test'}
    scene, project = old.snapshot['scene'], old.snapshot['project']
    config = clip_config(project, scene)
    graph = {'1': {'class_type': 'Test', 'inputs': {'steps': config['steps']}}}
    memory = {'gpu_uuid': 'GPU-a', 'gpu_name': 'Test96', 'gpu_total_bytes': 96*GIB}
    evidence = {'fingerprint': fingerprint(config, runtime, memory, capacity_graph_hash(graph)),
                'two_process_budget_bytes': 2*36*GIB*1.1}
    with service.sessions() as session:
        row = session.get(Job, old.id)
        row.host_id, row.kind = hosts[0], 'benchmark'
        row.snapshot = {**row.snapshot, 'benchmark_hosts': hosts, 'memory_evidence': evidence,
            'benchmark': {'model_residency': 'job', 'wan_concurrency': 2}}
        session.commit()
    connection = backend.connection
    @asynccontextmanager
    async def connected(host):
        async with connection(host) as (_, client):
            yield object(), client
    backend.connection = connected
    async def generation(executor, host):
        return '111:1' if host == hosts[0] else '222:1'
    async def versions(*args):
        return {'torch_version': runtime['torch_version'], 'cuda_version': runtime['cuda_version']}
    foreign = {'gib': 55}
    async def snapshot(executor):
        return {'gpus': [{'index': 0, 'uuid': 'GPU-a', 'name': 'Test96', 'total_mib': 96*1024,
                         'used_mib': (36+foreign['gib'])*1024, 'utilization': 95}],
                'processes': [{'uuid': 'GPU-a', 'pid': 111, 'used_mib': 36*1024},
                              {'uuid': 'GPU-a', 'pid': 333, 'used_mib': foreign['gib']*1024}]}
    async def watch(*args):
        return None
    backend.process_generation, backend.library_versions = generation, versions
    backend.telemetry.snapshot, backend.telemetry.watch = snapshot, watch
    with pytest.raises(ValueError, match='tiến trình khác'):
        await mock.generate()
    assert not mock.submitted and not service.require(Job, old.id).result.get('submissions')
    jobs.cost._save(hosts[0], {'fault': None})
    foreign['gib'] = 1
    await mock.generate()
    assert len(mock.submitted) == 1
    record = service.require(Job, old.id).result['submissions']['clip-0']
    assert record['cost_policy']['wan_concurrency'] == 2 and record['process_generation'] == '111:1'


@pytest.mark.asyncio
async def test_empty_queue_does_not_prove_ambiguous_post_was_cancelled(local, monkeypatch):
    mock = ComfyMock(local, monkeypatch)
    mock.backend.jobs = mock.jobs
    mock.jobs.checkpoint(mock.job.id, 'clip-0', {'state': 'submitting', 'prompt_id': 'uncertain'})
    with pytest.raises(ReconcileRequired, match='history còn thiếu'):
        await mock.backend.cancel(mock.service.require(Job, mock.job.id))
    saved = mock.service.require(Job, mock.job.id).result['submissions']['clip-0']
    assert not remote_settled(saved) and ('POST', '/prompt') not in mock.calls
    mock.submitted['uncertain'] = {}  # Comfy later proves the same prompt finished
    await mock.backend.cancel(mock.service.require(Job, mock.job.id))
    assert remote_settled(mock.service.require(Job, mock.job.id).result['submissions']['clip-0'])


@pytest.mark.asyncio
async def test_cancel_fence_rejects_new_intent_and_settles_the_latest_id(local, monkeypatch):
    _, service, jobs, old, _ = local
    with service.sessions() as session:
        row = session.get(Job, old.id)
        row.status = 'running'
        session.commit()
    patch = jobs.patch
    def cancel_after_new_intent(job_id, **values):
        if values.get('status') == 'cancelling':
            jobs.checkpoint(job_id, 'clip-1', {'state': 'submitted', 'prompt_id': 'latest'})
        patch(job_id, **values)
    monkeypatch.setattr(jobs, 'patch', cancel_after_new_intent)
    seen = []
    async def cancel(job):
        seen.append(job.result['submissions']['clip-1']['prompt_id'])
        with pytest.raises(asyncio.CancelledError):
            jobs.checkpoint(job.id, 'clip-2', {'state': 'submitting', 'prompt_id': 'never-posted'})
    monkeypatch.setattr(jobs.backend, 'cancel', cancel)
    assert (await jobs.cancel(old.id))['status'] == 'cancelled'
    assert seen == ['latest'] and 'clip-2' not in service.require(Job, old.id).result['submissions']


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['legacy-version', 'pending-steps', 'pending-frames'])
async def test_unmeasured_clip_remains_serial_even_beside_an_accepted_profile(lanes, monkeypatch, mismatch):
    _, service, jobs, old, _, hosts = lanes
    baseline, candidate = save_profile_reports(service, old, hosts[0])
    jobs.cost.apply(hosts[0], {'wan_concurrency': 2, 'baseline_id': baseline,
        'candidate_id': candidate, 'quality_review_passed': True})
    snapshot, result = deepcopy(old.snapshot), {}
    if mismatch == 'legacy-version':
        snapshot['generation_version'] = 3  # measured reports in this fixture are version 2
    else:
        override = {'clip_steps': 8} if mismatch == 'pending-steps' else {'shorten_last_shot': True}
        result['pending_clip_configs'] = {'clip-1': override}
    with service.sessions() as session:
        first = Job(project_id=old.project_id, host_id=hosts[0], kind='clip', status='queued',
            input_hash='unmeasured', snapshot=snapshot, result=result)
        second = Job(project_id=old.project_id, host_id=hosts[1], kind='clip', status='queued',
            input_hash='measured', snapshot=old.snapshot)
        session.add_all([first, second])
        session.commit()
    assert jobs.cost.config(first)['wan_concurrency'] == 1
    release = asyncio.Event()
    async def execute(job):
        await release.wait()
    monkeypatch.setattr(jobs, 'execute', execute)
    assert jobs.dispatch() == 1 and len(jobs.active) == 1
    release.set()
    await asyncio.gather(*jobs.active.values())
