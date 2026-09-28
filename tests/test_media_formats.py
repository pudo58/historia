"""CPU-only synthetic acceptance; no model quality/GPU claims."""
import json
import pytest
from studio.formats import resolve_format
from studio.media import render_film, run_ffmpeg, probe

@pytest.mark.parametrize("ratio", ["16:9", "9:16", "1:1", "4:5"])
@pytest.mark.parametrize("resolution", ["720p", "1080p", "1440p"])
@pytest.mark.parametrize("profile", ["draft", "standard"])
def test_geometry(ratio, resolution, profile):
    fmt = resolve_format(project_settings={"aspect_ratio": ratio, "output_resolution": resolution, "render_profile": profile})
    w, h = fmt["render_size"]
    a, b = map(int, ratio.split(":"))
    assert w % 16 == h % 16 == 0
    ow, oh = fmt["output_size"]
    assert ow * b == oh * a
    assert min(ow, oh) == int(resolution[:-1])
    if profile == "standard" and resolution == "1080p":
        assert (w, h) == ((ow + 15) // 16 * 16, (oh + 15) // 16 * 16)
        assert w >= ow and h >= oh
    else:
        assert w * b == h * a
        assert w * h <= (832 * 480 if profile == "draft" else 1280 * 720)


def test_legacy_and_ai_closed(tmp_path):
    assert resolve_format("draft")["render_size"] == (832, 480)
    assert resolve_format("final")["output_size"] == (1280, 720)
    with pytest.raises(ValueError, match="AI upscale"):
        render_film([], tmp_path, upscale_method="ai")

@pytest.mark.parametrize("spare,effect", [(0, "hard_cut"), (.6, "dissolve")])
def test_transition_handles_and_resume(tmp_path, monkeypatch, spare, effect):
    import studio.media as media
    audio = tmp_path / "speech.wav"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(audio)])
    scenes = []
    for i, color in enumerate(["red", "blue"]):
        clip = tmp_path / f"clip{i}.mp4"
        run_ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s=160x160:r=24:d={1+spare}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)])
        scenes.append({"audio": audio, "clips": [clip], "narration": "Hello."})
    out = tmp_path / "export"
    result = render_film(scenes, out, output_resolution="720p", aspect_ratio="1:1", transition="dissolve")
    metadata = json.loads(result["metadata"].read_text())
    assert metadata["transitions"][0]["effect"] == effect
    assert metadata["resize_method"] == "lanczos"
    assert probe(result["video"])["duration"] == pytest.approx(2, abs=.09)
    assert "00:00:01,000 --> 00:00:02,000" in result["subtitles"].read_text(encoding="utf-8-sig")
    original = media.run_ffmpeg
    calls = []
    def tracked(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(media, "run_ffmpeg", tracked)
    render_film(scenes, out, output_resolution="720p", aspect_ratio="1:1", transition="dissolve")
    assert not calls
    (out / "scene-0001.mkv").write_bytes(b"damaged")
    render_film(scenes, out, output_resolution="720p", aspect_ratio="1:1", transition="dissolve")
    assert calls


def test_native_1080_crops_the_16px_edge(tmp_path):
    audio = tmp_path / "speech.wav"
    clip = tmp_path / "clip.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(audio)])
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=1088x1920:r=24:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)])
    result = render_film([{"audio": audio, "clips": [clip], "narration": "Xin chào."}], tmp_path / "out",
                         render_profile="standard", output_resolution="1080p", aspect_ratio="9:16", transition="none")
    metadata = json.loads(result["metadata"].read_text(encoding="utf-8"))
    assert metadata["resize_method"] == "crop"
    assert metadata["format"]["render_size"] == [1088, 1920]
    video = probe(result["video"])
    assert (video["width"], video["height"]) == (1080, 1920)


def test_graphs_share_geometry():
    from studio.packs import graph_for
    settings = {"aspect_ratio": "9:16", "render_profile": "draft"}
    size = resolve_format(project_settings=settings)["render_size"]
    for name, node in [("qwen_image", "58"), ("wan_i2v", "98"), ("qwen_edit", "93")]:
        graph = graph_for(name, "test", 1, "final", ["ref.png"], "test", project_settings=settings)
        assert (graph[node]["inputs"]["width"], graph[node]["inputs"]["height"]) == size
