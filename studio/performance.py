"""Read-only production preview using actual scene audio and durable shot records."""
from copy import deepcopy

from sqlalchemy import select

from studio.generation import clip_config, shot_config
from studio.media import probe, shot_count
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
            count = shot_count(duration) if duration else None
            default_override = run.checkpoint.get('pending_clip_config') if run and not job else None
            default = clip_config(project, effective_scene, default_override)
            submissions = job.result.get('submissions', {}) if job else {}
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
                config = shot_config(job, stage) if job else default
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
        return {**result, 'run_id': run.id if run else None, 'scenes': scenes,
                'measured_audio_seconds': measured, 'shots': records,
                'unmeasured_scenes': sum(s['shot_count'] is None for s in scenes)}
