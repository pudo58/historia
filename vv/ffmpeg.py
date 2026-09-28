import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any


class FFmpegError(RuntimeError):
    pass


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise FFmpegError(f"Required executable was not found: {command[0]}") from exc
    if result.returncode:
        raise FFmpegError(f"Command failed ({result.returncode}): {' '.join(command)}\n{result.stderr}")
    return result


def probe(path: Path) -> dict[str, Any]:
    result = _run([
        "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)
    ])
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"No video stream found in {path}")
    rate = Fraction(video.get("avg_frame_rate", "0/1"))
    return {
        "path": str(path),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": float(rate),
        "fps_num": rate.numerator,
        "fps_den": rate.denominator,
        "frames": int(video["nb_frames"]) if video.get("nb_frames") else None,
        "duration": float(video.get("duration") or data.get("format", {}).get("duration", 0)),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def extract_frames(video: Path, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-i", str(video), "-vsync", "0", str(output_dir / "%08d.png")])
    return len(list(output_dir.glob("*.png")))


def encode_frames(frames_dir: Path, source_video: Path, output: Path, metadata: dict[str, Any], crf: int = 18) -> None:
    frame_count = len(list(frames_dir.glob("*.png")))
    if not frame_count:
        raise FFmpegError(f"No PNG frames found in {frames_dir}")
    output.parent.mkdir(parents=True, exist_ok=True)
    fps = f"{metadata['fps_num']}/{metadata['fps_den']}"
    _run([
        "ffmpeg", "-y", "-framerate", fps, "-i", str(frames_dir / "%08d.png"),
        "-i", str(source_video), "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264",
        "-crf", str(crf), "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-c:a", "copy",
        "-metadata:s:v:0", "color_primaries=bt709", "-metadata:s:v:0", "color_transfer=bt709",
        "-metadata:s:v:0", "colorspace=bt709", str(output),
    ])
    encoded = probe(output)
    if encoded["frames"] is not None and encoded["frames"] != frame_count:
        raise FFmpegError(f"Frame-count mismatch: input frames={frame_count}, output frames={encoded['frames']}")

