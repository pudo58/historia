"""Resolved generation settings; legacy snapshots retain their original meaning."""
from studio.formats import resolve_format

LEGACY_WORKFLOW_HASHES = {
    'qwen_image': '2aecde2d6f0186957c70ac59a915b269e964bd99b35d17e1cf39a487b94c0ee4',
    'qwen_edit': '82a8231f891e89f2c8664d5de065da3eaff0192329a1e296532244c90f476025',
    'wan_i2v': '5d3332dff8d9bf2364d61cc5d8a28a47673ff9ec24b3378354c6de3f114ae79f',
}


def compatible_workflow_hashes(old, current):
    return bool(old) and set(old) == set(current) and all(
        value in (current[name], LEGACY_WORKFLOW_HASHES.get(name)) for name, value in old.items())


def stage_steps(scene, kind, workflow=None):
    key = 'clip_steps' if kind == 'clip' else 'keyframe_steps'
    value = scene.get(key)
    if value is None:
        value = scene.get('steps')
    if value is None:
        value = 4 if kind == 'clip' or workflow == 'qwen_edit' else 20
    return max(2, value) if kind == 'clip' else value


def clip_config(project, scene, override=None):
    width, height = resolve_format(project.get('quality', 'draft'), project)['render_size']
    return {'version': 2, 'width': width, 'height': height, 'frames': 81, 'fps': 16,
            'steps': (override or {}).get('clip_steps', stage_steps(scene, 'clip')),
            'shot_seconds': 81 / 16}


def shot_config(job, stage):
    prior = job.result.get('submissions', {}).get(stage, {})
    if prior.get('config'):
        return prior['config']
    # A persisted intent always wins over overrides, including legacy intents.
    override = None if prior else job.result.get('pending_clip_configs', {}).get(stage)
    return clip_config(job.snapshot['project'], job.snapshot.get('scene') or {}, override)


def identity_scene(scene, kind):
    """Use the historical steps key so adding nullable fields doesn't invalidate media."""
    value = dict(scene)
    key = 'clip_steps' if kind == 'clip' else 'keyframe_steps'
    if kind in {'clip', 'keyframe'} and scene.get(key) is not None:
        value['steps'] = scene[key]
    value.pop('keyframe_steps', None)
    value.pop('clip_steps', None)
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
        actual = prior.get('config') or (original if prior else {**original, 'steps': overrides[stage]['clip_steps']})
        if actual != expected:
            return False
    return True
