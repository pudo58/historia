from pathlib import Path

from vv.config import load_config


def test_load_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("runs_dir: custom\nprocess_fps: native\nencode:\n  crf: 20\n")
    config = load_config(path)
    assert config.runs_dir == Path("custom")
    assert config.encode.crf == 20

