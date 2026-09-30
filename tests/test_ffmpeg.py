
import pytest

from vv.ffmpeg import FFmpegError, _run


def test_missing_executable_has_actionable_error(monkeypatch):
    monkeypatch.setattr("vv.ffmpeg.subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(FFmpegError, match="Required executable was not found"):
        _run(["ffprobe", "missing.mp4"])

