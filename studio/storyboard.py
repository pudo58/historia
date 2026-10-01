"""Opt-in shot direction. Legacy snapshots never acquire a storyboard implicitly."""
import hashlib
import json

QUALITY_STATUS = {
    'fast': {'available': True, 'steps': 4, 'workflow': 'wan_i2v', 'label': 'Wan LightX2V · 4 bước'},
    'quality': {'available': False, 'verification_status': 'official_source_fetch_unavailable_in_execution_context', 'official_source': 'https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged', 'reason': 'Chưa xác minh tổ hợp model, LoRA, CFG, sampler và chuyển high/low-noise từ nguồn chính thức; không tự tải hoặc fallback.'},
}
BENCHMARK_CASES = [
    {'id': key, 'subject': subject, 'status': 'awaiting_separate_paid_consent',
     'criteria': ['subject/crop', 'continuity', 'motion', 'elapsed_seconds'],
     'quality_profile_available': False}
    for key, subject in [('portrait', 'Cận nhân vật'), ('water', 'Thuyền và nước'), ('crowd', 'Toàn cảnh đông người')]]
COMPOSITION_FIELDS = ('purpose', 'subject', 'action', 'shot_size', 'camera_angle',
                      'placement', 'direction', 'composition', 'lighting', 'camera_move')

def composition(shot):
    return '\n'.join(f'{key}: {shot.get(key, "")}' for key in COMPOSITION_FIELDS)

def prompt_suffix(shot, portrait=False):
    text = composition(shot)
    if portrait:
        text += '\nNative portrait composition: foreground, midground, background; keep head, hands and historical details inside the central 80% safe crop (16-pixel alignment); reserve bottom 20% for subtitles added in editing. No text.'
    return text

def input_identity(scene, index, kind):
    shot = scene['shot_list'][index]
    value = {key: shot.get(key, '') for key in COMPOSITION_FIELDS}
    value['index'] = index if scene.get('image_strategy') == 'per_shot' or kind == 'clip' else 0
    value.update({key: scene.get(key) for key in ('visual_prompt', 'camera', 'seed', 'reference_ids', 'character_ids', 'keyframe_steps')})
    if kind == 'clip':
        value.update(profile=scene.get('video_profile'), steps=scene.get('clip_steps'),
                     speech_id=scene.get('speech_id'), duration=scene.get('duration'),
                     shorten_last_shot=scene.get('shorten_last_shot', False),
                     image=(scene.get('shot_keyframes') or [scene.get('keyframe_id')])[index if scene.get('image_strategy') == 'per_shot' else 0])
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def warnings(shots, strategy):
    result = []
    if strategy == 'chain_last':
        result.append('Nối frame cuối có nguy cơ tích lũy biến dạng; phải duyệt từng frame nối.')
    for index, (a, b) in enumerate(zip(shots, shots[1:]), 2):
        changed = [key for key in ('direction', 'lighting', 'placement') if a.get(key) != b.get(key)]
        if changed:
            result.append(f'Shot {index}: kiểm tra trục 180°, hướng, vị trí và ánh sáng ({", ".join(changed)}).')
    if shots:
        result.append('Kiểm tra chỉ dẫn, không chứng minh video liên tục hoặc hoàn hảo; cần duyệt hình thực tế.')
    return result


# Editing rhythm for an automatic storyboard: establish, move in, find a telling detail, step back out.
_RHYTHM = [
    ('wide', 'eye', 'slow_push', 'Establishing view that shows the whole setting and everyone in it.'),
    ('medium', 'eye', 'gentle_slide', 'Medium shot on the main figures, bodies and costumes readable, a different angle from the previous shot.'),
    ('close', 'low', 'slow_push', 'Close view on a telling detail of the moment: faces, hands, a banner, a tool or the water.'),
    ('medium', 'high', 'short_follow', 'Medium shot from a slightly higher angle following the action.'),
]


def _sentences(text, count):
    """The narration cut into ``count`` consecutive pieces of about equal length (sentence boundaries first)."""
    import re
    parts = [p.strip() for p in re.split(r'(?<=[.!?])\s+', (text or '').strip()) if p.strip()]
    if count <= 1 or not parts:
        return [(text or '').strip()] * count
    target = sum(len(p) for p in parts) / count
    groups, current, size = [], [], 0
    for part in parts:
        current.append(part)
        size += len(part)
        if size >= target * (len(groups) + 1) - sum(len(' '.join(g)) for g in groups) and len(groups) < count - 1:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    texts = [' '.join(g) for g in groups]
    while len(texts) < count:
        texts.append(texts[-1])
    return texts[:count]


def auto_shots(scene, count):
    """A varied shot list (size, angle, move) for ``count`` clips of a scene. Lighting, direction and
    placement stay constant so the 180-degree/lighting warnings do not fire."""
    excerpts = _sentences(scene.get('narration', ''), count)
    shots = []
    for index in range(count):
        size, angle, move, composition = _RHYTHM[index % len(_RHYTHM)]
        shots.append({'purpose': f'Shot {index + 1}/{count}: ' + composition.split('.')[0], 'subject': scene.get('title', ''),
                      'action': '', 'shot_size': size, 'camera_angle': angle, 'placement': 'center',
                      'direction': 'left-to-right', 'composition': composition, 'lighting': '',
                      'camera_move': move, 'narration_excerpt': excerpts[index][:1000]})
    return shots
