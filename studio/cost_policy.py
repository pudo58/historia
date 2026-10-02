"""Explicitly accepted benchmark profiles; unknown hardware/config stays serial."""
import json
from datetime import UTC, datetime

from sqlalchemy import select, text

from studio.generation import clip_config
from studio.gpu_memory import gpu_key
from studio.models import Job, ProductionRun
from studio.packs import load_graph
from studio.schemas import GPUCostConfig
from studio.service import canonical_hash

DEFAULT = {'model_residency': 'job', 'wan_concurrency': 1}


def remote_settled(submission):
    prompt_id = submission.get('prompt_id')
    return (submission.get('state') == 'downloaded' or bool(prompt_id and (
        submission.get('cancel_confirmed_prompt_id') == prompt_id or
        (submission.get('remote_terminal_prompt_id') == prompt_id and submission.get('remote_terminal_state') == 'error'))))


def result_settled(result):
    pending = (result.get('maintenance_pending') or result.get('speech_pending') or
               result.get('review_pending') or result.get('outline_pending') or result.get('chapter_pending') is not None)
    return not pending and all(remote_settled(s) for s in result.get('submissions', {}).values())


def capacity_graph_hash(graph):
    # Keep model names, links, sampler, dtype, size and frame count. Only the
    # scene-dependent content/seed and file names differ between equivalent shots.
    variable = {'text', 'prompt', 'image', 'filename_prefix', 'noise_seed', 'seed'}
    return canonical_hash({id: {**node, 'inputs': {k: ('<scene-input>' if k in variable and not isinstance(v, list) else v)
        for k, v in node['inputs'].items()}} for id, node in graph.items()})


def fingerprint(config, runtime, telemetry, workflow=None):
    """No prompt, seed, process PID or output prefix in the capacity identity."""
    required = ('comfy_version', 'torch_version', 'cuda_version', 'attention_backend', 'memory_policy')
    if not telemetry.get('gpu_uuid') or not telemetry.get('gpu_total_bytes') or any(not runtime.get(k) for k in required):
        return None
    return canonical_hash({'gpu_uuid': telemetry['gpu_uuid'], 'gpu_name': telemetry.get('gpu_name'),
        'gpu_total_bytes': telemetry['gpu_total_bytes'], 'runtime': {k: runtime[k] for k in required},
        'compute_flags': runtime.get('compute_flags', []), 'sageattention_version': runtime.get('sageattention_version'),
        'config': config, 'workflow': workflow or canonical_hash(load_graph('wan_i2v'))})


def measured_capacity(report):
    from studio.gpu_telemetry import finite
    if any(s.get('attempts', 1) != 1 or s.get('retry_ordinal', 0) for s in report.get('shots', {}).values()):
        raise ValueError('Lượt đo có retry/đối chiếu; chưa đủ hồ sơ VRAM sạch để mở hai Wan.')
    records = [s for s in report.get('shots', {}).values() if s.get('state') == 'downloaded' and s.get('attempts', 1) == 1]
    warm = [s for s in records if not s.get('cold_candidate')]
    if len(warm) < 5:
        raise ValueError('Cần ít nhất 5 shot nóng có số đo VRAM theo tiến trình.')
    identities = {s.get('capacity_fingerprint') for s in records}
    if len(identities) != 1 or None in identities:
        raise ValueError('Thiếu hoặc thay đổi GPU/workflow/runtime giữa lượt đo.')
    if any(not finite(s.get('telemetry', {}).get('process_peak_vram_bytes')) or
           not finite(s.get('telemetry', {}).get('gpu_total_bytes')) or
           s['telemetry']['process_peak_vram_bytes'] <= 0 or
           s['telemetry']['process_peak_vram_bytes'] > s['telemetry']['gpu_total_bytes'] or
           s['telemetry'].get('identity_changed') or s['telemetry'].get('unavailable') for s in records):
        raise ValueError('Thiếu số đo VRAM theo PID; không dùng VRAM allocator hoặc ước đoán thay thế.')
    peak = max(s['telemetry']['process_peak_vram_bytes'] for s in records)
    total = min(s['telemetry']['gpu_total_bytes'] for s in records)
    return {'fingerprint': next(iter(identities)), 'peak_bytes': peak, 'total_bytes': total,
            'gpu_uuid': records[0]['telemetry'].get('gpu_uuid'), 'gpu_name': records[0]['telemetry'].get('gpu_name'),
            'two_process_budget_bytes': 2 * peak * 1.1, 'headroom_bytes': total * .1,
            'fits_two': 2 * peak * 1.1 <= total * .9}


class CostPolicy:
    def __init__(self, jobs):
        self.jobs, self.service, self.hosts = jobs, jobs.service, jobs.hosts

    def key(self, host_id):
        return 'studio_gpu_cost:' + canonical_hash(gpu_key(host_id, self.jobs.installations.lane(host_id)))

    def state(self, host_id):
        self.hosts._require_host(host_id)
        saved = json.loads(self.hosts.setting(self.key(host_id)) or '{}')
        return {'config': {**DEFAULT, **saved.get('config', {})}, 'evidence': saved.get('evidence'),
                'fault': saved.get('fault'), 'profile_version': 1,
                'measurement': saved.get('measurement'), 'retained_seconds': 60}

    def config(self, job):
        if job.kind == 'benchmark':
            request = job.snapshot.get('benchmark', {})
            return {k: request.get(k, v) for k, v in DEFAULT.items()}
        if not job.host_id:
            return dict(DEFAULT)
        saved = self.state(job.host_id)
        config = dict(saved['config'])
        if config != DEFAULT and (saved.get('evidence') or {}).get('generation_version', 2) != job.snapshot.get('generation_version', 2):
            return dict(DEFAULT)
        if job.kind != 'clip':
            config['wan_concurrency'] = 1
        elif config['wan_concurrency'] == 2:
            scene, project = job.snapshot.get('scene') or {}, job.snapshot.get('project', {})
            evidence = saved.get('evidence') or {}
            overrides = job.result.get('pending_clip_configs', {})
            if (not self.compatible_job(job.host_id, project, scene) or
                    any(v.get('shorten_last_shot') or clip_config(project, scene, v) != evidence.get('config')
                        for stage, v in overrides.items() if not job.result.get('submissions', {}).get(stage)) or
                    any(not remote_settled(v) and (v.get('config') != evidence.get('config') or
                        v.get('capacity_fingerprint') != evidence.get('fingerprint'))
                        for v in job.result.get('submissions', {}).values())):
                return dict(DEFAULT)
        return config

    def _save(self, host_id, updates, validate=None):
        from ghm.models import LocalSetting
        key = self.key(host_id)
        with self.service.sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            if validate:
                validate(session)
            row = session.get(LocalSetting, key)
            saved = json.loads(self.hosts._secrets.decrypt(row.encrypted_value)) if row else {}
            saved.update(updates)
            session.merge(LocalSetting(name=key, encrypted_value=self.hosts._secrets.encrypt(json.dumps(saved))))
            session.commit()

    def siblings(self, host_id):
        lane = self.jobs.installations.lane(host_id)
        if not lane:
            return [host_id]
        return [h for h in dict.fromkeys([host_id, *self.jobs.installations.lanes_of(lane['pod_id'])])
                if gpu_key(h, self.jobs.installations.lane(h)) == gpu_key(host_id, lane)]

    def next_ready(self, job):
        if job.kind == 'benchmark':
            return job.snapshot.get('residency_next_ready', False)
        with self.service.sessions() as session:
            run_id = job.snapshot.get('production_run_id')
            run = session.get(ProductionRun, run_id) if run_id else None
            if run_id and (not run or run.status != 'running' or run.stage != job.kind):
                return False
            if session.scalar(select(Job.id).where(Job.host_id == job.host_id,
                    Job.id != job.id, Job.kind == job.kind, Job.status == 'queued')):
                return True
            if not run or run.status != 'running' or run.stage != job.kind:
                return False
            return any(f'{job.kind}:{s["id"]}' not in run.checkpoint.get('jobs', {})
                       for s in run.snapshot.get('scenes', []))

    def compatible_job(self, host_id, project, scene):
        state = self.state(host_id)
        evidence = state.get('evidence') or {}
        from studio.runtime import RuntimeConfig
        runtime = self.jobs.runtime.state(host_id)['config'] or RuntimeConfig().model_dump()
        adopted = self.hosts.options_for(host_id).adopt_existing
        report = self.hosts.latest_preflight(host_id)
        if (not report or not report.gpu or report.gpu.name != evidence.get('gpu_name') or
                abs(report.gpu.vram_gb * 1024**3 - evidence.get('total_bytes', 0)) > 6 * 1024**2):
            return False
        return bool(state['config']['wan_concurrency'] == 2 and not state.get('fault') and not scene.get('shorten_last_shot') and
                    evidence.get('config') == clip_config(project, scene) and
                    evidence.get('workflow_hash') == canonical_hash(load_graph('wan_i2v')) and
                    (adopted or all(evidence.get('runtime', {}).get(k) == v for k, v in runtime.items())))

    def clip_lanes(self, hosts, project, scene):
        result, counts = [], {}
        for host_id in hosts:
            key = gpu_key(host_id, self.jobs.installations.lane(host_id))
            limit = 2 if self.compatible_job(host_id, project, scene) else 1
            if counts.get(key, 0) < limit and (not counts.get(key) or
                    self.jobs.installations.component_proven(host_id, ['wan_i2v'])):
                result.append(host_id)
                counts[key] = counts.get(key, 0) + 1
        return result

    def record_measurement(self, host_id, record):
        self._save(host_id, {'measurement': {k: record.get(k) for k in
                   ('telemetry', 'timing', 'runtime', 'config', 'capacity_fingerprint')}})

    def fault(self, host_id, job_id):
        self._save(host_id, {'fault': {'job_id': job_id, 'at': datetime.now(UTC).isoformat(),
                          'reason': 'Dừng gửi shot mới trên GPU; đối chiếu từng prompt trước khi tiếp tục.'}})

    async def clear_fault(self, host_id):
        with self.service.sessions() as session:
            rows = list(session.scalars(select(Job).where(Job.host_id.in_(self.siblings(host_id)))))
        if any(j.status in {'running', 'queued', 'cancelling'} for j in rows):
            raise ValueError('Chờ mọi tiến trình dừng ở ranh giới trước khi đối chiếu GPU.')
        terminal = []
        for lane in self.siblings(host_id):
            async with self.jobs.backend.connection(lane) as (_, client):
                r = await client.get('/queue')
                r.raise_for_status()
                q = r.json()
                if not all(isinstance(q.get(k), list) and not q[k] for k in ('queue_running', 'queue_pending')):
                    raise ValueError('Queue chưa xác minh rỗng; giữ khóa GPU.')
                for job in rows:
                    for stage, submission in job.result.get('submissions', {}).items():
                        if submission.get('host_id', job.host_id) != lane:
                            continue
                        if remote_settled(submission):
                            continue
                        if not submission.get('prompt_id'):
                            continue
                        r = await client.get('/history/' + submission['prompt_id'])
                        r.raise_for_status()
                        status = r.json().get(submission['prompt_id'], {}).get('status', {})
                        if status.get('status_str') != 'error':
                            raise ValueError('Prompt còn thiếu output hoặc history chưa rõ; đối chiếu job trước.')
                        terminal.append((job.id, stage, submission['prompt_id']))
        def still_stopped(session):
            if session.scalar(select(Job.id).where(Job.host_id.in_(self.siblings(host_id)),
                    Job.status.in_(['running', 'queued', 'cancelling']))):
                raise ValueError('Có job mới trong lúc đối chiếu; giữ khóa GPU.')
            for job_id, stage, prompt_id in terminal:
                row = session.get(Job, job_id)
                submissions = dict(row.result.get('submissions', {}))
                if submissions.get(stage, {}).get('prompt_id') != prompt_id:
                    raise ValueError('Checkpoint đã thay đổi; đọc lại trạng thái trước khi mở khóa.')
                submissions[stage] = {**submissions[stage], 'remote_terminal_state': 'error',
                    'remote_terminal_prompt_id': prompt_id, 'remote_terminal_checked_at': datetime.now(UTC).isoformat()}
                row.result = {**row.result, 'submissions': submissions}
                if row.status in {'reconciling', 'failed'}:
                    row.status = 'interrupted'  # explicit Resume is still needed; no automatic retry
            for row in session.scalars(select(Job).where(Job.host_id.in_(self.siblings(host_id)),
                    Job.status.in_(['reconciling', 'failed']))):
                if row.result.get('submissions') and result_settled(row.result):
                    row.status = 'interrupted'  # previously persisted terminal proofs also allow explicit Resume
        self._save(host_id, {'fault': None}, still_stopped)
        return self.state(host_id)

    def apply(self, host_id, config):
        request = GPUCostConfig.model_validate(config)
        for h in self.siblings(host_id):
            if not self.jobs.runtime.at_boundary(h):
                raise ValueError('Tạm dừng các tiến trình ở ranh giới shot và đối chiếu trước khi đổi hồ sơ.')
        evidence = None
        if request.model_residency != 'job' or request.wan_concurrency != 1:
            from studio.benchmark import compare_cost
            comparison = compare_cost(self.jobs, request.baseline_id, request.candidate_id)
            candidate = self.service.require(Job, request.candidate_id)
            if candidate.host_id not in self.siblings(host_id) or not request.quality_review_passed:
                raise ValueError('Cần xem phim mẫu và xác nhận chất lượng trên đúng GPU này.')
            report = candidate.result['benchmark_report']
            if report.get('policy') != {'model_residency': request.model_residency, 'wan_concurrency': request.wan_concurrency}:
                raise ValueError('Hồ sơ được chọn khác cấu hình benchmark.')
            if comparison['hardware_changed'] or not comparison['candidate_eligible']:
                raise ValueError('Benchmark chưa đạt điều kiện thời gian/thông lượng để bật hồ sơ.')
            capacity = measured_capacity(report)
            if request.wan_concurrency == 2 and not capacity['fits_two']:
                raise ValueError('Hai tiến trình chưa đủ 10% dự phòng mỗi tiến trình và 10% VRAM GPU.')
            sample = next(iter(report['shots'].values()))
            evidence = {**capacity, 'baseline_id': request.baseline_id, 'candidate_id': request.candidate_id,
                        'generation_version': report.get('generation_version', candidate.snapshot.get('generation_version', 2)),
                        'quality_review_passed': True, 'config': report['config'], 'runtime': sample['runtime'],
                        'workflow_hash': report['workflow_hash'], 'comparison': comparison}
        def at_boundary(session):
            for h in self.siblings(host_id):
                if not self.jobs.runtime.at_boundary(h):
                    raise ValueError('Có job mới; tạm dừng ở ranh giới shot trước khi lưu hồ sơ.')
            for run in session.scalars(select(ProductionRun).where(ProductionRun.status.in_(
                    ['running', 'pause_requested', 'reconciling']))):
                used = {run.snapshot.get('host_id'), *(run.consent.get('parallel_host_ids') or [])}
                if used.intersection(self.siblings(host_id)):
                    raise ValueError('Tạm dừng Production Run ở ranh giới shot trước khi đổi hồ sơ GPU.')
        self._save(host_id, {'config': {'model_residency': request.model_residency, 'wan_concurrency': request.wan_concurrency},
                             'evidence': evidence}, at_boundary)
        return self.state(host_id)
