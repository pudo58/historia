"""Durable, consent-scoped draft production on the existing serial job queue.

Snapshots are never edited. Checkpoints carry derived media separately. No remote
submission is retried automatically after an ambiguous failure.
"""
import logging
from copy import deepcopy

from sqlalchemy import select, text

from studio.media import probe
from studio.models import Job, ProductionRun, Scene
from studio.packs import load_graph
from studio.schemas import SceneInput
from studio.service import canonical_hash

LIVE = {'running', 'pause_requested', 'paused', 'duration_review', 'keyframe_review', 'reconciling', 'failed'}


def dependency_identity(project, scene, kind):
    from studio.formats import resolve_format
    from studio.generation import identity_scene
    if scene:
        scene = identity_scene(scene, kind)
    if kind == 'speech':
        return {'narration': scene['narration'], 'voice': project['voice'],
                'pronunciation': project.get('pronunciation', '')}
    if kind == 'rife':
        return {'profile': project.get('frame_interpolation'), 'clip_ids': scene.get('clip_ids')}
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
        if scene.get('shot_list'):
            value['scene']['shot_list'] = scene['shot_list']
        if kind == 'clip' and scene.get('video_profile'):
            value['scene']['video_profile'] = scene['video_profile']
        if kind == 'keyframe' and project.get('keyframe_profile', 'standard') != 'standard':
            value['keyframe_profile'] = project['keyframe_profile']
        if kind == 'clip':
            value.update(keyframe_id=scene.get('keyframe_id'), speech_id=scene.get('speech_id'), duration=scene.get('duration'))
            value['scene'].update({k: scene[k] for k in ('motion', 'image_strategy', 'shorten_last_shot') if k in scene})
        if kind == 'keyframe' and scene.get('image_strategy') == 'per_shot':
            value['scene']['image_strategy'] = 'per_shot'
        return value
    return {'project': project}


def active_ids(checkpoint):
    """Jobs the run is currently waiting on. `current_job_id` stays the first entry for old readers."""
    ids = list(checkpoint.get('active_job_ids') or [])
    current = checkpoint.get('current_job_id')
    if current and current not in ids:
        ids.insert(0, current)
    return ids


def set_active(checkpoint, ids):
    if ids:
        checkpoint['active_job_ids'] = list(ids)
        checkpoint['current_job_id'] = ids[0]
    else:
        checkpoint.pop('active_job_ids', None)
        checkpoint.pop('current_job_id', None)


def review_waiting(checkpoint):
    """Scenes whose shot keyframes are generated but not yet approved, in queue order.

    Older checkpoints only stored the single ``review_scene_id``.
    """
    queued = list(checkpoint.get('review_pending') or [])
    legacy = checkpoint.get('review_scene_id')
    if legacy and legacy not in queued:
        queued.insert(0, legacy)
    media = checkpoint.get('media') or {}
    return [s for s in queued if not media.get(s, {}).get('shot_keyframes_approved')]


def parallel_hosts(run):
    """Extra Pods that may render Wan clips of this run at the same time as the main Pod."""
    return [h for h in (run.consent or {}).get('parallel_host_ids') or [] if h != run.snapshot.get('host_id')]


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
        ids = active_ids(run.checkpoint)
        active = [self.service.require(Job, id) for id in ids]
        current = active[0] if active else None
        value['current_job_id'] = current.id if current else None
        value['current_scene_id'] = current.scene_id if current else None
        value['queue_status'] = current.status if current else None
        value['active_job_ids'] = ids
        value['active_scene_ids'] = [j.scene_id for j in active if j.scene_id]
        value['parallel_host_ids'] = parallel_hosts(run)
        value['progress'] = 100 if run.status == 'completed' else min(99, round(
            len(run.checkpoint.get('jobs', {})) * 100 /
            ((4 if run.snapshot.get('frame_interpolation') == 'rife24' else 3) * value['scene_count'] + 1)))
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
                live = [j for j in (session.get(Job, id) for id in active_ids(existing.checkpoint)) if j]
                if existing.status not in {'paused', 'duration_review', 'failed'} or any(
                        current.status in {'running', 'reconciling', 'cancelling'} or self.jobs.remote_pending(current.result)
                        for current in live):
                    raise ValueError('Dự án đã có lượt sản xuất. Tạm dừng/đối chiếu lượt hiện tại trước.')
                for current in live:
                    if current.status in {'queued', 'paused'}:
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
            needed = ['tts', 'qwen_image']
            if any(s.get('motion', 'wan') == 'wan' for s in project['scenes']):
                needed.append('wan_i2v')
            if any(s.get('reference_ids') or s.get('character_ids') for s in project['scenes']):
                needed.append('qwen_edit')
            if not self.jobs.installations.component_proven(host_id, needed):
                raise ValueError('Model cần dùng chưa sẵn sàng/kiểm chứng trên GPU đã chọn.')
            parallel = list(dict.fromkeys(request.parallel_host_ids))
            if host_id in parallel:
                raise ValueError('Pod chính của dự án đã được dùng; chỉ chọn thêm Pod khác để chạy song song.')
            if parallel and 'wan_i2v' not in needed:
                raise ValueError('Chạy song song chỉ tăng tốc cảnh Wan; dự án không có cảnh Wan nào.')
            for extra in parallel:
                host = self.jobs.hosts._require_host(extra)
                if not host.pinned_fingerprint:
                    raise ValueError(f'Pod song song {host.label}: xác nhận fingerprint SSH trước.')
                self.jobs.assert_no_abandoned_remote(extra)
                self.jobs.recipes.assert_recipes_idle(extra)
                if not self.jobs.installations.component_proven(extra, ['wan_i2v']):
                    raise ValueError(f'Pod song song {host.label}: Wan chưa cài/kiểm chứng trên máy này.')
            if (project.get('keyframe_profile') == 'lightning' and any(
                    not s.get('reference_ids') and not s.get('character_ids') for s in project['scenes']) and
                    not self.jobs.optional_ready(host_id, 'qwen-image-lightning-fp8')):
                raise ValueError('Cài LoRA Qwen-Image Lightning tùy chọn trước khi chạy cảnh tạo ảnh từ chữ.')
            if (project.get('frame_interpolation') == 'rife24' and
                    any(s.get('motion', 'wan') == 'wan' for s in project['scenes']) and
                    not self.jobs.optional_ready(host_id, 'rife-v4.26')):
                raise ValueError('Cài model RIFE 4.26 tùy chọn trước khi bắt đầu lượt final.')
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
                snapshot=snapshot, consent={**request.model_dump(), 'parallel_host_ids': parallel},
                status='running', stage='speech',
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
            if run.status == 'keyframe_review':
                raise ValueError('Duyệt toàn bộ ảnh riêng trước khi đổi trạng thái lượt sản xuất.')
            checkpoint = deepcopy(run.checkpoint)
            live = [j for j in (session.get(Job, id) for id in active_ids(checkpoint)) if j]
            if action == 'pause':
                run.status = 'pause_requested' if any(j.status == 'running' for j in live) else 'paused'
                for job in live:
                    if job.status == 'queued':
                        job.status = 'paused'
                    if job.kind in {'clip', 'rife'} and job.status == 'running':
                        job.result = {**job.result, 'pause_requested': True}
            elif action == 'accept-duration':
                if run.status != 'duration_review':
                    raise ValueError('Chưa có thời lượng đo cần xác nhận.')
                checkpoint['duration_accepted'] = True
                run.status = 'running'
            elif action == 'resume':
                if run.status == 'keyframe_review':
                    raise ValueError('Duyệt toàn bộ ảnh riêng trước khi tiếp tục Wan.')
                if run.status == 'duration_review':
                    raise ValueError('Chấp nhận thời lượng đo hoặc sửa lời đọc trước.')
                from ghm.models import Host
                # Pod migration: rebind live project GPU onto frozen snapshot/job so
                # resume can continue after the old host was deleted/replaced.
                live_host = self.service.project(run.project_id).get('host_id')
                old_host = run.snapshot.get('host_id')
                migrated = bool(live_host and live_host != old_host)
                if migrated:
                    snap = deepcopy(run.snapshot)
                    snap['host_id'] = live_host
                    run.snapshot = snap
                # A deleted parallel Pod is dropped; its unfinished scene moves to the main Pod.
                kept = [h for h in parallel_hosts(run) if session.get(Host, h) is not None and h != live_host]
                if kept != parallel_hosts(run):
                    run.consent = {**run.consent, 'parallel_host_ids': kept}
                for job in live:
                    gone = bool(job.host_id) and session.get(Host, job.host_id) is None
                    if live_host and job.host_id and ((migrated and job.host_id == old_host) or gone):
                        job_snap = deepcopy(job.snapshot)
                        project_snap = dict(job_snap.get('project') or {})
                        project_snap['host_id'] = live_host
                        job_snap['project'] = project_snap
                        job.snapshot = job_snap
                        job.host_id = live_host
                    elif not migrated and live_host and not job.host_id and job.kind not in {'export'} and not (
                            job.kind in {'clip', 'rife'} and job.snapshot.get('scene', {}).get('motion', 'wan') != 'wan'):
                        job.host_id = live_host
                for job in live:
                    if job.status not in {'paused', 'interrupted', 'reconciling', 'failed', 'cancelled'}:
                        continue
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

    def approve_keyframes(self, id, scene_ids):
        """Approve one or several scenes' shot keyframes; all-or-nothing."""
        scene_ids = [scene_ids] if isinstance(scene_ids, str) else list(scene_ids)
        with self.service.sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            run = session.get(ProductionRun, id)
            if not run:
                raise KeyError(id)
            cp = deepcopy(run.checkpoint)
            waiting = review_waiting(cp)
            # Early approval is allowed while other scenes' keyframes are still generating.
            if run.status not in {'keyframe_review', 'running', 'pause_requested', 'paused'} or not scene_ids or any(
                    scene_id not in waiting for scene_id in scene_ids):
                raise ValueError('Lượt sản xuất không chờ duyệt ảnh của cảnh này.')
            from studio.media import scene_clip_count
            for scene_id in scene_ids:
                scene = next((s for s in run.snapshot['scenes'] if s['id'] == scene_id), None)
                media = cp.get('media', {}).get(scene_id, {})
                selected = media.get('shot_keyframes') or []
                duration = probe(self.service.artifact_path(media['speech_id']))['duration'] if media.get('speech_id') else 0
                if (not scene or len(selected) != (scene_clip_count(scene, duration) if scene.get('image_strategy') == 'per_shot' else 1) or
                        not all(self.jobs.artifact_valid(a) for a in selected)):
                    raise ValueError('Thiếu ảnh riêng hợp lệ; không chạy Wan.')
                cp['media'][scene_id] = {**media, 'keyframe_approved': True,
                                         'shot_keyframes_approved': True}
            left = [s for s in waiting if s not in scene_ids]
            cp['review_pending'] = left
            if left:
                cp['review_scene_id'] = left[0]
            else:
                cp.pop('review_scene_id', None)
            if run.status == 'keyframe_review' and not left:
                run.status = 'running'
            run.checkpoint = cp
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
                    if run is None or run.status == 'abandoned':
                        continue
                    run.status, run.error = 'failed', str(exc)[:2000]
                    session.commit()
            except Exception:  # transient (e.g. DB lock): log, keep run state, retry next tick
                logging.getLogger(__name__).exception('Production run %s advance failed', id)

    def valid_output(self, kind, output):
        ids = output.get('clip_ids', []) if kind == 'clip' else output.get('rife_clip_ids', []) if kind == 'rife' else output.get('artifact_ids', []) if kind == 'export' else [output.get('speech_id' if kind == 'speech' else 'keyframe_id')]
        if kind == 'keyframe' and output.get('shot_keyframes'):
            ids = [*ids, *output['shot_keyframes']]
        return bool(ids) and all(self.jobs.artifact_valid(id) for id in ids)

    def advance(self, id):
        with self.service.sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            run = session.get(ProductionRun, id)
            if run.status not in {'running', 'pause_requested'}:
                return
            cp = deepcopy(run.checkpoint)
            remaining = []
            for current in (session.get(Job, id) for id in active_ids(cp)):
                if current is None:
                    continue
                if current.status != 'completed':
                    remaining.append(current)
                    continue
                output = current.result if current.kind == 'export' else current.result.get('production_output', {})
                if not self.valid_output(current.kind, output):
                    raise ValueError('Checkpoint thiếu file hoặc checksum không hợp lệ; không tự chạy lại GPU.')
                if current.kind != 'export':
                    cp['media'][current.scene_id] = {**cp['media'].get(current.scene_id, {}), **output}
                    if (current.kind == 'keyframe' and (current.snapshot['scene'].get('image_strategy') == 'per_shot' or current.snapshot['scene'].get('shot_list'))
                            and not cp['media'][current.scene_id].get('shot_keyframes_approved')):
                        # Queue for review but keep generating other scenes' keyframes:
                        # the GPU no longer idles while one scene waits for a person.
                        # Wan is held back until every queued scene is approved.
                        waiting = cp.setdefault('review_pending', [])
                        if current.scene_id not in waiting:
                            waiting.append(current.scene_id)
                else:
                    cp['artifact_ids'] = output['artifact_ids']
                    run.status, run.stage = 'completed', 'completed'
            set_active(cp, [j.id for j in remaining])
            working = [j for j in remaining if j.status in {'queued', 'running', 'cancelling'}]
            stuck = [j for j in remaining if j.status in {'failed', 'cancelled', 'reconciling', 'interrupted', 'paused'}]
            if stuck and not working and run.status in {'running', 'pause_requested'}:
                statuses = {j.status for j in stuck}
                run.status = ('reconciling' if statuses & {'reconciling', 'interrupted'} else
                              'failed' if statuses & {'failed', 'cancelled'} else 'paused')
                run.error = next((j.error for j in stuck if j.error), None)
            if run.status == 'pause_requested' and not working:
                run.status = 'paused'
            if run.status != 'running' or len(working) != len(remaining):
                # Paused/failed/uncertain work stops new dispatch; Pods still rendering finish first.
                run.checkpoint = cp
                session.commit()
                return
            project = deepcopy(run.snapshot)
            for scene in project['scenes']:
                scene.update(cp['media'].get(scene['id'], {}))
            from ghm.models import Host
            hosts = [project.get('host_id'), *(h for h in parallel_hosts(run) if session.get(Host, h) is not None)]
            used = {j.host_id for j in remaining}
            dispatched = [j.id for j in remaining]
            stages = ('speech', 'keyframe', 'clip', 'rife', 'export') if project.get('frame_interpolation') == 'rife24' else ('speech', 'keyframe', 'clip', 'export')
            for kind in stages:
                if kind == 'clip':
                    waiting = review_waiting(cp)
                    if waiting:
                        # All keyframes are done; review them together before any Wan clip.
                        cp['review_pending'], cp['review_scene_id'] = waiting, waiting[0]
                        run.status, run.stage = 'keyframe_review', 'keyframe'
                        break
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
                # Only Wan clips fan out across Pods; every other stage stays one job at a time.
                pending = any(j.kind == kind for j in remaining)
                for scene in scenes:
                    key = kind + ':' + (scene['id'] if scene else 'film')
                    if key in cp['jobs']:
                        continue
                    local_clip = kind in {'clip', 'rife'} and scene.get('motion', 'wan') != 'wan'
                    if kind != 'clip' and dispatched:
                        pending = True
                        break
                    if kind == 'clip':
                        lane = None if local_clip else next((h for h in hosts if h not in used), False)
                        if lane is False or (local_clip and None in used):
                            pending = True
                            continue
                    if kind == 'clip':
                        self.service.artifact_path(scene['keyframe_id'])
                    if kind == 'export':
                        for s in project['scenes']:
                            duration = probe(self.service.artifact_path(s['speech_id']))['duration']
                            source_ids = s.get('rife_clip_ids', []) if project.get('frame_interpolation') == 'rife24' else s.get('clip_ids', [])
                            available = sum(probe(self.service.artifact_path(a))['duration'] for a in source_ids)
                            if duration <= 0 or available + .05 < duration:
                                raise ValueError('Clip chưa đủ thời lượng lời đọc; không kéo chậm/lặp clip.')
                    workflows = {name: canonical_hash(load_graph(name)) for name in ('qwen_image', 'qwen_edit', 'wan_i2v', 'rife_post')}
                    workflow = {} if kind in {'speech', 'export'} or (kind in {'clip', 'rife'} and scene.get('motion', 'wan') != 'wan') else ({'wan_i2v': workflows['wan_i2v']} if kind == 'clip' else {'rife_post': workflows['rife_post']} if kind == 'rife' else {k: v for k, v in workflows.items() if k in {'qwen_image', 'qwen_edit'}})
                    clip_host = None if local_clip else lane if kind == 'clip' else project.get('host_id')
                    identity = {'production_version': 1, 'kind': kind, 'project_id': run.project_id,
                        'scene_id': scene['id'] if scene else None,
                        'inputs': dependency_identity(project, scene, kind), 'workflow': workflow}
                    hashed = canonical_hash({**identity, 'host': clip_host if kind != 'export' else None})
                    # A clip already finished on any Pod of this run is reused, not re-rendered.
                    candidates = [hashed, *(canonical_hash({**identity, 'host': h}) for h in hosts
                                            if kind == 'clip' and not local_clip and h != clip_host)]
                    existing = session.scalar(select(Job).where(Job.input_hash.in_(candidates), Job.status == 'completed').order_by(Job.created_at.desc()))
                    from studio.generation import clip_output_matches
                    if existing and kind == 'clip' and not clip_output_matches(existing, project, scene):
                        existing = None
                    output = (existing.result if kind == 'export' else existing.result.get('production_output', {})) if existing else {}
                    if not existing and scene and kind not in {'export', 'rife'}:
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
                            host_id=clip_host if kind != 'export' else None, kind=kind, input_hash=hashed,
                            snapshot={'project': snap_project, 'scene': deepcopy(scene), 'production_run_id': run.id,
                                      'generation_version': 3,
                                      'draft_export_authorized': True, 'request': {'kind': kind}, 'workflow_hashes': workflows})
                        session.add(job)
                        session.flush()
                        if kind == 'clip' and cp.get('pending_clip_config'):
                            self.jobs.set_pending_clip_config(job, cp['pending_clip_config'])
                    cp['jobs'][key] = job.id
                    dispatched.append(job.id)
                    pending = True
                    if job.status != 'completed':
                        used.add(job.host_id)
                    if kind != 'clip':
                        break
                if pending:
                    break
            set_active(cp, dispatched)
            run.checkpoint = cp
            session.commit()
