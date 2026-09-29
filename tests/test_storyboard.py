from types import SimpleNamespace

import pytest

from studio.generation import clip_config, shot_config, stage_steps
from studio.schemas import SceneInput, ShotDesign
from studio.storyboard import QUALITY_STATUS, composition, input_identity, prompt_suffix, warnings


def test_legacy_steps_unchanged_and_fast_guard():
    assert stage_steps({'steps': 12}, 'clip') == 12
    assert stage_steps({}, 'clip') == 4
    assert stage_steps({'video_profile': 'fast'}, 'clip') == 4
    with pytest.raises(ValueError):
        SceneInput(title='x', video_profile='fast', clip_steps=12)
    with pytest.raises(ValueError):
        SceneInput(title='x', video_profile='quality')
    assert not QUALITY_STATUS['quality']['available']
    with pytest.raises(ValueError):
        clip_config({}, {'video_profile': 'fast'}, {'clip_steps': 8})


def test_persisted_config_always_wins():
    old = {'steps': 12, 'frames': 81}
    job = SimpleNamespace(result={'submissions': {'clip-0': {'config': old}},
                                 'pending_clip_configs': {'clip-0': {'clip_steps': 4}}}, snapshot={})
    assert shot_config(job, 'clip-0') is old


def test_same_composition_prompts_and_angle_identity():
    shot = ShotDesign(subject='boat').model_dump()
    scene = {'shot_list': [shot, dict(shot)], 'shot_keyframes': ['a', 'b'],
             'image_strategy': 'per_shot', 'speech_id': 'audio', 'duration': 8}
    before = input_identity(scene, 0, 'keyframe')
    untouched = input_identity(scene, 1, 'clip')
    scene['shot_list'][0] = {**shot, 'camera_angle': 'low'}
    assert input_identity(scene, 0, 'keyframe') != before
    assert input_identity(scene, 1, 'clip') == untouched
    assert composition(shot) in prompt_suffix(shot, True)
    assert '20%' in prompt_suffix(shot, True)
    assert warnings([shot, {**shot, 'direction': 'right-to-left'}], 'chain_last')


def test_fast_graph_has_four_steps_and_two_noise_stages():
    from studio.packs import graph_for
    graph = graph_for('wan_i2v', 'boat', 42, 'final', ['image.png'], 'test', steps=4)
    assert graph['86']['inputs']['steps'] == 4
    assert graph['86']['inputs']['end_at_step'] == 2
    assert graph['85']['inputs']['start_at_step'] == 2
    assert graph['85']['inputs']['end_at_step'] == 4


def test_changed_angle_requires_separate_reviewed_image():
    shots = [ShotDesign(), ShotDesign(camera_angle='low')]
    with pytest.raises(ValueError):
        SceneInput(title='x', shot_list=shots)
    assert SceneInput(title='x', shot_list=shots, image_strategy='per_shot')
