"""Opt-in benchmark through the same durable prompt transport as production."""
import asyncio
import json
import math
import time
from contextlib import AsyncExitStack
from statistics import median

from sqlalchemy import select, text

from studio.generation import clip_config, stage_steps
from studio.media import probe
from studio.models import Artifact, Job
from studio.service import canonical_hash


def start(jobs, project_id, request):
    project = jobs.service.project(project_id)
    jobs.service.assert_idle(project_id)
    scene = next((s for s in project['scenes'] if s['id'] == request.scene_id), None)
    if not scene or not scene.get('keyframe_id') or not scene.get('keyframe_approved'):
        raise ValueError('Benchmark cần ảnh đại diện đã duyệt.')
    jobs.service.artifact_path(scene['keyframe_id'])
    host_id = project.get('host_id')
    if not host_id:
        raise ValueError('Chọn GPU trước khi benchmark.')
    jobs.host_idle(host_id)
    jobs.recipes.assert_recipes_idle(host_id)
    if not jobs.installations.component_proven(host_id, ['wan_i2v']):
        raise ValueError('Wan chưa sẵn sàng trên GPU này.')
    snapshot = {'project': project, 'scene': scene, 'benchmark': request.model_dump(), 'generation_version': 2}
    with jobs.service.sessions() as session:
        from studio.jobs import ACTIVE
        session.execute(text('BEGIN IMMEDIATE'))
        if session.scalar(select(Job.id).where(Job.host_id == host_id, Job.status.in_(ACTIVE))):
            raise ValueError('Host đã có job mới; không gửi benchmark trùng.')
        job = Job(project_id=project_id, scene_id=scene['id'], host_id=host_id,
                  kind='benchmark', snapshot=snapshot, input_hash=canonical_hash(snapshot))
        session.add(job)
        session.commit()
        return jobs.service.read(job)


def report(submissions, durations, hourly_usd=None):
    warm = [s for s in submissions if not s.get('cold_candidate') and s.get('state') == 'downloaded' and s.get('attempts', 1) == 1]
    wall = [s['timing']['total_seconds'] for s in warm if s.get('timing', {}).get('total_seconds') is not None]
    execution = [s['timing']['comfy_execution_seconds'] for s in warm if s.get('timing', {}).get('comfy_execution_seconds') is not None]
    seconds = median(wall) if wall else None
    scenarios = []
    total = sum(durations)
    for target in (300, 326):
        count = sum(math.ceil((d * target / total) / (81 / 16)) for d in durations) if total else None
        hours = count * seconds / 3600 if count is not None and seconds is not None else None
        scenarios.append({'audio_seconds': target, 'shots': count, 'gpu_hours_estimate': hours,
                          'usd_estimate': hours * hourly_usd if hours is not None and hourly_usd is not None else None})
    return {'warm_samples': len(wall), 'median_hot_wall_seconds': seconds,
            'hot_wall_total_seconds': sum(wall),
            'measured_stage_wall_seconds': sum(s.get('timing', {}).get('total_seconds', 0) for s in submissions),
            'median_hot_comfy_seconds': median(execution) if execution else None,
            'scenarios': scenarios, 'quality_review': 'required', 'measured_speedup': None,
            'extrapolation': 'same scene-duration proportions; excludes cold start, review and pod idle time'}


async def execute(jobs, job):
    if job.result.get('benchmark_report') and job.result.get('artifact_ids') and all(
            jobs.artifact_valid(id) for id in job.result['artifact_ids']):
        return
    request = job.snapshot['benchmark']
    scene, project = job.snapshot['scene'], job.snapshot['project']
    prompt = f"{project['style']}. {project['era']}. {project['location']}. {scene['visual_prompt']}. {scene['camera']}"
    started = time.monotonic()
    previous_elapsed = job.result.get('benchmark_elapsed_seconds', 0)
    log = lambda message: jobs.event(job.id, message)
    checkpoint = lambda stage, value: jobs.checkpoint(job.id, stage, value)
    ids = list(job.result.get('artifact_ids', []))
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
                    raise ValueError('Đã chạm giới hạn benchmark ở ranh giới shot; giữ kết quả đã xong. Không gửi lượt tiếp theo.')
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
