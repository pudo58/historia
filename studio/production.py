"""Durable, consent-scoped draft production on the existing serial job queue.

Snapshots are never edited. Checkpoints carry derived media separately. No remote
submission is retried automatically after an ambiguous failure.
"""
from copy import deepcopy

from sqlalchemy import select, text

from studio.media import probe
from studio.models import Job, ProductionRun, Scene
from studio.packs import load_graph
from studio.schemas import SceneInput
from studio.service import canonical_hash

LIVE = {'running', 'pause_requested', 'paused', 'duration_review', 'reconciling', 'failed'}


def dependency_identity(project, scene, kind):
    from studio.formats import resolve_format
    from studio.generation import identity_scene
    if scene:
        scene = identity_scene(scene, kind)
    if kind == 'speech':
        return {'narration': scene['narration'], 'voice': project['voice'],
                'pronunciation': project.get('pronunciation', '')}
    if kind in {'keyframe', 'clip'}:
        character_ids = scene.get('character_ids', [])
        characters = [c for c in project['characters'] if c['id'] in character_ids]
        refs = set(scene.get('reference_ids', []))
        for character in characters:
            refs.update(character.get('reference_ids', []))
        value = {'scene': {k: scene.get(k) for k in ('visual_prompt', 'camera', 'seed', 'steps')},
                 'settings': {k: project.get(k) for k in ('style', 'era', 'location', 'aspect_ratio')},
                 'render_size': resolve_format(project.get('quality', 'draft'), project)['render_size'],
                 'render_profile': project.get('render_profile') or ('standard' if project.get('quality') == 'final' else 'draft'),
                 'characters': characters, 'references': [s for s in project['sources'] if s['id'] in refs]}
        if kind == 'clip':
            value.update(keyframe_id=scene.get('keyframe_id'), speech_id=scene.get('speech_id'), duration=scene.get('duration'))
        return value
    return {'project': project}


class ProductionRuns:
    def __init__(self, jobs):
        self.jobs, self.service = jobs, jobs.service

    def list(self, project_id):
        self.service.project(project_id)
        with self.service.sessions() as session:
            return [self.view(r) for r in session.scalars(select(ProductionRun).where(
                ProductionRun.project_id == project_id).order_by(ProductionRun.created_at.desc()))]

    def view(self, run):
        value = self.service.read(run)
        value['job_ids'] = list(run.checkpoint.get('jobs', {}).values())
        value['measured_audio_seconds'] = run.checkpoint.get('measured_audio_seconds', 0)
        value['target_duration_seconds'] = run.snapshot['duration_seconds']
        value['duration_review_required'] = run.status == 'duration_review'
        media = run.checkpoint.get('media', {})
        value['scene_count'] = len(run.snapshot['scenes'])
        value['completed_scene_count'] = sum(bool(m.get('clip_ids')) for m in media.values())
        current_id = run.checkpoint.get('current_job_id')
        current = self.service.require(Job, current_id) if current_id else None
        value['current_job_id'] = current_id
        value['current_scene_id'] = current.scene_id if current else None
        value['queue_status'] = current.status if current else None
        value['progress'] = 100 if run.status == 'completed' else min(99, round(
            len(run.checkpoint.get('jobs', {})) * 100 / (3 * value['scene_count'] + 1)))
        value['billing_notice'] = 'Tạm dừng sản xuất không dừng tiền thuê GPU.'
        return value

    def get(self, id):
        return self.view(self.service.require(ProductionRun, id))

    def create(self, project_id, request):
        if not request.script_approved or not request.gpu_consent:
            raise ValueError('Cần duyệt kịch bản và xác nhận sử dụng GPU có phí.')
        with self.service.sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            existing = session.scalar(select(ProductionRun).where(ProductionRun.project_id == project_id,
                ProductionRun.idempotency_key == request.idempotency_key))
            if existing:
                return self.view(existing)
            existing = session.scalar(select(ProductionRun).where(ProductionRun.project_id == project_id,
                ProductionRun.status.in_(LIVE)))
            if existing:
                current_id = existing.checkpoint.get('current_job_id')
                current = session.get(Job, current_id) if current_id else None
                if existing.status not in {'paused', 'duration_review', 'failed'} or (current and
                        (current.status in {'running', 'reconciling', 'cancelling'} or self.jobs.remote_pending(current.result))):
                    raise ValueError('Dự án đã có lượt sản xuất. Tạm dừng/đối chiếu lượt hiện tại trước.')
                if current and current.status in {'queued', 'paused'}:
                    current.status = 'cancelled'
                    session.flush()
                existing.status = 'superseded'
            project = self.service.project(project_id)
            if not project['scenes']:
                raise ValueError('Chưa có cảnh để sản xuất.')
            if project.get('upscale_method') == 'ai':
                raise ValueError('AI upscale chưa được benchmark; chọn lanczos.')
            if {s['id']: s['revision'] for s in project['scenes']} != request.scene_revisions:
                raise ValueError('Kịch bản đã thay đổi. Tải lại và duyệt đúng revision.')
            if session.scalar(select(Job.id).where(Job.project_id == project_id,
                    Job.status.in_(['queued', 'running', 'cancelling', 'reconciling', 'paused']))):
                raise ValueError('Dự án có tác vụ đang chạy/chờ. Đối chiếu trước khi sản xuất.')
            host_id = project.get('host_id')
            if not host_id or not self.jobs.hosts._require_host(host_id).pinned_fingerprint:
                raise ValueError('Chọn GPU và xác nhận fingerprint SSH trước.')
            self.jobs.assert_no_abandoned_remote(host_id)
            self.jobs.recipes.assert_recipes_idle(host_id)
            needed = ['tts', 'qwen_image', 'wan_i2v']
            if any(s.get('reference_ids') or s.get('character_ids') for s in project['scenes']):
                needed.append('qwen_edit')
            if not self.jobs.installations.component_proven(host_id, needed):
                raise ValueError('Model cần dùng chưa sẵn sàng/kiểm chứng trên GPU đã chọn.')
            for scene in project['scenes']:
                self.service.validate_scene(project_id, SceneInput.model_validate({k: scene[k] for k in SceneInput.model_fields if k in scene}))
                if not scene['narration'].strip() or not scene['visual_prompt'].strip():
                    raise ValueError('Mỗi cảnh cần lời đọc và mô tả hình.')
                if not scene.get('citations') and not request.unsourced_consent:
                    raise ValueError('Cảnh chưa có nguồn: cần xác nhận riêng nội dung chưa kiểm chứng.')
                refs = set(scene.get('reference_ids', []))
                for character in project['characters']:
                    if character['id'] in scene.get('character_ids', []):
                        refs.update(character.get('reference_ids', []))
                if len(refs) > 3:
                    raise ValueError('Tối đa 3 ảnh tham chiếu cho một cảnh.')
                for ref in refs:
                    source = next(s for s in project['sources'] if s['id'] == ref)
                    self.service.artifact_path(source['artifact_id'])
                # The one explicit approval applies to this exact script revision.
                row = session.get(Scene, scene['id'])
                row.data = {**row.data, 'script_approved': True}
                scene['script_approved'] = True
            snapshot = deepcopy(project)
            from studio.tts_device import new_tts_device
            snapshot["tts_device"] = new_tts_device(project)
            run = ProductionRun(project_id=project_id, idempotency_key=request.idempotency_key,
                snapshot=snapshot, consent=request.model_dump(), status='running', stage='speech',
                checkpoint={'jobs': {}, 'media': {}, 'duration_accepted': False})
            session.add(run)
            session.commit()
        return self.get(run.id)

    def action(self, id, action):
        with self.service.sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            run = session.get(ProductionRun, id)
            if not run:
                raise KeyError(id)
            if run.status == 'abandoned':
                raise ValueError('Lượt đã bỏ không thể tiếp tục; chọn GPU và xác nhận lượt mới riêng.')
            if run.status in {'completed', 'superseded'}:
                return self.view(run)
            checkpoint = deepcopy(run.checkpoint)
            job = session.get(Job, checkpoint.get('current_job_id')) if checkpoint.get('current_job_id') else None
            if action == 'pause':
                run.status = 'pause_requested' if job and job.status == 'running' else 'paused'
                if job and job.status == 'queued':
                    job.status = 'paused'
                if job and job.kind == 'clip' and job.status == 'running':
                    job.result = {**job.result, 'pause_requested': True}
            elif action == 'accept-duration':
                if run.status != 'duration_review':
                    raise ValueError('Chưa có thời lượng đo cần xác nhận.')
                checkpoint['duration_accepted'] = True
                run.status = 'running'
            elif action == 'resume':
                if run.status == 'duration_review':
                    raise ValueError('Chấp nhận thời lượng đo hoặc sửa lời đọc trước.')
                # Pod migration: rebind live project GPU onto frozen snapshot/job so
                # resume can continue after the old host was deleted/replaced.
                live_host = self.service.project(run.project_id).get('host_id')
                if live_host and live_host != run.snapshot.get('host_id'):
                    snap = deepcopy(run.snapshot)
                    snap['host_id'] = live_host
                    run.snapshot = snap
                    if job:
                        job_snap = deepcopy(job.snapshot)
                        project_snap = dict(job_snap.get('project') or {})
                        project_snap['host_id'] = live_host
                        job_snap['project'] = project_snap
                        job.snapshot = job_snap
                        job.host_id = live_host
                elif job and live_host and not job.host_id:
                    job.host_id = live_host
                if job and job.status in {'paused', 'interrupted', 'reconciling', 'failed', 'cancelled'}:
                    if job.status == 'failed':
                        retries = job.result.get('production_retry_count', 0)
                        if retries >= 3:
                            raise ValueError('Đã thử lại bước lỗi 3 lần; sửa đầu vào và tạo revision mới.')
                        job.result = {**job.result, 'production_retry_count': retries + 1}
                    # Preserve prompt IDs, downloaded shots and speech checkpoints.
                    # Explicit retries only; never reset an uncertain submission.
                    # cancelled is included: pause/abandon of a dead pod leaves the
                    # current step cancelled, and UI retry must requeue it.
                    job.status, job.error = 'queued', None
                    job.result = {**job.result, 'pause_requested': False}
                run.status = 'running'
            run.error = None
            run.checkpoint = checkpoint
            session.commit()
        return self.get(id)

    def tick(self):
        with self.service.sessions() as session:
            ids = list(session.scalars(select(ProductionRun.id).where(ProductionRun.status.in_(['running', 'pause_requested']))))
        for id in ids:
            try:
                self.advance(id)
            except (ValueError, KeyError) as exc:
                with self.service.sessions() as session:
                    session.execute(text('BEGIN IMMEDIATE'))
                    run = session.get(ProductionRun, id)
                    if run.status == 'abandoned':
                        continue
                    run.status, run.error = 'failed', str(exc)[:2000]
                    session.commit()

    def valid_output(self, kind, output):
        ids = output.get('clip_ids', []) if kind == 'clip' else output.get('artifact_ids', []) if kind == 'export' else [output.get('speech_id' if kind == 'speech' else 'keyframe_id')]
        return bool(ids) and all(self.jobs.artifact_valid(id) for id in ids)

    def advance(self, id):
        with self.service.sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            run = session.get(ProductionRun, id)
            if run.status not in {'running', 'pause_requested'}:
                return
            cp = deepcopy(run.checkpoint)
            current = session.get(Job, cp.get('current_job_id')) if cp.get('current_job_id') else None
            if current:
                if current.status != 'completed':
                    if current.status in {'failed', 'cancelled', 'reconciling', 'interrupted', 'paused'}:
                        run.status = 'paused' if current.status == 'paused' else 'reconciling' if current.status in {'reconciling', 'interrupted'} else 'failed'
                        run.error = current.error
                    session.commit()
                    return
                output = current.result if current.kind == 'export' else current.result.get('production_output', {})
                if not self.valid_output(current.kind, output):
                    raise ValueError('Checkpoint thiếu file hoặc checksum không hợp lệ; không tự chạy lại GPU.')
                if current.kind != 'export':
                    cp['media'][current.scene_id] = {**cp['media'].get(current.scene_id, {}), **output}
                else:
                    cp['artifact_ids'] = output['artifact_ids']
                    run.status, run.stage = 'completed', 'completed'
                cp.pop('current_job_id', None)
            if run.status == 'pause_requested':
                run.status = 'paused'
            if run.status != 'running':
                run.checkpoint = cp
                session.commit()
                return
            project = deepcopy(run.snapshot)
            for scene in project['scenes']:
                scene.update(cp['media'].get(scene['id'], {}))
            for kind in ('speech', 'keyframe', 'clip', 'export'):
                run.stage = kind
                if kind == 'keyframe':
                    duration = sum(probe(self.service.artifact_path(s['speech_id']))['duration'] for s in project['scenes'])
                    if duration <= 0:
                        raise ValueError('Giọng đọc không có thời lượng hợp lệ.')
                    cp['measured_audio_seconds'] = duration
                    target = project['duration_seconds']
                    if not cp.get('duration_accepted') and abs(duration-target) > max(2, target*.1):
                        run.status, run.stage = 'duration_review', 'duration_review'
                        break
                scenes = [None] if kind == 'export' else project['scenes']
                pending = False
                for scene in scenes:
                    key = kind + ':' + (scene['id'] if scene else 'film')
                    if key in cp['jobs']:
                        continue
                    if kind == 'clip':
                        self.service.artifact_path(scene['keyframe_id'])
                    if kind == 'export':
                        for s in project['scenes']:
                            duration = probe(self.service.artifact_path(s['speech_id']))['duration']
                            available = sum(probe(self.service.artifact_path(a))['duration'] for a in s.get('clip_ids', []))
                            if duration <= 0 or available + .05 < duration:
                                raise ValueError('Clip chưa đủ thời lượng lời đọc; không kéo chậm/lặp clip.')
                    workflows = {name: canonical_hash(load_graph(name)) for name in ('qwen_image', 'qwen_edit', 'wan_i2v')}
                    workflow = {} if kind in {'speech', 'export'} else ({'wan_i2v': workflows['wan_i2v']} if kind == 'clip' else {k: v for k, v in workflows.items() if k != 'wan_i2v'})
                    hashed = canonical_hash({'production_version': 1, 'kind': kind, 'project_id': run.project_id,
                        'scene_id': scene['id'] if scene else None, 'host': project.get('host_id') if kind != 'export' else None,
                        'inputs': dependency_identity(project, scene, kind), 'workflow': workflow})
                    existing = session.scalar(select(Job).where(Job.input_hash == hashed, Job.status == 'completed').order_by(Job.created_at.desc()))
                    from studio.generation import clip_output_matches
                    if existing and kind == 'clip' and not clip_output_matches(existing, project, scene):
                        existing = None
                    output = (existing.result if kind == 'export' else existing.result.get('production_output', {})) if existing else {}
                    if not existing and scene and kind != 'export':
                        # Reuse legacy media only with a completed job snapshot proving
                        # the exact dependency identity, never merely an existing file.
                        from studio.models import Artifact
                        ids = scene.get('clip_ids', []) if kind == 'clip' else [scene.get('speech_id' if kind == 'speech' else 'keyframe_id')]
                        artifact = session.get(Artifact, ids[0]) if ids and ids[0] else None
                        legacy = session.get(Job, artifact.job_id) if artifact and artifact.job_id else None
                        if legacy and legacy.status == 'completed' and legacy.kind == kind and legacy.snapshot.get('scene') and (kind != 'clip' or clip_output_matches(legacy, project, scene)) and dependency_identity(
                                legacy.snapshot['project'], legacy.snapshot['scene'], kind) == dependency_identity(project, scene, kind):
                            output = ({'speech_id': scene['speech_id'], 'duration': scene.get('duration', 0), 'shot_count': scene.get('shot_count', 0)} if kind == 'speech' else
                                      {'keyframe_id': scene['keyframe_id'], 'keyframe_approved': False} if kind == 'keyframe' else
                                      {'clip_ids': ids, 'clip_approved': False})
                            if self.valid_output(kind, output):
                                existing = legacy
                                legacy.result = {**legacy.result, 'production_output': output}
                    if existing and self.valid_output(kind, output):
                        job = existing
                    else:
                        snap_project = deepcopy(project)
                        if kind == 'speech':
                            from studio.tts_device import snapshot_tts_device
                            snap_project['tts_device'] = snapshot_tts_device(project)
                        job = Job(project_id=run.project_id, scene_id=scene['id'] if scene else None,
                            host_id=project.get('host_id') if kind != 'export' else None, kind=kind, input_hash=hashed,
                            snapshot={'project': snap_project, 'scene': deepcopy(scene), 'production_run_id': run.id,
                                      'generation_version': 2,
                                      'draft_export_authorized': True, 'request': {'kind': kind}, 'workflow_hashes': workflows})
                        session.add(job)
                        session.flush()
                        if kind == 'clip' and cp.get('pending_clip_config'):
                            self.jobs.set_pending_clip_config(job, cp['pending_clip_config'])
                    cp['jobs'][key] = job.id
                    cp['current_job_id'] = job.id
                    pending = True
                    break
                if pending:
                    break
            run.checkpoint = cp
            session.commit()
