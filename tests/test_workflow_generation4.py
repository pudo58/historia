"""Generation 4 graph changes are gated, so resumed older jobs reproduce exactly."""
from studio.packs import CLIP_CRF, GENERATION_VERSION, graph_for


def test_new_jobs_use_generation_4():
    assert GENERATION_VERSION == 4


def test_clips_saved_near_lossless_only_from_generation_4():
    old = graph_for('wan_i2v', 'A fort', 1, 'draft', ['k.png'], 'p', generation_version=3)
    new = graph_for('wan_i2v', 'A fort', 1, 'draft', ['k.png'], 'p', generation_version=4)
    assert 'format.codec.encoding' not in old['108']['inputs']
    assert new['108']['inputs']['format.codec.encoding'] == 're-encode'
    assert new['108']['inputs']['format.codec.encoding.crf'] == CLIP_CRF == 17
    rife = graph_for('rife_post', '', 0, 'draft', ['c.mp4'], 'p', generation_version=4)
    assert rife['6']['inputs']['format.codec.encoding.crf'] == CLIP_CRF


def test_exclusions_move_into_positive_prompt_when_cfg_is_1():
    new = graph_for('wan_i2v', 'A fort', 1, 'draft', ['k.png'], 'p', generation_version=4)
    old = graph_for('wan_i2v', 'A fort', 1, 'draft', ['k.png'], 'p', generation_version=3)
    assert 'watermarks' in new['93']['inputs']['text'] and 'watermarks' not in old['93']['inputs']['text']
    assert new['85']['inputs']['cfg'] == 1 and new['86']['inputs']['cfg'] == 1
    light = graph_for('qwen_image', 'A fort', 1, 'draft', [], 'p', generation_version=4,
                      project_settings={'keyframe_profile': 'lightning'})
    base = graph_for('qwen_image', 'A fort', 1, 'draft', [], 'p', generation_version=4)
    assert 'watermarks' in light['6']['inputs']['text'] and base['6']['inputs']['text'] == 'A fort'
