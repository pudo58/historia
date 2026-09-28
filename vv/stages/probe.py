from pathlib import Path

from vv import ffmpeg


def run(video: Path) -> dict:
    return ffmpeg.probe(video)

