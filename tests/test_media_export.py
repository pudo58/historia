"""Local synthetic media checks, not evidence of real GPU acceptance."""
import json

import pytest

from studio.media import probe, render_film, run_ffmpeg, subtitle_cues


def test_approximate_subtitles_split_sentences():
    cues, alignment = subtitle_cues({"narration": "Câu đầu tiên. Câu tiếp theo!"}, 8)
    assert len(cues) == 2
    assert cues[0]["start"] == 0
    assert cues[-1]["end"] == pytest.approx(8)
    assert "approximate" in alignment


def test_supplied_timestamps_are_preserved():
    timestamps = [{"start": .5, "end": 2, "text": "Xin chào."}]
    cues, alignment = subtitle_cues({"timestamps": timestamps}, 3)
    assert cues == timestamps
    assert alignment == "provided_timestamps"


@pytest.mark.parametrize("timestamps", [[], [{"start": 2, "end": 1, "text": "Sai"}], [{"start": 0, "end": 5, "text": "Sai"}], [{"start": 0, "end": 2, "text": "A"}, {"start": 1, "end": 3, "text": "B"}]])
def test_invalid_timestamps_rejected(timestamps):
    with pytest.raises(ValueError):
        subtitle_cues({"timestamps": timestamps}, 3)


@pytest.mark.parametrize("quality,size", [("draft", (832, 480)), ("final", (1280, 720))])
def test_synthetic_export_quality_streams_and_alignment(tmp_path, quality, size):
    audio, clip = tmp_path / "speech.wav", tmp_path / "shot.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(audio)])
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=832x480:r=24:d=1.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)])
    outputs = render_film([{"audio": audio, "clips": [clip], "narration": "Câu một. Câu hai."}], tmp_path / quality, quality=quality)
    info = probe(outputs["video"])
    assert (info["width"], info["height"]) == size
    assert info["audio"] and abs(info["duration"] - 1) < .1
    metadata = json.loads(outputs["metadata"].read_text(encoding="utf-8"))
    assert metadata["source_render_resolutions"] == [{"width": 832, "height": 480}]
    assert "approximate" in metadata["subtitle_alignment"][0]["alignment"]
    assert outputs["subtitles"].read_text(encoding="utf-8-sig").count(" --> ") == 2


def test_multiscene_timestamps_and_legacy_default(tmp_path):
    audio, clip = tmp_path / "speech.wav", tmp_path / "shot.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(audio)])
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=832x480:r=24:d=1.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)])
    scene = {"audio": audio, "clips": [clip], "narration": "Xin chào.",
             "timestamps": [{"start": .1, "end": .9, "text": "Xin chào."}]}
    outputs = render_film([scene, scene], tmp_path / "film")
    info = probe(outputs["video"])
    assert (info["width"], info["height"]) == (1280, 720)
    assert abs(info["duration"] - 2) < .1
    srt = outputs["subtitles"].read_text(encoding="utf-8-sig")
    assert "00:00:00,100 --> 00:00:00,900" in srt
    assert "00:00:01,100 --> 00:00:01,900" in srt


def test_export_rejects_missing_stream_or_short_coverage(tmp_path, monkeypatch):
    import studio.media as media
    scene = {"audio": "audio.wav", "clips": ["clip.mp4"], "narration": "Lời đọc"}
    monkeypatch.setattr(media, "probe", lambda p: {"duration": 2, "audio": False})
    with pytest.raises(ValueError, match="audio"):
        render_film([scene], tmp_path)
    monkeypatch.setattr(media, "probe", lambda p: {"duration": 2, "audio": True} if p.suffix == ".wav" else {"duration": 1, "width": 832, "height": 480})
    with pytest.raises(ValueError, match="đủ clip"):
        render_film([scene], tmp_path)
