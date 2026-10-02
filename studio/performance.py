"""Read-only production preview using actual scene audio and durable shot records."""
from copy import deepcopy

from sqlalchemy import select

from studio.generation import clip_config, shot_config
from studio.media import probe, scene_clip_count
from studio.metrics import estimate
from studio.models import Artifact, Job, ProductionRun
from studio.production import dependency_identity


def performance(jobs, project_id, run_id=None):
    service = jobs.service
    project = service.project(project_id)
    with service.sessions() as session:
        run = session.scalar(select(ProductionRun).where(ProductionRun.project_id == project_id,
            ProductionRun.status.notin_(['abandoned', 'superseded', 'completed'])).order_by(ProductionRun.created_at.desc()))
        if run_id:
            run = service.require(ProductionRun, run_id)
            if run.project_id != project_id:
                raise ValueError('Lượt sản xuất không thuộc dự án.')
        if run:
            project = deepcopy(run.snapshot)
            for scene in project['scenes']:
                scene.update(run.checkpoint.get('media', {}).get(scene['id'], {}))
            selected = [session.get(Job, id) for id in run.checkpoint.get('jobs', {}).values()]
        else:
            selected = list(session.scalars(select(Job).where(Job.project_id == project_id,
                Job.kind == 'clip', Job.status != 'abandoned').order_by(Job.created_at.desc())))
        clips = {}
        live_scenes = {s['id']: s for s in project['scenes']}
        for job in selected:
            if job and job.kind == 'clip':
                scene = live_scenes.get(job.scene_id)
                if not run and (not scene or job.result.get('stale') or dependency_identity(
                        project, scene, 'clip') != dependency_identity(
                            job.snapshot['project'], job.snapshot['scene'], 'clip')):
                    continue
                clips.setdefault(job.scene_id, job)
        latest_runtime = {}
        for job in sorted(clips.values(), key=lambda j: j.updated_at, reverse=True):
            for item in reversed(list(job.result.get('submissions', {}).values())):
                if item.get('runtime'):
                    latest_runtime.setdefault(job.host_id, item['runtime'])
        records, pending, scenes = [], [], []
        measured = 0.0
        for scene in project['scenes']:
            job = clips.get(scene['id'])
            effective_scene = job.snapshot['scene'] if job else scene
            audio = effective_scene.get('speech_id')
            duration = probe(service.artifact_path(audio))['duration'] if audio else None
            if duration:
                measured += duration
            count = scene_clip_count(effective_scene, duration) if duration else None
            default_override = run.checkpoint.get('pending_clip_config') if run and not job else None
            default = clip_config(project, effective_scene, default_override)
            submissions = ({**job.result.get('submissions', {}), **job.result.get('local_shots', {})}
                           if job else {})
            host_id = job.host_id if job else project.get('host_id')
            runtime = latest_runtime.get(host_id, {})
            # After an explicit runtime change, wait for a sample from that runtime.
            if host_id:
                import json
                saved = json.loads(jobs.hosts.setting('studio_runtime:' + host_id) or '{}').get('config')
                if saved and any(runtime.get(k) != v for k, v in saved.items()):
                    runtime = {}
            artifacts = {a.name: a for a in session.scalars(select(Artifact).where(Artifact.job_id == job.id))} if job else {}
            done = 0
            configurations = []
            shots = []
            for index in range(count or 0):
                stage = f'clip-{index}'
                config = shot_config(job, stage, duration) if job else clip_config(
                    project, effective_scene, default_override, index=index, duration=duration)
                if effective_scene.get('motion', 'wan') != 'wan':
                    import math
                    frames = math.ceil(duration * 24)
                    config = {**config, 'frames': frames, 'fps': 24, 'steps': 0,
                              'shot_seconds': frames / 24, 'motion': effective_scene['motion']}
                if config not in configurations:
                    configurations.append(config)
                artifact = artifacts.get(stage + '.mp4')
                item = submissions.get(stage)
                shots.append({'index': index, 'stage': stage, 'job_id': job.id if job else None,
                              'artifact_id': artifact.id if artifact else None,
                              'state': 'downloaded' if artifact else (item or {}).get('state', 'pending'),
                              'config': config, 'timing': (item or {}).get('timing', {})})
                if artifact:
                    done += 1
                    # Count legacy artifacts without fabricating timing samples.
                    records.append(item or {'state': 'downloaded'})
                else:
                    pending.append((config, runtime))
            scenes.append({'scene_id': scene['id'], 'title': scene['title'], 'audio_seconds': duration,
                           'shot_count': count, 'completed_shots': done,
                           'keyframe_id': effective_scene.get('keyframe_id'), 'shots': shots,
                           'job_id': job.id if job else None, 'status': job.status if job else 'pending',
                           'configs': configurations or [default]})
        result = estimate(records, pending, project.get('hourly_usd'))
        if any(s['shot_count'] is None for s in scenes):
            result['eta_seconds'] = result['estimated_remaining_usd'] = None
        # Wall-clock estimate when several Pods render different scenes at once. Processing
        # cost is unchanged (same GPU-seconds); each extra Pod is billed for its own rental.
        from studio.gpu_memory import gpu_key
        from studio.production import parallel_hosts
        hosts = [h for h in [project.get('host_id'), *(parallel_hosts(run) if run else [])] if h]
        groups = {gpu_key(h, jobs.installations.lane(h)): h for h in hosts}
        gpus = max(1, len(groups))
        result['parallel_gpus'] = gpus
        result['wall_eta_seconds'] = result['eta_seconds'] / gpus if result['eta_seconds'] is not None else None
        profiles = [{'host_id': h, **jobs.cost.state(h)} for h in groups.values()]
        # A measured physical-GPU throughput already includes both lanes. Never
        # divide a sum of overlapping shot times and call it rented GPU-hours.
        dual = [p for p in profiles if p['config']['wan_concurrency'] == 2 and p.get('evidence') and not p.get('fault')]
        if pending and len(dual) == len(profiles) and profiles:
            compatible = all(all(c == p['evidence']['config'] and
                all(r.get(k) == p['evidence']['runtime'].get(k) for k in ('comfy_version', 'torch_version', 'cuda_version', 'attention_backend', 'memory_policy'))
                for p in dual) for c, r in pending)
            if compatible:
                reports = [service.require(Job, p['evidence']['candidate_id']).result['benchmark_report'] for p in dual]
                throughput = sum(r['clips_per_gpu_hour'] for r in reports)
                result['wall_eta_seconds'] = len(pending) / throughput * 3600
                result['eta_seconds'] = result['wall_eta_seconds']
                result['estimated_remaining_usd'] = result['wall_eta_seconds'] / 3600 * project['hourly_usd'] * gpus if project.get('hourly_usd') is not None else None
                result['eta_basis'] = 'accepted_physical_gpu_throughput'
        telemetry = [r.get('telemetry', {}) for r in records]
        result['process_peak_vram_bytes'] = max((t['process_peak_vram_bytes'] for t in telemetry if t.get('process_peak_vram_bytes') is not None), default=None)
        result['host_peak_ram_bytes'] = max((t['host_peak_ram_bytes'] for t in telemetry if t.get('host_peak_ram_bytes') is not None), default=None)
        result['gpu_profiles'] = profiles
        return {**result, 'run_id': run.id if run else None, 'scenes': scenes,
                'measured_audio_seconds': measured, 'shots': records,
                'unmeasured_scenes': sum(s['shot_count'] is None for s in scenes)}
