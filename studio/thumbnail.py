"""Small, on-demand JPEG previews of verified local video artifacts."""
import hashlib
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from studio.media import ffmpeg

# A fixed number of locks prevents duplicate decodes without a growing lock registry.
_LOCKS = tuple(threading.Lock() for _ in range(32))
_MAX_JPEG_BYTES = 2_000_000


def video_thumbnail(source: Path, root: Path, checksum: str) -> Path:
    """Extract a bounded still into an app-owned cache, never beside the artifact."""
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Checksum artifact không hợp lệ.")
    cache = root / "thumbnail-cache"
    cache.mkdir(parents=True, exist_ok=True)
    if cache.is_symlink() or not cache.resolve().is_relative_to(root.resolve()):
        raise ValueError("Thư mục thumbnail không hợp lệ.")
    target = cache / f"video-v1-{checksum}.jpg"
    lock = _LOCKS[int(checksum[:2], 16) % len(_LOCKS)]
    with lock:
        if target.is_symlink():
            raise ValueError("Thumbnail cache không hợp lệ.")
        if _valid_jpeg(target):
            return target
        # mkstemp provides a unique name for concurrent workers and keeps the
        # temporary output on the same filesystem for an atomic final replace.
        fd, name = tempfile.mkstemp(prefix="thumbnail-", suffix=".jpg", dir=cache)
        os.close(fd)
        temporary = Path(name)
        try:
            try:
                result = subprocess.run(
                    [ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-threads", "1", "-ss", "0.5", "-i", str(source), "-an", "-sn", "-dn",
                     "-frames:v", "1", "-vf",
                     "scale=480:270:force_original_aspect_ratio=decrease:force_divisible_by=2",
                     "-q:v", "4", "-f", "image2", "-y", str(temporary)],
                    capture_output=True, timeout=15, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError("Video không thể tạo thumbnail trong thời gian cho phép.") from exc
            if result.returncode or not _valid_jpeg(temporary):
                raise ValueError("Video không có frame hợp lệ để tạo thumbnail.")
            temporary.replace(target)
            return target
        finally:
            temporary.unlink(missing_ok=True)


def _valid_jpeg(path: Path) -> bool:
    if not path.is_file() or path.is_symlink() or not 4 <= path.stat().st_size <= _MAX_JPEG_BYTES:
        return False
    with path.open("rb") as handle:
        return handle.read(3) == b"\xff\xd8\xff"
