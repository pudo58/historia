from pathlib import Path
from typing import Any

from vv import ffmpeg


def run(frames_dir: Path, source_video: Path, output: Path, metadata: dict[str, Any], crf: int) -> None:
    ffmpeg.encode_frames(frames_dir, source_video, output, metadata, crf)

