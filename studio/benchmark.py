"""Opt-in benchmark through the same durable prompt transport as production."""
import asyncio
import json
import math
import time
from contextlib import AsyncExitStack
from copy import deepcopy
from statistics import median
from types import SimpleNamespace

from sqlalchemy import select, text

from studio.generation import clip_config, stage_steps
from studio.media import probe
from studio.models import Artifact, Job, ProductionRun
from studio.packs import GENERATION_VERSION, load_graph
from studio.service import canonical_hash


class BenchmarkBudgetReached(ValueError):
    """Stop at a settled boundary; cancelling the paused job preserves its outputs."""


def start(jobs, project_id, request):
    project = jobs.service.project(project_id)
    with jobs.service.sessions() as session:
        paused = session.scalar(select(ProductionRun).where(ProductionRun.project_id == project_id,
            ProductionRun.status == 'paused').order_by(ProductionRun.created_at.desc()))
        if paused:
            rate = project.get('hourly_usd')
            project = deepcopy(paused.snapshot)
            project['hourly_usd'] = rate
            for s in project['scenes']:
                s.update(paused.checkpoint.get('media', {}).get(s['id'], {}))
    def idle(session, hosts):
        from studio.cost_policy import result_settled
        from studio.jobs import ACTIVE
        if session.scalar(select(ProductionRun.id).where(ProductionRun.project_id == project_id,
                ProductionRun.status.in_(['running', 'pause_requested', 'reconciling']))):
            raise ValueError('Tạm dừng Production Run trước khi benchmark; giữ toàn bộ kết quả đã lưu.')
        for row in session.scalars(select(Job).where(
                (Job.project_id == project_id) | Job.host_id.in_(hosts))):
            if row.status in ACTIVE and not (row.status == 'paused' and row.kind in {'clip', 'rife'} and result_settled(row.result)):
                raise ValueError('Chờ GPU rảnh hoặc pause ở ranh giới đã lưu output; không benchmark khi còn prompt cần đối chiếu.')
            if not result_settled(row.result):
                raise ValueError('Còn intent chưa đối chiếu trên GPU/dự án; không tạo lượt benchmark mới.')
    scene = next((s for s in project['scenes'] if s['id'] == request.scene_id), None)
    if not scene or not scene.get('keyframe_id') or not scene.get('keyframe_approved'):
        raise ValueError('Benchmark cần ảnh đại diện đã duyệt.')
    jobs.service.artifact_path(scene['keyframe_id'])
    host_id = request.host_id or project.get('host_id')
    if not host_id:
        raise ValueError('Chọn GPU trước khi benchmark.')
    jobs.hosts._require_host(host_id)
    project['host_id'] = host_id  # benchmark only; never edit the production snapshot
    if request.hourly_usd is not None:
        project['hourly_usd'] = request.hourly_usd
    with jobs.service.sessions() as session:
        idle(session, jobs.cost.siblings(host_id))
    jobs.recipes.assert_recipes_idle(host_id)
    if not jobs.installations.component_proven(host_id, ['wan_i2v']):
        raise ValueError('Wan chưa sẵn sàng trên GPU này.')
    scenes = [scene]
    if request.model_residency == 'stage' and not request.second_scene_id:
        raise ValueError('Đo giữ model theo công đoạn cần hai cảnh liên tiếp.')
    if request.second_scene_id:
        other = next((s for s in project['scenes'] if s['id'] == request.second_scene_id), None)
        if not other or other['id'] == scene['id'] or not other.get('keyframe_approved') or not other.get('keyframe_id'):
            raise ValueError('Chọn hai cảnh khác nhau, đều có ảnh đã duyệt để đo ranh giới cảnh.')
        jobs.service.artifact_path(other['keyframe_id'])
        if clip_config(project, other) != clip_config(project, scene):
            raise ValueError('Hai cảnh benchmark phải có cùng cấu hình Wan.')
        scenes.append(other)
    if not request.max_cost_usd or not project.get('hourly_usd'):
        raise ValueError('Chốt giá thuê thực mỗi giờ và giới hạn USD trước khi chạy benchmark có phí.')
    hosts, memory = [host_id], None
    if request.wan_concurrency == 2:
        if not request.second_scene_id and request.warm_shots < 10:
            raise ValueError('Benchmark hai Wan cần cùng bộ ít nhất 10 shot nóng.')
        baseline = jobs.service.require(Job, request.memory_baseline_id) if request.memory_baseline_id else None
        if not baseline or baseline.kind != 'benchmark' or baseline.status != 'completed' or baseline.host_id not in jobs.cost.siblings(host_id):
            raise ValueError('Cần benchmark VRAM đã hoàn thành trên đúng GPU trước khi thử hai Wan.')
        from studio.cost_policy import measured_capacity
        memory = measured_capacity(baseline.result['benchmark_report'])
        if not memory['fits_two']:
            raise ValueError('VRAM đo được chưa đủ hai tiến trình cộng dự phòng 10% mỗi tiến trình và 10% GPU.')
        second = next((h for h in jobs.cost.siblings(host_id) if h != host_id and
                       jobs.installations.component_proven(h, ['wan_i2v'])), None)
        if not second:
            raise ValueError('Cần hai tiến trình ComfyUI đã kiểm chứng trên cùng GPU.')
        jobs.recipes.assert_recipes_idle(second)
        hosts.append(second)
    snapshot = {'project': project, 'scene': scene, 'benchmark_scenes': scenes,
                'benchmark_hosts': hosts, 'memory_evidence': memory,
                'benchmark': request.model_dump(), 'generation_version': GENERATION_VERSION}
    with jobs.service.sessions() as session:
        session.execute(text('BEGIN IMMEDIATE'))
        idle(session, jobs.cost.siblings(host_id))
        job = Job(project_id=project_id, scene_id=scene['id'], host_id=host_id,
                  kind='benchmark', snapshot=snapshot, input_hash=canonical_hash(snapshot))
        session.add(job)
        session.commit()
        return jobs.service.read(job)


def report(submissions, durations, hourly_usd=None):
    warm = [s for s in submissions if not s.get('cold_candidate') and not s.get('boundary_sample') and not s.get('retry_ordinal') and s.get('state') == 'downloaded' and s.get('attempts', 1) == 1]
    wall = [s['timing']['total_seconds'] for s in warm if s.get('timing', {}).get('total_seconds') is not None]
    execution = [s['timing']['comfy_execution_seconds'] for s in warm if s.get('timing', {}).get('comfy_execution_seconds') is not None]
    seconds = median(wall) if wall else None
    intervals = [(s['benchmark_started_at'], s['benchmark_finished_at']) for s in warm
                 if s.get('benchmark_started_at') is not None and s.get('benchmark_finished_at') is not None]
    hot_elapsed = max(b for _, b in intervals) - min(a for a, _ in intervals) if intervals else sum(wall)
    throughput = len(wall) * 3600 / hot_elapsed if hot_elapsed > 0 else None
    all_intervals = [(s['benchmark_started_at'], s['benchmark_finished_at']) for s in submissions
                     if s.get('benchmark_started_at') is not None and s.get('benchmark_finished_at') is not None]
    stage_elapsed = max(b for _, b in all_intervals) - min(a for a, _ in all_intervals) if all_intervals else sum(s.get('timing', {}).get('total_seconds', 0) for s in submissions)
    scenarios = []
    total = sum(durations)
    for target in (300, 326):
        count = sum(math.ceil((d * target / total) / (81 / 16)) for d in durations) if total else None
        hours = count / throughput if count is not None and throughput else None
        scenarios.append({'audio_seconds': target, 'shots': count, 'gpu_hours_estimate': hours,
                          'usd_estimate': hours * hourly_usd if hours is not None and hourly_usd is not None else None})
    return {'warm_samples': len(wall), 'median_hot_wall_seconds': seconds,
            'hot_wall_total_seconds': sum(wall),
            'hot_stage_wall_seconds': hot_elapsed, 'clips_per_gpu_hour': throughput,
            'measured_stage_wall_seconds': stage_elapsed,
            'usd_per_finished_minute': (hourly_usd / throughput / (81 / 16 / 60)) if hourly_usd is not None and throughput else None,
            'hourly_usd': hourly_usd,
            'median_hot_comfy_seconds': median(execution) if execution else None,
            'scenarios': scenarios, 'quality_review': 'required', 'measured_speedup': None,
            'extrapolation': 'same scene-duration proportions; excludes cold start, review and pod idle time'}


async def execute(jobs, job):
    if job.result.get('benchmark_report') and job.result.get('artifact_ids') and all(
            jobs.artifact_valid(id) for id in job.result['artifact_ids']):
        return
    request = job.snapshot['benchmark']
    if job.snapshot.get('benchmark_hosts'):
        return await execute_profile(jobs, job)
    scene, project = job.snapshot['scene'], job.snapshot['project']
    prompt = f"{project['style']}. {project['era']}. {project['location']}. {scene['visual_prompt']}. {scene['camera']}"
    started = time.monotonic()
    previous_elapsed = job.result.get('benchmark_elapsed_seconds', 0)
    def log(message):
        return jobs.event(job.id, message)
    def checkpoint(stage, value):
        return jobs.checkpoint(job.id, stage, value)
    ids = [id for id in job.result.get('artifact_ids', []) if jobs.artifact_valid(id)]
    try:
        async with AsyncExitStack() as stack:
            if hasattr(jobs.backend, 'generation_session'):
                await stack.enter_async_context(jobs.backend.generation_session(job))
            for index in range(request['warm_shots'] + 1):
                current = jobs.service.require(Job, job.id)
                if current.status == 'cancelling':
                    raise asyncio.CancelledError()
                if current.status == 'reconciling':
                    from studio.backend import ReconcileRequired
                    raise ReconcileRequired('Benchmark cần đối chiếu, không gửi shot tiếp theo.')
                stage = f'benchmark-{index}'
                prior = current.result.get('submissions', {}).get(stage, {})
                if prior.get('artifact_id') and jobs.artifact_valid(prior['artifact_id']):
                    if prior['artifact_id'] not in ids:
                        ids.append(prior['artifact_id'])
                    continue
                if not prior and previous_elapsed + time.monotonic() - started >= request['max_wall_seconds']:
                    raise BenchmarkBudgetReached('Đã chạm giới hạn benchmark ở ranh giới shot; giữ kết quả đã xong. Không gửi lượt tiếp theo.')
                path = await jobs.backend.generate(current, 'wan_i2v', prompt, [jobs.service.artifact_path(scene['keyframe_id'])],
                    scene['seed'], project['quality'], log, checkpoint, stage, stage_steps(scene, 'clip'), index)
                record = jobs.service.require(Job, job.id).result['submissions'][stage]
                storage_started = time.monotonic()
                artifact = jobs.service.artifact(path, job.project_id, stage + '.mp4', job.id, {'generation': record})
                storage_seconds = time.monotonic() - storage_started
                checkpoint(stage, {'artifact_id': artifact['id'], 'timing': {
                    'storage_seconds': storage_seconds,
                    'total_seconds': record.get('timing', {}).get('total_seconds', 0) + storage_seconds}})
                ids.append(artifact['id'])
                jobs.patch(job.id, progress=round((index+1)*95/(request['warm_shots']+1)))
        submissions = jobs.service.require(Job, job.id).result.get('submissions', {})
        durations = [probe(jobs.service.artifact_path(s['speech_id']))['duration'] for s in project['scenes'] if s.get('speech_id')]
        result = report(list(submissions.values()), durations if len(durations) == len(project['scenes']) else [], project.get('hourly_usd'))
        result.update(config=clip_config(project, scene), shots=submissions,
                      benchmark_input_hash=canonical_hash({'prompt': prompt, 'seed': scene['seed'],
                          'warm_shots': request['warm_shots'],
                          'image': jobs.service.require(Artifact, scene['keyframe_id']).sha256,
                          'config': clip_config(project, scene)}))
        path = jobs.service.job_directory(job.id) / 'benchmark.json'
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        artifact = jobs.service.artifact(path, job.project_id, 'benchmark.json', job.id)
        jobs.patch(job.id, result={**jobs.service.require(Job, job.id).result,
                                  'benchmark_report': result, 'artifact_ids': [*ids, artifact['id']]})
    finally:
        jobs.patch(job.id, result={**jobs.service.require(Job, job.id).result, 'artifact_ids':
            jobs.service.require(Job, job.id).result.get('artifact_ids', ids),
            'benchmark_elapsed_seconds': previous_elapsed + time.monotonic() - started})


def compare(jobs, baseline_id, candidate_id):
    baseline, candidate = (jobs.service.require(Job, id) for id in (baseline_id, candidate_id))
    a, b = (j.result.get('benchmark_report', {}) for j in (baseline, candidate))
    if any(j.kind != 'benchmark' or j.status != 'completed' for j in (baseline, candidate)):
        raise ValueError('Cần hai benchmark đã hoàn thành.')
    if a.get('benchmark_input_hash') != b.get('benchmark_input_hash') or baseline.host_id != candidate.host_id:
        raise ValueError('Chỉ so cùng input/cấu hình và cùng host GPU.')
    if min(a.get('warm_samples', 0), b.get('warm_samples', 0)) < 5:
        raise ValueError('Cần ít nhất 5 shot nóng hợp lệ mỗi cấu hình.')
    def environment(r):
        values = [s.get('runtime', {}) for s in r['shots'].values()]
        if not values or any(v != values[0] for v in values):
            raise ValueError('Runtime thay đổi giữa benchmark; chạy lại bộ đo riêng.')
        return {k: values[0].get(k) for k in ('devices', 'comfy_version', 'torch_version', 'cuda_version')}
    baseline_environment, candidate_environment = environment(a), environment(b)
    if baseline_environment != candidate_environment or not all(baseline_environment.values()):
        raise ValueError('Thiếu bằng chứng cùng GPU/VRAM và phiên bản Comfy/Torch/CUDA.')
    if not b.get('median_hot_wall_seconds'):
        raise ValueError('Không có số đo thời gian hợp lệ.')
    return {'baseline': baseline_id, 'candidate': candidate_id,
            'wall_speedup': a['median_hot_wall_seconds'] / b['median_hot_wall_seconds'],
            'quality_review': 'required', 'auto_enable': False}


async def execute_profile(jobs, job):
    """Separate Comfy lanes, common deterministic hot input set, durable per-shot IDs."""
    request, project = job.snapshot['benchmark'], job.snapshot['project']
    scenes, hosts = job.snapshot['benchmark_scenes'], job.snapshot['benchmark_hosts']
    started, previous = time.monotonic(), job.result.get('benchmark_elapsed_seconds', 0)
    budget = request.get('max_cost_usd')
    limit = min(request['max_wall_seconds'], budget / project['hourly_usd'] * 3600) if budget and project.get('hourly_usd') else request['max_wall_seconds']
    halted = asyncio.Event()
    ids = [id for id in job.result.get('artifact_ids', []) if jobs.artifact_valid(id)]
    hot_count = request['warm_shots'] * len(scenes)
    groups = [[(scenes[0], i) for i in range(request['warm_shots'])]]
    if len(scenes) == 2:
        groups.append([(scenes[1], request['warm_shots'] + i) for i in range(request['warm_shots'])])
    elif len(hosts) == 2:
        inputs = groups[0]
        groups = [inputs[::2], inputs[1::2]]
    errors = []

    async def run_group(group, host_id, group_index, retain_next=False):
        scene = group[0][0]
        snap = {**job.snapshot, 'scene': scene, 'residency_next_ready': retain_next}
        clone = SimpleNamespace(id=job.id, project_id=job.project_id, scene_id=scene['id'], host_id=host_id,
                                kind='benchmark', snapshot=snap, result={})
        prompt = f"{project['style']}. {project['era']}. {project['location']}. {scene['visual_prompt']}. {scene['camera']}"
        def checkpoint(stage, value):
            jobs.checkpoint(job.id, stage, value)
        async with AsyncExitStack() as stack:
            if hasattr(jobs.backend, 'generation_session'):
                await stack.enter_async_context(jobs.backend.generation_session(clone))
            # Each boundary has the same excluded startup sample in the serial and
            # retained profiles; only the hot deterministic inputs enter throughput.
            samples = [(f'benchmark-cold-{group_index}', scene, group[0][1], True),
                       *((f'benchmark-hot-{i}', s, i, False) for s, i in group)]
            for stage, shot_scene, index, boundary in samples:
                current = jobs.service.require(Job, job.id)
                prior = current.result.get('submissions', {}).get(stage, {})
                clone.result = current.result
                if current.status == 'cancelling':
                    halted.set()
                    raise asyncio.CancelledError()
                if prior.get('artifact_id') and jobs.artifact_valid(prior['artifact_id']):
                    if prior['artifact_id'] not in ids:
                        ids.append(prior['artifact_id'])
                    continue
                if halted.is_set() and not prior:
                    break  # settle in-flight IDs, never dispatch after the peer failed
                if not prior and previous + time.monotonic() - started >= limit:
                    halted.set()
                    raise BenchmarkBudgetReached('Đã chạm giới hạn benchmark thời gian/USD ở ranh giới shot. Giữ clip đã lưu; hủy job đã pause nếu muốn tạo lượt đo với ngân sách mới.')
                before = time.time()
                clone.snapshot['benchmark_remaining_wall_seconds'] = max(0, limit - previous - (time.monotonic() - started))
                try:
                    path = await jobs.backend.generate(clone, 'wan_i2v', prompt,
                        [jobs.service.artifact_path(shot_scene['keyframe_id'])], shot_scene['seed'],
                        project['quality'], lambda message: jobs.event(job.id, message), checkpoint,
                        stage, stage_steps(shot_scene, 'clip'), index)
                    record = jobs.service.require(Job, job.id).result['submissions'][stage]
                    storage_started = time.monotonic()
                    artifact = jobs.service.artifact(path, job.project_id, stage + '.mp4', job.id, {'generation': record})
                    storage_seconds = time.monotonic() - storage_started
                    checkpoint(stage, {'artifact_id': artifact['id'], 'host_id': host_id,
                        'boundary_sample': boundary, 'benchmark_started_at': prior.get('benchmark_started_at', before),
                        'benchmark_finished_at': time.time(), 'timing': {'storage_seconds': storage_seconds,
                        'total_seconds': record.get('timing', {}).get('total_seconds', 0) + storage_seconds}})
                    ids.append(artifact['id'])
                except BaseException:
                    halted.set()
                    raise

    async def run_lane(lane_groups, host_id, lane_index):
        for group_index, group in enumerate(lane_groups):
            await run_group(group, host_id, group_index * len(hosts) + lane_index,
                            retain_next=group_index + 1 < len(lane_groups))

    try:
        if len(hosts) == 2:
            generations = []
            for host_id in hosts:
                async with jobs.backend.connection(host_id) as (executor, _):
                    generations.append(await jobs.backend.process_generation(executor, host_id))
            if any(not g for g in generations) or len(set(generations)) != len(hosts):
                raise ValueError('Chưa xác minh hai PID Comfy riêng; không gửi benchmark song song.')
            lane_groups = ([[[(s, n) for s, n in g if n % 2 == i] for g in groups] for i in range(2)] if len(scenes) == 2
                           else [[g] for g in groups])
            outcomes = await asyncio.gather(*(run_lane(g, hosts[i], i) for i, g in enumerate(lane_groups)), return_exceptions=True)
            errors = [r for r in outcomes if isinstance(r, BaseException)]
            if errors:
                raise next((e for e in errors if not isinstance(e, BenchmarkBudgetReached)), errors[0])
        else:
            for index, group in enumerate(groups):
                await run_group(group, hosts[0], index, retain_next=index + 1 < len(groups))
        submissions = jobs.service.require(Job, job.id).result.get('submissions', {})
        durations = [probe(jobs.service.artifact_path(s['speech_id']))['duration'] for s in project['scenes'] if s.get('speech_id')]
        result = report(list(submissions.values()), durations if len(durations) == len(project['scenes']) else [], project.get('hourly_usd'))
        result.update(config=clip_config(project, scenes[0]), shots=submissions,
            workflow_hash=canonical_hash(load_graph('wan_i2v')),
            measured_stage_wall_seconds=previous + time.monotonic() - started,
            policy={k: request.get(k, v) for k, v in {'model_residency': 'job', 'wan_concurrency': 1}.items()},
            generation_version=job.snapshot.get('generation_version', 2),
            expected_hot_shots=hot_count)
        # Sort interleaved groups by shot index for the same comparison input hash.
        ordered = sorted((i, s) for group in groups for s, i in group)
        result['benchmark_input_hash'] = canonical_hash({'hot_inputs': [
            {'prompt': f"{project['style']}. {project['era']}. {project['location']}. {s['visual_prompt']}. {s['camera']}",
             'image': jobs.service.require(Artifact, s['keyframe_id']).sha256,
             'seed': s['seed'], 'index': i, 'config': clip_config(project, s)} for i, s in ordered],
            'workflow': result['workflow_hash']})
        def peak(key):
            values = [s.get('telemetry', {}).get(key) for s in submissions.values()]
            return max((v for v in values if v is not None), default=None)
        result.update(process_peak_vram_bytes=peak('process_peak_vram_bytes'), gpu_peak_used_bytes=peak('gpu_peak_used_bytes'),
                      host_peak_ram_bytes=peak('host_peak_ram_bytes'),
                      transport_gap_seconds=sum(s.get('timing', {}).get('inter_job_gap_seconds', 0) for s in submissions.values()),
                      measured_processing_usd=(previous + time.monotonic() - started) / 3600 * project['hourly_usd'] if project.get('hourly_usd') is not None else None,
                      budget_usd=budget, billing_note='Includes measured benchmark processing only; storage and Pod waiting are separate.')
        boundaries = [s.get('timing', {}).get('total_seconds') for s in submissions.values() if s.get('boundary_sample')]
        boundaries = [v for v in boundaries if v is not None]
        result['boundary_shot_seconds'] = boundaries
        result['cold_boundary_overhead_estimate_seconds'] = max(0, median(boundaries) - result['median_hot_wall_seconds']) if boundaries and result['median_hot_wall_seconds'] is not None else None
        result['model_load_seconds'] = None
        result['model_load_note'] = 'Boundary overhead is a cold-versus-hot estimate, not an isolated GPU/model-loading timer.'
        path = jobs.service.job_directory(job.id) / 'benchmark.json'
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        artifact = jobs.service.artifact(path, job.project_id, 'benchmark.json', job.id)
        jobs.patch(job.id, result={**jobs.service.require(Job, job.id).result,
                                  'benchmark_report': result, 'artifact_ids': [*ids, artifact['id']]})
    finally:
        for host_id in hosts:
            if hasattr(jobs.backend, 'release_resident'):
                await jobs.backend.release_resident(host_id)
        jobs.patch(job.id, result={**jobs.service.require(Job, job.id).result,
            'artifact_ids': jobs.service.require(Job, job.id).result.get('artifact_ids', ids),
            'benchmark_elapsed_seconds': previous + time.monotonic() - started})


def compare_cost(jobs, baseline_id, candidate_id):
    if not baseline_id or not candidate_id or baseline_id == candidate_id:
        raise ValueError('Chọn baseline và candidate khác nhau đã hoàn tất.')
    baseline, candidate = (jobs.service.require(Job, id) for id in (baseline_id, candidate_id))
    if any(j.kind != 'benchmark' or j.status != 'completed' for j in (baseline, candidate)):
        raise ValueError('Cần hai benchmark đã hoàn thành; lượt lỗi/OOM không đủ điều kiện.')
    a, b = (j.result.get('benchmark_report', {}) for j in (baseline, candidate))
    if any(s.get('attempts', 1) != 1 or s.get('retry_ordinal', 0) for r in (a, b) for s in r.get('shots', {}).values()):
        raise ValueError('Lượt đo đã phải tiếp tục/đối chiếu; dùng bộ đo sạch để quyết định thông lượng và chi phí.')
    if not a.get('benchmark_input_hash') or a.get('benchmark_input_hash') != b.get('benchmark_input_hash'):
        raise ValueError('Chỉ so cùng bộ input/seed/workflow/kích thước và số shot.')
    if min(a.get('warm_samples', 0), b.get('warm_samples', 0)) < 5:
        raise ValueError('Mỗi cấu hình cần ít nhất 5 shot nóng.')
    from studio.cost_policy import measured_capacity
    ca, cb = measured_capacity(a), measured_capacity(b)
    ar = next(iter(a['shots'].values()))['runtime']
    br = next(iter(b['shots'].values()))['runtime']
    if any(ar.get(k) != br.get(k) for k in ('comfy_version', 'torch_version', 'cuda_version')):
        raise ValueError('Phiên bản Comfy/Torch/CUDA khác nhau; không quy cải thiện cho một thay đổi runtime.')
    auuid = next(iter(a['shots'].values()))['telemetry']['gpu_uuid']
    buuid = next(iter(b['shots'].values()))['telemetry']['gpu_uuid']
    changed = auuid != buuid
    throughput = b.get('clips_per_gpu_hour', 0) / a.get('clips_per_gpu_hour', 1)
    time_ratio = a['measured_stage_wall_seconds'] / b['measured_stage_wall_seconds']
    cost_a, cost_b = a.get('measured_processing_usd'), b.get('measured_processing_usd')
    saving = 1 - cost_b / cost_a if cost_a and cost_b is not None else None
    concurrent = b.get('policy', {}).get('wan_concurrency') == 2
    eligible = (time_ratio >= 1 and saving is not None and saving >= .2) if changed else (
        time_ratio > 1 and (not concurrent or (throughput >= 1.2 and cb['fits_two'] and
        min(a.get('warm_samples', 0), b.get('warm_samples', 0)) >= 10)))
    return {'baseline': baseline_id, 'candidate': candidate_id, 'hardware_changed': changed,
            'stage_wall_speedup': time_ratio, 'throughput_gain': throughput, 'processing_cost_saving': saving,
            'baseline_capacity': ca, 'candidate_capacity': cb, 'candidate_eligible': eligible,
            'quality_review': 'required', 'auto_enable': False}
