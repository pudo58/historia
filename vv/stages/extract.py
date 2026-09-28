from pathlib import Path

from vv import ffmpeg


def run(video: Path, output_dir: Path) -> int:
    return ffmpeg.extract_frames(video, output_dir)

