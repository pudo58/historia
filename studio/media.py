"""Bounded local media operations. Paths come only from the artifact store."""
import hashlib
import json
import math
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any


def ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, RuntimeError) as exc:
        raise ValueError("Thiếu FFmpeg. Cài các dependency local rồi khởi động lại.") from exc


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def probe(path: Path) -> dict[str, Any]:
    import av
    try:
        with av.open(str(path)) as container:
            duration = (container.duration or 0) / av.time_base
            video = next(iter(container.streams.video), None)
            audio = next(iter(container.streams.audio), None)
            def stream_duration(stream):
                return float(stream.duration * stream.time_base) if stream is not None and stream.duration is not None and stream.time_base else None
            return {"duration": duration, "audio_duration": stream_duration(audio),
                    "video_duration": stream_duration(video), "width": video.width if video else None,
                    "height": video.height if video else None,
                    "fps": float(video.average_rate or 0) if video else None,
                    "frames": video.frames if video else None, "audio": audio is not None}
    except (OSError, ValueError) as exc:
        raise ValueError("Không đọc được tệp media; kiểm tra định dạng hoặc tệp bị hỏng.") from exc


def run_ffmpeg(arguments: list[str], timeout: int = 300) -> None:
    process = subprocess.run([ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *arguments],
                             capture_output=True, timeout=timeout, check=False)
    if process.returncode:
        raise ValueError("FFmpeg không xử lý được media. Kiểm tra codec, dung lượng ổ và các tệp đầu vào.")


def measure_loudness(path: Path) -> dict:
    result = subprocess.run([ffmpeg(), '-nostdin', '-hide_banner', '-i', str(path),
        '-vn', '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json',
        '-f', 'null', '-'], capture_output=True, timeout=600, check=False)
    if result.returncode:
        raise ValueError('Không đo được độ lớn tiếng của bản xuất.')
    message = result.stderr.decode('utf-8', errors='replace')
    match = re.search(r'\{\s*"input_i".*?\}', message, re.DOTALL)
    if not match:
        raise ValueError('FFmpeg không trả số đo LUFS/true peak.')
    data = json.loads(match.group())
    return {'integrated_lufs': float(data['input_i']), 'true_peak_dbfs': float(data['input_tp'])}


def reference_frames(path: Path, output_dir: Path) -> list[Path]:
    info = probe(path)
    if not info["width"] or info["duration"] <= 0:
        raise ValueError("Video tham khảo không có hình ảnh hoặc thời lượng hợp lệ.")
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(8):
        target = output_dir / f"reference-{index:02d}.jpg"
        seconds = min(info["duration"] * (index + .5) / 8, max(0, info["duration"] - .1))
        run_ffmpeg(["-ss", str(seconds), "-i", str(path), "-frames:v", "1",
                    "-vf", "scale=1280:720:force_original_aspect_ratio=decrease", str(target)])
        if target.is_file() and target.stat().st_size:
            paths.append(target)
    return paths


def frame_fit(clips, render_size, output_size):
    """Keep native 1080 pixels. Crop only the 16-pixel alignment; upscale everything smaller."""
    width, height = output_size
    tail = ",setsar=1,settb=1/24,setpts=PTS-STARTPTS,fps=24,format=yuv420p"
    render_w, render_h = render_size
    aligned = clips and all(info.get("width") == render_w and info.get("height") == render_h for info in clips)
    if aligned and (render_w, render_h) == (width, height):
        return "none", "null" + tail
    if aligned and 0 <= render_w - width < 16 and 0 <= render_h - height < 16:
        return "crop", f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2" + tail
    return "lanczos", f"scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2" + tail


def shot_count(duration: float, seconds: float = 5.0625) -> int:
    if duration <= 0 or duration > 600:
        raise ValueError("Đoạn lời đọc phải dài hơn 0 và không quá 10 phút.")
    return max(1, math.ceil(duration / seconds))


def scene_clip_count(scene: dict, duration: float) -> int:
    """Local still-image modes cover the entire narration with one video artifact."""
    return 1 if scene.get('motion', 'wan') in {'static', 'kenburns'} else shot_count(duration)


def render_still_clip(image: Path, target: Path, duration: float, size: tuple[int, int],
                      motion: str) -> None:
    if motion not in {'static', 'kenburns'} or not 0 < duration <= 600:
        raise ValueError('Cấu hình clip ảnh tĩnh không hợp lệ.')
    width, height = size
    frames = math.ceil(duration * 24)
    if motion == 'static':
        video_filter = f'scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=decrease,' \
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24'
    else:
        # Fit before zooming so no historical subject is cropped by aspect conversion.
        video_filter = (f'scale={width}:{height}:flags=lanczos:force_original_aspect_ratio=decrease,'
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,'
            f"zoompan=z='min(zoom+0.0007,1.08)':x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':d={frames}:s={width}x{height}:fps=24,setsar=1")
    target.parent.mkdir(parents=True, exist_ok=True)
    _cached_render(target, ['-loop', '1', '-i', str(image), '-an', '-vf', video_filter,
                            '-frames:v', str(frames), '-c:v', 'libx264', '-crf', '18',
                            '-pix_fmt', 'yuv420p'],
                   {'image': digest(image), 'motion': motion, 'duration': duration,
                    'size': size, 'version': 1}, duration)


def extract_last_frame(clip: Path, target: Path) -> None:
    import av
    with av.open(str(clip)) as container:
        last = None
        for frame in container.decode(video=0):
            last = frame
        if last is None:
            raise ValueError('Clip trước không có frame để nối.')
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix('.partial.png')
        last.to_image().save(part)
        part.replace(target)


def render_rife24(source: Path, target: Path, original: Path) -> None:
    """Downsample RIFE 48 fps to 24 while preserving the source shot duration."""
    seconds = probe(original)['duration']
    if seconds <= 0:
        raise ValueError('Clip nguồn RIFE không có thời lượng hợp lệ.')
    target.parent.mkdir(parents=True, exist_ok=True)
    _cached_render(target, ['-i', str(source), '-an', '-vf',
        'fps=24,tpad=stop_mode=clone:stop_duration=0.15', '-t', str(seconds),
        '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p'],
        {'rife_source': digest(source), 'original': digest(original), 'fps': 24, 'version': 1}, seconds)
    measured = probe(target)
    if measured['fps'] is None or abs(measured['fps'] - 24) > .1 or measured['duration'] + .05 < seconds:
        raise ValueError('Clip RIFE 24 fps không phủ đủ thời lượng shot nguồn.')


def srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    return f"{millis//3600000:02d}:{millis//60000%60:02d}:{millis//1000%60:02d},{millis%1000:03d}"


def subtitle_cues(scene: dict, duration: float) -> tuple[list[dict], str]:
    """Timestamps are scene-relative seconds; fallback is explicitly approximate."""
    supplied = scene.get("timestamps")
    if supplied is not None:
        cues, previous = [], 0.0
        for cue in supplied:
            start, end = float(cue["start"]), float(cue["end"])
            text = " ".join(str(cue["text"]).split())
            if not text or not all(math.isfinite(v) for v in (start, end)) or start < previous or end <= start or end > duration + .001:
                raise ValueError("Timestamp phụ đề không hợp lệ hoặc nằm ngoài lời đọc.")
            cues.append({"start": start, "end": end, "text": text})
            previous = end
        if not cues:
            raise ValueError("Timestamp phụ đề rỗng.")
        return cues, "provided_timestamps"
    text = " ".join(str(scene.get("narration", "")).split())
    if not text:
        raise ValueError("Cảnh thiếu lời đọc để tạo phụ đề.")
    chunks = [chunk for sentence in re.split(r"(?<=[.!?;])\s+", text)
              for chunk in textwrap.wrap(sentence, width=84, break_long_words=False, break_on_hyphens=False)]
    total = sum(len(chunk.split()) for chunk in chunks)
    cues, cursor = [], 0.0
    for chunk in chunks:
        end = cursor + duration * len(chunk.split()) / total
        cues.append({"start": cursor, "end": min(duration, end), "text": chunk})
        cursor = end
    return cues, "approximate_text_weighted_not_forced_alignment"


def _cached_render(target, arguments, identity, duration):
    """Atomic checksum checkpoints; a killed encode never becomes reusable."""
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    sidecar = target.with_suffix(".checkpoint.json")
    try:
        saved = json.loads(sidecar.read_text())
        if saved.get("key") == key and target.is_file() and saved.get("sha256") == digest(target):
            return
    except (OSError, ValueError):
        pass
    # Conservative working-space allowance, not a promise of final bitrate.
    if shutil.disk_usage(target.parent).free < max(64 * 1024**2, int(duration * 8_000_000)):
        raise ValueError("Không đủ dung lượng trống để render checkpoint; giữ kết quả đã có.")
    part = target.with_name(target.stem + ".partial" + target.suffix)
    run_ffmpeg([*arguments, str(part)], timeout=1800)
    if not part.is_file() or not part.stat().st_size or probe(part)["duration"] <= 0:
        raise ValueError("Checkpoint render không hợp lệ.")
    part.replace(target)
    temporary = sidecar.with_suffix(".tmp")
    temporary.write_text(json.dumps({"key": key, "sha256": digest(target)}), encoding="utf-8")
    temporary.replace(sidecar)


def render_film(scenes: list[dict], output_dir: Path, music: Path | None = None,
                quality: str = "final", project_settings: dict | None = None,
                *, render_profile=None, output_resolution=None, aspect_ratio=None,
                transition=None, upscale_method=None) -> dict[str, Path]:
    """Never loop/stretch a shot. Trim excess video to measured speech duration."""
    if quality not in {"draft", "final"}:
        raise ValueError("Chất lượng export phải là draft hoặc final.")
    from studio.formats import resolve_format
    fmt = resolve_format(quality, project_settings, render_profile=render_profile,
                         output_resolution=output_resolution, aspect_ratio=aspect_ratio,
                         transition=transition, upscale_method=upscale_method)
    if fmt["upscale_method"] == "ai":
        raise ValueError("AI upscale chưa có model/version/checksum và benchmark được duyệt; không tự thay bằng resize.")
    width, height = fmt["output_size"]
    output_dir.mkdir(parents=True, exist_ok=True)
    segments, subtitles, time_cursor = [], [], 0.0
    alignment, sources, boundaries, scene_durations, fits = [], [], [], [], []
    previous_source, previous_duration, previous_handle = None, 0, False
    handle = 8 / 24  # 0.333 seconds, exact frame count; no audio overlap.
    for index, scene in enumerate(scenes):
        audio = Path(scene["audio"])
        audio_info = probe(audio)
        duration = audio_info.get("audio_duration") or audio_info["duration"]
        if not audio_info.get("audio") or not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"Cảnh {index+1} không có luồng audio/thời lượng hợp lệ.")
        clips = [Path(p) for p in scene["clips"]]
        clip_info = [probe(p) for p in clips]
        if any(not info.get("width") or not info.get("height") or not math.isfinite(info["duration"]) or info["duration"] <= 0 for info in clip_info):
            raise ValueError(f"Cảnh {index+1} có clip thiếu luồng video/thời lượng hợp lệ.")
        available = sum(info.get("video_duration") or info["duration"] for info in clip_info)
        sources.extend({"width": info["width"], "height": info["height"]} for info in clip_info)
        if available + .05 < duration:
            raise ValueError(f"Cảnh {index+1} chưa có đủ clip để phủ lời đọc; cần tạo thêm shot.")
        cues, mode = subtitle_cues(scene, duration)
        # The concat list contains only app-owned hexadecimal artifact filenames.
        listing = output_dir / f"shots-{index}.txt"
        listing.write_text("\n".join("file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'" for p in clips), encoding="utf-8")
        fit, normalization = frame_fit(clip_info, fmt["render_size"], (width, height))
        fits.append(fit)
        has_handle = available >= duration + handle + 1/24
        source_duration = duration + (handle + 1/24 if has_handle else 0)
        hashes = [digest(p) for p in clips]
        target = output_dir / f"scene-{index:04d}.mkv"
        # Natural avoids dissolving action unless the caller marks a setting/chapter change.
        wanted = fmt["transition"] == "dissolve" or (fmt["transition"] == "natural" and
                 bool(scene.get("transition_boundary") or (index and scene.get("chapter_id") != scenes[index-1].get("chapter_id"))))
        dissolve = bool(index and wanted and previous_handle and duration >= handle)
        next_scene = scenes[index+1] if index+1 < len(scenes) else None
        next_wants = bool(next_scene and (fmt['transition'] == 'dissolve' or
            (fmt['transition'] == 'natural' and (next_scene.get('transition_boundary') or
             next_scene.get('chapter_id') != scene.get('chapter_id')))))
        need_source = dissolve or (has_handle and next_wants)
        source = output_dir / f"source-{index:04d}.mkv" if need_source else None
        if source:
            _cached_render(source, ["-f", "concat", "-safe", "0", "-i", str(listing),
                           "-an", "-vf", normalization, "-c:v", "ffv1", "-level", "3",
                           "-t", str(source_duration)], {"clips": hashes, "filter": normalization,
                           "duration": source_duration, "version": 2}, source_duration)
            args = ["-i", str(source), "-i", str(audio)]
        else:
            args = ["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(audio),
                    "-vf", normalization]
        if dissolve:
            args += ["-ss", str(previous_duration), "-t", str(handle), "-i", str(previous_source),
                     "-filter_complex_threads", "1", "-filter_complex",
                     f"[2:v]settb=1/24,setpts=PTS-STARTPTS,fps=24[p];[0:v]settb=1/24,setpts=PTS-STARTPTS,fps=24[c];[p][c]xfade=transition=fade:duration={handle}:offset=0[v]",
                     "-map", "[v]"]
        else:
            args += ["-map", "0:v:0"]
        args += ["-map", "1:a:0", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-t", str(duration)]
        _cached_render(target, args, {"source": digest(source) if source else hashes,
                       'filter': normalization if not source else None, "audio": digest(audio),
                       "previous": digest(previous_source) if dissolve else None,
                       "previous_duration": previous_duration if dissolve else None,
                       "duration": duration, "dissolve": dissolve, "version": 2}, duration)
        if index:
            boundaries.append({"scene": index+1, "at": time_cursor, "effect": "dissolve" if dissolve else "hard_cut",
                               "seconds": handle if dissolve else 0,
                               "reason": "extra_tail_handle" if dissolve else "not_requested_or_insufficient_handle"})
        previous_source, previous_duration, previous_handle = source, duration, bool(source and has_handle)
        segments.append(target)
        scene_durations.append(duration)
        alignment.append({"scene": index + 1, "alignment": mode})
        for cue in cues:
            text = "\n".join(textwrap.wrap(cue["text"], width=42, break_long_words=False, break_on_hyphens=False))
            subtitles.append(f"{len(subtitles)+1}\n{srt_time(time_cursor+cue['start'])} --> {srt_time(time_cursor+cue['end'])}\n{text}\n")
        time_cursor += duration
    if not segments:
        raise ValueError("Dự án chưa có cảnh để xuất.")
    # Bounded groups (at most 12 scenes), reusable even for hundreds of shots.
    chapters = []
    for group in range(0, len(segments), 12):
        subset = segments[group:group+12]
        chapter = output_dir / f"chapter-{group//12:04d}.mkv"
        chapter_list = output_dir / f"chapter-{group//12:04d}.txt"
        chapter_list.write_text("\n".join(f"file '{p.name}'\nduration {d:.9f}" for p, d in
                                           zip(subset, scene_durations[group:group+12])), encoding="utf-8")
        _cached_render(chapter, ["-f", "concat", "-safe", "1", "-i", str(chapter_list), "-c", "copy"],
                       {"scenes": [digest(p) for p in subset], "durations": scene_durations[group:group+12], "version": 2},
                       sum(probe(p)["duration"] for p in subset))
        chapters.append(chapter)
    listing = output_dir / "segments.txt"
    listing.write_text("\n".join(f"file '{p.name}'\nduration {sum(scene_durations[i*12:(i+1)*12]):.9f}"
                                 for i, p in enumerate(chapters)), encoding="utf-8")
    film = output_dir / "film.mp4"
    args = ["-f", "concat", "-safe", "1", "-i", str(listing)]
    duck = (project_settings or {}).get('audio_mix_profile') == 'voice_duck_v1'
    if music:
        mix = ('[1:a]volume=0.12[bg];[bg][0:a]sidechaincompress='
               'threshold=0.02:ratio=8:attack=20:release=300[duck];'
               '[0:a][duck]amix=inputs=2:duration=first:normalize=0[mix];'
               '[mix]loudnorm=I=-16:TP=-1.5:LRA=11[a]' if duck else
               '[1:a]volume=0.12[bg];[0:a][bg]amix=inputs=2:duration=first[a]')
        args += ["-stream_loop", "-1", "-i", str(music), "-filter_complex",
                 mix,
                 "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac"]
    else:
        args += ["-c:v", "copy", "-c:a", "aac"]
    _cached_render(film, [*args, "-t", str(time_cursor), "-movflags", "+faststart"],
                   {"chapters": [digest(p) for p in chapters], "music": digest(music) if music else None,
                    "duration": time_cursor, "mix": 'voice_duck_v1' if duck and music else 'legacy',
                    "version": 2}, time_cursor)
    result = probe(film)
    if not result.get("audio") or not result.get("width") or not math.isfinite(result["duration"]) or abs(result["duration"] - time_cursor) > max(.5, len(scenes)*.05):
        raise ValueError("Kiểm tra xuất phim thất bại: thiếu luồng hình/tiếng hoặc thời lượng không khớp.")
    for stream in ("audio_duration", "video_duration"):
        measured = result.get(stream)
        if measured is not None and (not math.isfinite(measured) or abs(measured-time_cursor) > max(.25, len(scenes)*.05)):
            raise ValueError("Kiểm tra xuất phim thất bại: thời lượng luồng hình/tiếng không khớp lời đọc.")
    if (result.get("width"), result.get("height")) != (width, height):
        raise ValueError("Kiểm tra xuất phim thất bại: kích thước export không khớp.")
    subtitle = output_dir / "subtitles.srt"
    subtitle.write_text("\n".join(subtitles), encoding="utf-8-sig")
    metadata = output_dir / "export-metadata.json"
    loudness = measure_loudness(film) if duck and music else None
    metadata.write_text(json.dumps({"export_quality": quality, "export_resolution": {"width": width, "height": height},
                                    "source_render_resolutions": sources, "duration_seconds": result["duration"],
                                    "subtitle_alignment": alignment, "format": fmt,
                                    "resize_method": fits[0] if fits and all(item == fits[0] for item in fits) else "mixed",
                                    "ai_upscale_model": None,
                                    "quality_claim": "native_16px_lattice_cropped_to_1080" if fits and set(fits) <= {"crop", "none"} else "resize_only_not_native_or_ai_enhancement",
                                    "transitions": boundaries, "checkpoint_group_size": 12,
                                    "audio_mix_profile": 'voice_duck_v1' if duck and music else 'legacy',
                                    "loudness": loudness,
                                    "notice": "1080p standard renders the nearest multiple of 16, then center-crops to the exact frame. Smaller sources are still ordinary Lanczos resizes. Approximate subtitles are not forced alignment."},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    return {"video": film, "subtitles": subtitle, "metadata": metadata}
