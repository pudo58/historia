"""Speech device policy. Missing fields keep the historical CPU behavior."""

DEVICES = ("cuda", "cpu")


def snapshot_tts_device(project) -> str:
    """A stored job or production snapshot without the field stays on CPU."""
    value = (project or {}).get("tts_device")
    return value if value in DEVICES else "cpu"


def new_tts_device(project) -> str:
    """A new submission defaults to CUDA unless the user explicitly chose CPU."""
    value = (project or {}).get("tts_device")
    return value if value in DEVICES else "cuda"
