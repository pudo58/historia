"""Executed only on the SSH host in isolated, provisioned environments."""
import hashlib
import json
import math
import os
import re
import sys
import time
import wave
from pathlib import Path


def split_speech(text, max_chars=240):
    """Bounded sentence/phrase chunks; timestamps represent chunks, not word alignment."""
    words = re.sub(r"\s+", " ", text).strip()
    if not words:
        raise ValueError("Speech text is empty")
    segments = []
    for sentence in re.split(r"(?<=[.!?;])\s+", words):
        chunk = ""
        for word in sentence.split():
            if len(word) > max_chars:
                raise ValueError("Speech token exceeds segment limit")
            if chunk and len(chunk) + len(word) + 1 > max_chars:
                segments.append(chunk)
                chunk = ""
            chunk = (chunk + " " + word).strip()
        if chunk:
            segments.append(chunk)
    return segments


def inspect_wav(path):
    """Validate actual PCM samples without optional dependencies (also runs locally)."""
    with wave.open(str(path), "rb") as audio:
        channels, width, rate, frames = (audio.getnchannels(), audio.getsampwidth(),
                                         audio.getframerate(), audio.getnframes())
        if channels not in (1, 2) or width not in (1, 2, 3, 4) or rate < 8000 or frames <= 0:
            raise ValueError("Unsupported or empty PCM audio")
        count, energy, peak = 0, 0.0, 0.0
        while True:
            data = audio.readframes(65536)
            if not data:
                break
            if len(data) % (width * channels):
                raise ValueError("Truncated PCM audio")
            for offset in range(0, len(data), width):
                raw = data[offset:offset + width]
                value = (raw[0] - 128) / 128 if width == 1 else int.from_bytes(raw, "little", signed=True) / (2 ** (width * 8 - 1))
                energy += value * value
                peak = max(peak, abs(value))
                count += 1
        if count != frames * channels:
            raise ValueError("Truncated PCM audio")
        rms = math.sqrt(energy / count)
        if rms < 0.0001 or peak < 0.001:
            raise ValueError("Audio is silent or below minimum signal level")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"duration": frames / rate, "sample_rate": rate, "channels": channels,
            "frames": frames, "sample_width": width, "rms": rms, "peak": peak,
            "sha256": digest.hexdigest(), "non_silent": True}


def synthesize_segments(model, text, voice, output):
    output = Path(output)
    chunks, paths, cursor = [], [], 0
    try:
        for index, sentence in enumerate(split_speech(text)):
            path = output.with_name(f"speech-segment-{index:04d}.wav")
            paths.append(path)
            model.save(model.infer(text=sentence, voice=voice), str(path))
            measured = inspect_wav(path)
            if chunks and any(measured[key] != chunks[0][key] for key in ("sample_rate", "channels", "sample_width")):
                raise ValueError("Inconsistent speech segment audio format")
            rate = measured["sample_rate"]
            chunks.append({"text": sentence, "start": cursor / rate,
                           "end": (cursor + measured["frames"]) / rate, **measured})
            cursor += measured["frames"]
        with wave.open(str(output), "wb") as combined:
            combined.setnchannels(chunks[0]["channels"])
            combined.setsampwidth(chunks[0]["sample_width"])
            combined.setframerate(chunks[0]["sample_rate"])
            for path in paths:
                with wave.open(str(path), "rb") as source:
                    while True:
                        data = source.readframes(65536)
                        if not data:
                            break
                        combined.writeframesraw(data)
        return {"audio": inspect_wav(output), "segments": chunks,
                "timestamp_kind": "measured_tts_segments", "word_aligned": False}
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def local_codec_path(repo_id, filename="model.onnx"):
    """Use the checksummed local ONNX file. VieNeu otherwise treats the path as a Hub repo id."""
    candidate = Path(repo_id)
    if candidate.is_dir() and (candidate / filename).is_file():
        return candidate / filename
    if candidate.is_file() and candidate.name == filename:
        return candidate
    return None


def audition_voices(model, text, output):
    """The same line in every preset voice the loaded model ships, one WAV per voice (audition-N.wav)."""
    output = Path(output)
    voices = []
    for index, (label, voice_id) in enumerate(model.list_preset_voices()):
        path = output.with_name(f"audition-{index:02d}.wav")
        model.save(model.infer(text=text, voice=model.get_preset_voice(voice_id)), str(path))
        measured = inspect_wav(path)
        voices.append({"voice": voice_id, "label": label, "file": str(path),
                       "duration": measured["duration"], "sha256": measured["sha256"]})
    if not voices:
        raise RuntimeError("Model giọng đọc không có giọng preset nào.")
    return {"audition": voices}


def main() -> None:
    config = json.load(sys.stdin)
    root = Path(config["root"])
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if config["mode"] == "language":
        import torch
        from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
        location = root / "service-models/Qwen/Qwen3-VL-8B-Instruct"
        model = Qwen3VLForConditionalGeneration.from_pretrained(location, dtype=torch.bfloat16,
                                                               device_map="auto", local_files_only=True)
        processor = AutoProcessor.from_pretrained(location, local_files_only=True)
        content = [{"type": "image", "image": path} for path in config.get("images", [])]
        content.append({"type": "text", "text": config["prompt"]})
        messages = [{"role": "user", "content": content}]
        inputs = processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                                return_dict=True, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            generated = model.generate(**inputs, max_new_tokens=12000, do_sample=False)
        text = processor.batch_decode(generated[:, inputs.input_ids.shape[1]:], skip_special_tokens=True)[0]
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Model did not produce a JSON object.")
        result = json.loads(text[start:end+1])
    elif config["mode"] == "speech":
        from importlib.metadata import version
        from vieneu import Vieneu
        from vieneu.utils import NeuCodecOnnx
        codec_dir = root / "service-models/neuphonic/neucodec-onnx-decoder-int8"

        def _load_local_onnx(cls, repo_id, filename="model.onnx", hf_token=None):
            local = local_codec_path(repo_id, filename)
            if local is not None:
                return cls(str(local))
            from huggingface_hub import hf_hub_download
            return cls(hf_hub_download(repo_id=repo_id, filename=filename, token=hf_token))

        NeuCodecOnnx.from_pretrained = classmethod(_load_local_onnx)
        requested = config.get("tts_device", "cpu")
        if requested not in ("cuda", "cpu"):
            raise RuntimeError("Thiết bị giọng đọc không hợp lệ. Không tự chuyển thiết bị.")
        import torch
        if requested == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA không khả dụng trong môi trường giọng đọc. Không tự chuyển về CPU.")
        backbone_device = requested
        if requested == "cuda":
            torch.cuda.reset_peak_memory_stats()
        load_started = time.monotonic()
        model = Vieneu(mode="standard", backbone_repo=str(root / "service-models/pnnbao-ump/VieNeu-TTS"),
                       backbone_device=backbone_device, gguf_filename=None,
                       codec_repo=str(codec_dir),
                       codec_device="cpu")
        try:
            backbone = getattr(model, "backbone", None)
            if backbone is None or not hasattr(backbone, "parameters"):
                raise RuntimeError("Không xác nhận được thiết bị backbone giọng đọc.")
            devices = {parameter.device.type for parameter in backbone.parameters()}
            if requested == "cuda" and devices != {"cuda"}:
                raise RuntimeError("Backbone giọng đọc không nằm trên CUDA. Không ghi kết quả giả.")
            load_seconds = round(time.monotonic() - load_started, 3)
            model.use_chat_format = True
            if config.get("audition"):
                result = audition_voices(model, config["text"], config["output"])
                result.update({"vieneu_version": version("vieneu"), "requested_device": requested})
                print("STUDIO_RESULT=" + json.dumps(result, ensure_ascii=False))
                return
            voice_name = config.get("voice", "default")
            voice = model.get_preset_voice(None if voice_name == "default" else voice_name)
            synth_started = time.monotonic()
            result = synthesize_segments(model, config["text"], voice, config["output"])
            synth_seconds = round(time.monotonic() - synth_started, 3)
            result.update({"output": config["output"], "voice": voice_name,
                           "device": backbone_device, "backbone_device": backbone_device,
                           "codec_device": "cpu", "requested_device": requested,
                           "gpu_name": torch.cuda.get_device_name(0) if backbone_device == "cuda" else None,
                           "runtime": f"torch {torch.__version__}",
                           "peak_vram_bytes": int(torch.cuda.max_memory_allocated()) if backbone_device == "cuda" else 0,
                           "vieneu_version": version("vieneu"),
                           "load_seconds": load_seconds, "synth_seconds": synth_seconds,
                           "elapsed_seconds": round(load_seconds + synth_seconds, 3)})
            sidecar = Path(config["output"]).with_suffix(".segments.json")
            sidecar.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        finally:
            model.close()
            if requested == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()
    else:
        raise ValueError("Unsupported worker mode")
    print("STUDIO_RESULT=" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
