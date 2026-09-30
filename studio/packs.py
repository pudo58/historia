"""Curated workflow pack, explicit runtime metadata lock and no arbitrary repo installation."""
import copy
import json
import re
from pathlib import Path

from huggingface_hub import HfApi, get_hf_file_metadata, hf_hub_url

from ghm.manifests import ModelAsset, resolve_model
from studio.service import canonical_hash

PACK_ID = "historical-v1"
COMFY_COMMIT = "ee71d5c4993f29086b27fde1629a945ae48425bf"
# New jobs record this in their snapshot; graph changes are gated on it so a resumed older
# job reproduces exactly what it started with. 4: clips saved at CRF 17 instead of ~23.
GENERATION_VERSION = 4
CLIP_CRF = 17
TEMPLATE_COMMIT = "fc54797eb70273aee6e3918eeeecc1d0ac0760e1"
WORKFLOWS = Path(__file__).parent / "workflows"
ENVIRONMENTS = {
    "llm": "transformers==5.17.0 accelerate==1.12.0 huggingface_hub==1.7.1 pillow==12.1.1",
    # torchao 0.15+ imports torch.nn.functional.ScalingType, which the pinned torch 2.8 build does not have.
    "tts": "vieneu==3.8.3 transformers==4.57.6 huggingface_hub==0.36.2 neucodec==0.0.4 torchao==0.14.1",
}
REPOS = {"qwen": "Comfy-Org/Qwen-Image_ComfyUI", "edit": "Comfy-Org/Qwen-Image-Edit_ComfyUI",
         "wan": "Comfy-Org/Wan_2.2_ComfyUI_Repackaged"}

# All filenames were read from pinned official workflow templates, never guessed hashes.
MODEL_FILES = [
    ("qwen-image", REPOS["qwen"], "split_files/diffusion_models/qwen_image_fp8_e4m3fn.safetensors", "models/diffusion_models"),
    ("qwen-encoder", REPOS["qwen"], "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors", "models/text_encoders"),
    ("qwen-vae", REPOS["qwen"], "split_files/vae/qwen_image_vae.safetensors", "models/vae"),
    ("qwen-edit", REPOS["edit"], "split_files/diffusion_models/qwen_image_edit_2509_fp8_e4m3fn.safetensors", "models/diffusion_models"),
    ("qwen-edit-lightning", "lightx2v/Qwen-Image-Lightning", "Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors", "models/loras"),
    ("wan-high", REPOS["wan"], "split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", "models/diffusion_models"),
    ("wan-low", REPOS["wan"], "split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors", "models/diffusion_models"),
    ("wan-high-lora", REPOS["wan"], "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors", "models/loras"),
    ("wan-low-lora", REPOS["wan"], "split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors", "models/loras"),
    ("wan-encoder", "Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors", "models/text_encoders"),
    ("wan-vae", REPOS["wan"], "split_files/vae/wan_2.1_vae.safetensors", "models/vae"),
]

# Deliberately excluded from the base installer; choosing a profile must not
# silently download optional multi-GB files onto every GPU.
OPTIONAL_MODEL_FILES = {
    'qwen-image-lightning-fp8': ('lightx2v/Qwen-Image-Lightning',
        'Qwen-Image-fp8-e4m3fn-Lightning-4steps-V1.0-bf16.safetensors', 'models/loras'),
    'rife-v4.26': ('Comfy-Org/frame_interpolation',
        'frame_interpolation/rife_v4.26.safetensors', 'models/frame_interpolation'),
    # Wan2.2 S2V (the character speaks, lips follow the audio). Three files that are only useful together.
    's2v-model': (REPOS['wan'],
        'split_files/diffusion_models/wan2.2_s2v_14B_fp8_scaled.safetensors', 'models/diffusion_models'),
    's2v-audio-encoder': (REPOS['wan'],
        'split_files/audio_encoders/wav2vec2_large_english_fp16.safetensors', 'models/audio_encoders'),
    's2v-lightning-lora': (REPOS['wan'],
        'split_files/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors', 'models/loras'),
}
RIFE_SHA256 = '151874592c877740e5db11522f4514df569eeafb0a0fcb2696f16e9e8d317c94'
# Reviewed checksums (read from the Hugging Face LFS metadata); the install refuses any other content.
OPTIONAL_SHA256 = {
    'rife-v4.26': RIFE_SHA256,
    's2v-model': '140e75af5534ac3d91e710d9df756f7032addd64b341ba2c1c70e3e6da9aa216',
    's2v-audio-encoder': 'f0017a43ea57ef6b3d4866be607844bbd8cada6d30966f7d70044ed0d63d3f9e',
    's2v-lightning-lora': '698321cb86bd30c4af06c9b84e656a1048c8cb54e06d50694536fb5de37fde41',
}
# One click installs every member of a group; the group is ready only when all members are.
OPTIONAL_MODEL_GROUPS = {'wan-s2v': ('s2v-model', 's2v-audio-encoder', 's2v-lightning-lora')}
S2V_MODEL = 'wan2.2_s2v_14B_fp8_scaled.safetensors'
S2V_AUDIO_ENCODER = 'wav2vec2_large_english_fp16.safetensors'
S2V_LORA = 'wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors'
# Wan S2V works in chunks of 77 frames; each chunk covers 5 s of audio (80 frames at 16 fps) and the
# decoded film is 2 frames short of K*80 (the template drops 3 frames after prepending the first latent).
S2V_CHUNK_FRAMES = 77
S2V_FPS = 16
S2V_CHUNK_SECONDS = 80 / S2V_FPS
S2V_MAX_CHUNKS = 3


def optional_members(name: str) -> tuple[str, ...]:
    """The model files behind one installable name (a group expands to its members)."""
    if name in OPTIONAL_MODEL_GROUPS:
        return OPTIONAL_MODEL_GROUPS[name]
    if name in OPTIONAL_MODEL_FILES and not any(name in members for members in OPTIONAL_MODEL_GROUPS.values()):
        return (name,)
    raise ValueError('Model tùy chọn không được hỗ trợ.')


def optional_names() -> list[str]:
    """Names the UI can install: single files not owned by a group, then the groups."""
    owned = {member for members in OPTIONAL_MODEL_GROUPS.values() for member in members}
    return [name for name in OPTIONAL_MODEL_FILES if name not in owned] + list(OPTIONAL_MODEL_GROUPS)


def resolve_optional_model(name: str, token: str | None = None):
    if name not in OPTIONAL_MODEL_FILES:
        raise ValueError('Model tùy chọn không được hỗ trợ.')
    repo, filename, destination = OPTIONAL_MODEL_FILES[name]
    asset = resolve_model(ModelAsset(name=name, repo=repo, filename=filename, destination=destination,
                                    sha256=OPTIONAL_SHA256.get(name)), token)
    return asset.model_dump(mode='json')


def s2v_chunks(audio_seconds: float) -> int:
    """Chunks needed so the decoded film (5 s per chunk minus 2 frames) covers the audio."""
    if audio_seconds <= 0:
        raise ValueError('Audio rỗng.')
    return max(1, -int(-(audio_seconds + 2 / S2V_FPS) // S2V_CHUNK_SECONDS))


def pack_info(lock: dict | None = None) -> dict:
    return {
        "id": PACK_ID, "name": "Bộ Video lịch sử", "version": 1,
        "description": "Qwen3-VL · Qwen-Image/Edit · Wan2.2 I2V · VieNeu 0.5B",
        "comfy_commit": COMFY_COMMIT, "template_commit": TEMPLATE_COMMIT,
        "status": "resolved" if lock else "needs_metadata",
        "download_bytes": lock.get("download_bytes") if lock else None,
        "lock_hash": canonical_hash(lock) if lock else None,
        "workflows": [
            {"id": "qwen_image", "name": "Tạo ảnh từ mô tả", "verified": False},
            {"id": "qwen_edit", "name": "Ảnh theo nhân vật / trang phục tham khảo", "verified": False},
            {"id": "wan_i2v", "name": "Tạo clip từ ảnh đã duyệt", "verified": False},
        ],
        "license_notice": "Kiểm tra license model và quyền sử dụng ảnh/giọng tham chiếu. Không dùng bản TTS 0.3B NC.",
        "models": lock.get("models", []) if lock else [
            {"name": n, "repo": r, "filename": f, "destination": d} for n,r,f,d in MODEL_FILES
        ],
    }


def check_model_access(lock: dict, token: str | None = None) -> None:
    """Check pinned file download endpoints, not just public repository metadata.

    HEAD requests only: no model payload downloads and no raw upstream errors.
    Rechecked on execution so old plans and resumed installs cannot bypass it.
    """
    files = [(m['name'], m['repo'], m['revision'], m['filename'])
             for m in lock['models']]
    files.extend((s['name'], s['repo'], s['revision'], f['filename'])
                 for s in lock['snapshots'] for f in s['files'])
    for name, repo, revision, filename in files:
        try:
            get_hf_file_metadata(hf_hub_url(repo, filename, revision=revision),
                                 token=token or False, timeout=30)
        except Exception as exc:  # noqa: BLE001 -- never leak token/signed URL
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            location = f'{name}: {repo}/{filename}'
            if status in (401, 403) or type(exc).__name__ == 'GatedRepoError':
                reason = ('Chưa có quyền tải. Đăng nhập https://huggingface.co/' + repo
                          + ' để kiểm tra/chấp thuận quyền truy cập, rồi cấu hình token Hugging Face '
                          'có quyền đọc repository trong ứng dụng. Không gửi token vào chat.')
            elif status == 404:
                reason = 'Không tìm thấy file/revision hoặc repository bị ẩn với quyền hiện tại.'
            else:
                reason = 'Không xác minh được quyền tải do kết nối hoặc dịch vụ Hugging Face. Thử kiểm tra lại.'
            raise ValueError(f'Kiểm tra quyền tải thất bại — {location}. {reason} '
                             'Đã dừng trước khi tải model hoặc cài dependency; giữ nguyên file đã có.') from None


REMOTE_ACCESS_SCRIPT = r'''
import json, sys, urllib.request, urllib.error
c = json.load(sys.stdin)
class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials to CDN/signed redirects; keep requests HEAD-only.
        if not newurl.startswith('https://'):
            raise ValueError('Insecure redirect')
        from urllib.parse import urlsplit
        headers = dict(req.headers) if urlsplit(newurl).netloc == urlsplit(req.full_url).netloc else {}
        return urllib.request.Request(newurl, headers=headers, method='HEAD')
opener = urllib.request.build_opener(SafeRedirect())
for index, url in enumerate(c['urls']):
    try:
        headers = {'Authorization': 'Bearer ' + c['token']} if c.get('token') else {}
        with opener.open(urllib.request.Request(url, headers=headers, method='HEAD'), timeout=30) as response:
            status = response.status
        if status != 200:
            print(json.dumps({'ok': False, 'index': index, 'status': status})); break
    except urllib.error.HTTPError as exc:
        print(json.dumps({'ok': False, 'index': index, 'status': exc.code})); break
    except Exception:
        print(json.dumps({'ok': False, 'index': index, 'status': 0})); break
else:
    print(json.dumps({'ok': True, 'checked_files': len(c['urls'])}))
'''


async def check_remote_model_access(executor, lock, token=None):
    """Read-only Pod HEAD checks before any install mutation; token only via stdin."""
    import shlex
    files = [(m['repo'], m['revision'], m['filename']) for m in lock['models']]
    files += [(s['repo'], s['revision'], f['filename']) for s in lock['snapshots'] for f in s['files']]
    if not files:
        raise ValueError('Bộ model không có file để kiểm tra quyền tải.')
    urls = [hf_hub_url(repo, filename, revision=revision) for repo, revision, filename in files]
    result = await executor.run_input('python3 -c ' + shlex.quote(REMOTE_ACCESS_SCRIPT),
                                      json.dumps({'urls': urls, 'token': token or None}),
                                      timeout=max(60, 35 * len(urls)))
    try:
        evidence = json.loads(result.stdout) if result.rc == 0 else {}
    except (TypeError, ValueError):
        evidence = {}
    if evidence.get('ok') is not True or evidence.get('checked_files') != len(files):
        status = evidence.get('status')
        reason = ('Thiếu quyền tải (401/403): chấp thuận license repository và cấu hình token đọc trong ứng dụng; không gửi token vào chat.'
                  if status in (401, 403) else 'Không xác minh được file/revision hoặc kết nối HTTPS từ Pod.')
        raise ValueError('Kiểm tra quyền tải từ Pod thất bại. ' + reason + ' Chưa cài dependency hoặc tải model.') from None
    return {'status': 'accessible', 'method': 'HEAD', 'origin': 'pod', 'checked_files': len(files)}


def resolve_pack(token: str | None = None) -> dict:
    """Network metadata only. Called explicitly, never automatically on startup."""
    api = HfApi(endpoint="https://huggingface.co", token=token or False)
    models = []
    for name, repo, filename, destination in MODEL_FILES:
        asset = resolve_model(ModelAsset(name=name, repo=repo, filename=filename, destination=destination), token, api)
        models.append(asset.model_dump(mode="json"))
    snapshots = []
    for name, repo in [("qwen3-vl", "Qwen/Qwen3-VL-8B-Instruct"), ("vieneu", "pnnbao-ump/VieNeu-TTS"),
                       ("codec", "neuphonic/neucodec-onnx-decoder-int8")]:
        info = api.model_info(repo, files_metadata=True, timeout=30)
        if not re.fullmatch(r"[a-f0-9]{40}", info.sha or ""):
            raise ValueError("Repo dịch vụ chưa có revision cố định.")
        files = [f for f in (info.siblings or []) if f.rfilename.endswith((".json", ".safetensors", ".txt", ".model", ".tiktoken", ".onnx"))]
        locked_files = []
        for f in files:
            sha = getattr(f.lfs, "sha256", None)
            oid = sha or f.blob_id
            if not oid or not re.fullmatch(r"[a-f0-9]{64}" if sha else r"[a-f0-9]{40}", oid) or f.size is None:
                raise ValueError("Model dịch vụ thiếu checksum/dung lượng: " + name)
            if Path(f.rfilename).is_absolute() or ".." in f.rfilename.split("/"):
                raise ValueError("Đường dẫn model không hợp lệ.")
            locked_files.append({"filename": f.rfilename, "size_bytes": f.size,
                                 "digest": oid, "algorithm": "sha256" if sha else "git-sha1"})
        snapshots.append({"name": name, "repo": repo, "revision": info.sha,
                          "files": locked_files, "size_bytes": sum(f["size_bytes"] for f in locked_files)})
    return {"pack_id": PACK_ID, "comfy_commit": COMFY_COMMIT, "models": models,
            "snapshots": snapshots, "download_bytes": sum(int(m.get("size_bytes") or 0) for m in models) + sum(int(s["size_bytes"] or 0) for s in snapshots),
            "environments": ENVIRONMENTS,
            "workflow_hashes": {name: canonical_hash(load_graph(name)) for name in ["qwen_image", "qwen_edit", "wan_i2v"]}}


def load_graph(name: str) -> dict:
    if name not in {"qwen_image", "qwen_edit", "wan_i2v", "rife_post", "wan_s2v"}:
        raise ValueError("Workflow không thuộc bộ cho phép.")
    return json.loads((WORKFLOWS / f"{name}.json").read_text(encoding="utf-8"))


def graph_for(name: str, prompt: str, seed: int, quality: str, image_names: list[str],
              output_prefix: str, steps: int | None = None, shot: int = 0,
              project_settings: dict | None = None, *, frames: int = 81,
              generation_version: int = 2, chunks: int = 1) -> dict:
    from studio.formats import resolve_format
    graph = copy.deepcopy(load_graph(name))
    width, height = resolve_format(quality, project_settings)["render_size"]
    if name == 'wan_s2v':
        if len(image_names) != 2:
            raise ValueError('S2V cần đúng một ảnh keyframe và một file giọng.')
        return s2v_graph(graph, prompt, seed, image_names[0], image_names[1], output_prefix, width, height, chunks)
    if name == 'rife_post':
        if len(image_names) != 1:
            raise ValueError('RIFE cần đúng một clip nguồn đã lưu.')
        graph['1']['inputs']['file'] = image_names[0]
        graph['6']['inputs']['filename_prefix'] = output_prefix
        if generation_version >= 4:
            high_quality_save(graph['6']['inputs'])
    elif name == "qwen_image":
        graph["6"]["inputs"]["text"] = prompt
        graph["58"]["inputs"].update(width=width, height=height)
        lightning = generation_version >= 3 and (project_settings or {}).get('keyframe_profile') == 'lightning'
        if lightning and generation_version >= 4:
            # cfg 1 ignores the negative prompt; keep the exclusions in the positive one.
            graph["6"]["inputs"]["text"] = prompt + " No text, captions or watermarks; no modern objects."
        if lightning:
            graph['67'] = {'class_type': 'LoraLoaderModelOnly', 'inputs': {
                'model': ['37', 0], 'lora_name':
                'Qwen-Image-fp8-e4m3fn-Lightning-4steps-V1.0-bf16.safetensors', 'strength_model': 1}}
            graph['66']['inputs']['model'] = ['67', 0]
            graph['66']['inputs']['shift'] = 3
            graph['3']['inputs']['cfg'] = 1
        graph["3"]["inputs"].update(seed=seed, steps=steps or (4 if lightning else 20))
        graph["60"]["inputs"]["filename_prefix"] = output_prefix
    elif name == "qwen_edit":
        if not image_names:
            raise ValueError("Workflow tham chiếu cần ít nhất một ảnh đã chọn.")
        graph["78"]["inputs"]["image"] = image_names[0]
        if project_settings and any(project_settings.get(k) is not None for k in ("render_profile", "aspect_ratio", "output_resolution")):
            # References are fitted/padded locally by the backend, never stretched or cropped.
            graph["93"] = {"class_type": "ImageScale", "inputs": {
                "image": ["78", 0], "upscale_method": "lanczos",
                "width": width, "height": height, "crop": "disabled"}}
        for i, image in enumerate(image_names[1:3], start=2):
            node_id = str(400+i)
            graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": image}}
            for encoder in ["110", "111"]:
                graph[encoder]["inputs"][f"image{i}"] = [node_id, 0]
        graph["111"]["inputs"]["prompt"] = (
            'Preserve the identities and historical details of the reference images. '
            'Create this scene: ' + prompt if generation_version >= 3 else prompt)
        graph["3"]["inputs"].update(seed=seed, steps=steps or 4)
        graph["60"]["inputs"]["filename_prefix"] = output_prefix
    else:
        if not image_names:
            raise ValueError("Cần ảnh đại diện đã duyệt trước khi tạo clip.")
        graph["97"]["inputs"]["image"] = image_names[0]
        graph["93"]["inputs"]["text"] = (prompt + f" Shot variation {shot+1}; no text or subtitles."
            if generation_version < 3 else prompt + f" Shot {shot+1}: continuous natural motion, clean historical imagery."
            # cfg 1 (LightX2V) ignores the negative prompt, so the key exclusions live here.
            + (" No on-screen text, captions, logos or watermarks." if generation_version >= 4 else ""))
        graph["98"]["inputs"].update(width=width, height=height, length=frames)
        count = max(2, steps or 4)
        graph["86"]["inputs"].update(noise_seed=seed+shot, steps=count, end_at_step=count//2)
        graph["85"]["inputs"].update(steps=count, start_at_step=count//2, end_at_step=count)
        graph["108"]["inputs"]["filename_prefix"] = output_prefix
        if generation_version >= 4:
            high_quality_save(graph["108"]["inputs"])
    return graph


def s2v_graph(graph: dict, prompt: str, seed: int, image: str, audio: str, output_prefix: str,
              width: int, height: int, chunks: int) -> dict:
    """Wan2.2 S2V, wired like the official template: one first chunk, then ``chunks - 1`` extensions.

    Each extension is conditioned on the audio window after the frames already made and on the
    accumulated latent, then appended to it; the tail decodes the whole latent once.
    """
    if not 1 <= chunks <= S2V_MAX_CHUNKS:
        raise ValueError(f'S2V chỉ nhận 1–{S2V_MAX_CHUNKS} đoạn (mỗi đoạn ~5 giây giọng nói).')
    if width % 16 or height % 16:
        raise ValueError('Kích thước S2V phải chia hết cho 16.')
    graph['21']['inputs']['text'] = prompt
    graph['32']['inputs']['audio'] = audio
    graph['34']['inputs']['image'] = image
    graph['40']['inputs'].update(width=width, height=height, length=S2V_CHUNK_FRAMES)
    graph['41']['inputs']['seed'] = seed
    latest = '41'
    for index in range(1, chunks):
        extend, sample, join = f'5{index}0', f'5{index}1', f'5{index}2'
        graph[extend] = {'class_type': 'WanSoundImageToVideoExtend', 'inputs': {
            'positive': ['21', 0], 'negative': ['22', 0], 'vae': ['30', 0], 'length': S2V_CHUNK_FRAMES,
            'video_latent': [latest, 0], 'audio_encoder_output': ['33', 0], 'ref_image': ['34', 0]}}
        graph[sample] = {'class_type': 'KSampler', 'inputs': {
            'model': ['12', 0], 'seed': seed + index, 'steps': 4, 'cfg': 1, 'sampler_name': 'uni_pc',
            'scheduler': 'simple', 'positive': [extend, 0], 'negative': [extend, 1],
            'latent_image': [extend, 2], 'denoise': 1}}
        graph[join] = {'class_type': 'LatentConcat', 'inputs': {
            'samples1': [latest, 0], 'samples2': [sample, 0], 'dim': 't'}}
        latest = join
    graph['60']['inputs']['samples'] = [latest, 0]
    graph['61']['inputs']['samples2'] = [latest, 0]
    graph['65']['inputs']['filename_prefix'] = output_prefix
    high_quality_save(graph['65']['inputs'])
    return graph


def high_quality_save(inputs: dict) -> None:
    """Clips are re-encoded again at export; keep the intermediate near-lossless (verified on
    ComfyUI ee71d5c: SaveVideo h264 'encoding' dynamic combo, default is ~CRF 23)."""
    inputs.update({"format.codec.encoding": "re-encode", "format.codec.encoding.crf": CLIP_CRF})
