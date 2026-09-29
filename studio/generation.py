"""Resolved generation settings; legacy snapshots retain their original meaning."""
import math

from studio.formats import resolve_format

LEGACY_WORKFLOW_HASHES = {
    'qwen_image': '2aecde2d6f0186957c70ac59a915b269e964bd99b35d17e1cf39a487b94c0ee4',
    'qwen_edit': '82a8231f891e89f2c8664d5de065da3eaff0192329a1e296532244c90f476025',
    'wan_i2v': '5d3332dff8d9bf2364d61cc5d8a28a47673ff9ec24b3378354c6de3f114ae79f',
}


def compatible_workflow_hashes(old, current):
    return bool(old) and set(old) == set(current) and all(
        value in (current[name], LEGACY_WORKFLOW_HASHES.get(name)) for name, value in old.items())


def stage_steps(scene, kind, workflow=None, project=None, generation_version=2):
    if kind == 'clip' and scene.get('video_profile'):
        if scene['video_profile'] != 'fast':
            raise ValueError('Profile chất lượng chưa được xác minh; không fallback.')
        if scene.get('clip_steps', scene.get('steps')) not in (None, 4):
            raise ValueError('LightX2V fast cần đúng 4 bước.')
        return 4
    lightning = (generation_version >= 3 and workflow == 'qwen_image' and
                 (project or {}).get('keyframe_profile') == 'lightning')
    if lightning:
        explicit = scene.get('keyframe_steps')
        if explicit not in (None, 4):
            raise ValueError('Qwen-Image Lightning cần đúng 4 bước; sửa steps ảnh của cảnh.')
        return 4
    key = 'clip_steps' if kind == 'clip' else 'keyframe_steps'
    value = scene.get(key)
    if value is None:
        value = scene.get('steps')
    if value is None:
        value = 4 if kind == 'clip' or workflow == 'qwen_edit' or lightning else 20
    return max(2, value) if kind == 'clip' else value


def clip_config(project, scene, override=None, *, index=None, duration=None):
    width, height = resolve_format(project.get('quality', 'draft'), project)['render_size']
    settings = override or {}
    if scene.get('video_profile') and settings.get('clip_steps', 4) != 4:
        raise ValueError('LightX2V fast cần đúng 4 bước; không sửa cấu hình đã gửi.')
    frames = 81
    if settings.get('shorten_last_shot', scene.get('shorten_last_shot', False)) and index is not None and duration:
        count = math.ceil(duration / (81 / 16))
        if index == count - 1:
            remaining = max(0, duration - index * (81 / 16))
            frames = min(81, max(33, 4 * math.ceil((math.ceil(remaining * 16 - 1e-9) - 1) / 4) + 1))
    return {'version': 2, 'width': width, 'height': height, 'frames': frames, 'fps': 16,
            'steps': (override or {}).get('clip_steps', stage_steps(scene, 'clip')),
            'shot_seconds': frames / 16}


def shot_config(job, stage, duration=None):
    prior = job.result.get('submissions', {}).get(stage, {})
    if prior.get('config'):
        return prior['config']
    # A persisted intent always wins over overrides, including legacy intents.
    override = None if prior else job.result.get('pending_clip_configs', {}).get(stage)
    index = int(stage.rsplit('-', 1)[1]) if stage.startswith('clip-') else None
    return clip_config(job.snapshot['project'], job.snapshot.get('scene') or {}, override,
                       index=index if job.snapshot.get('generation_version', 2) >= 3 else None,
                       duration=duration)


def identity_scene(scene, kind):
    """Use the historical steps key so adding nullable fields doesn't invalidate media."""
    value = dict(scene)
    key = 'clip_steps' if kind == 'clip' else 'keyframe_steps'
    if kind in {'clip', 'keyframe'} and scene.get(key) is not None:
        value['steps'] = scene[key]
    value.pop('keyframe_steps', None)
    value.pop('clip_steps', None)
    for field, default in [('motion', 'wan'), ('image_strategy', 'shared'),
                           ('shorten_last_shot', False)]:
        if kind != 'clip' or value.get(field, default) == default:
            value.pop(field, None)
    return value


def clip_output_matches(job, project, scene):
    """Mixed/overridden output is reusable only when every shot matches the request."""
    expected = clip_config(project, scene)
    original = clip_config(job.snapshot['project'], job.snapshot.get('scene') or {})
    if original != expected:
        # Old artifacts may predate per-shot metadata. Do not relabel them by inference.
        return False
    submissions = job.result.get('submissions', {})
    overrides = job.result.get('pending_clip_configs', {})
    stages = set(submissions) | set(overrides)
    if not stages:
        return original == expected
    for stage in stages:
        if not stage.startswith('clip-'):
            continue
        prior = submissions.get(stage, {})
        index = int(stage.rsplit('-', 1)[1])
        actual = prior.get('config') or (original if prior else clip_config(
            job.snapshot['project'], job.snapshot.get('scene') or {}, overrides[stage],
            index=index if job.snapshot.get('generation_version', 2) >= 3 else None,
            duration=(job.snapshot.get('scene') or {}).get('duration')))
        wanted = clip_config(project, scene, index=index,
            duration=scene.get('duration')) if job.snapshot.get('generation_version', 2) >= 3 else expected
        if actual != wanted:
            return False
    return True
