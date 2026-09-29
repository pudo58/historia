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
