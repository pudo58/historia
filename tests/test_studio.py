import io
import json
import sqlite3
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from studio.media import probe, reference_frames, render_film, run_ffmpeg, shot_count
from studio.models import Artifact, Job, Scene
from studio.packs import graph_for


@pytest.fixture
def studio(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'studio.db'}", studio_root=tmp_path / "data"),
                     SecretStore("unit-test-key"), lambda host, secret: FakeExecutor())
    with TestClient(app) as client:
        yield client, app.state.studio, app.state.studio_jobs


def project(client):
    result = client.post("/api/studio/projects", json={"title": "Phim kiểm thử", "topic": "Chủ đề thử nghiệm"})
    assert result.status_code == 201, result.text
    return result.json()["id"]


def scene(client, pid, **fields):
    result = client.post(f"/api/studio/projects/{pid}/scenes", json={"title": "Cảnh 1", "narration": "Lời đọc thử", **fields})
    assert result.status_code == 201, result.text
    return result.json()


def test_project_persists_after_restart(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'persist.db'}", studio_root=tmp_path / "data")
    app = create_app(settings, SecretStore("test"), lambda h, s: FakeExecutor())
    with TestClient(app) as client:
        pid = project(client)
        scene(client, pid)
    second = create_app(settings, SecretStore("test"), lambda h, s: FakeExecutor())
    with TestClient(second) as client:
        assert len(client.get(f"/api/studio/projects/{pid}").json()["scenes"]) == 1


def test_backup_preserves_old_database(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE keep_me (value TEXT)")
        connection.execute("INSERT INTO keep_me VALUES ('preserve')")
    create_app(Settings(database_url=f"sqlite:///{db}"), SecretStore("test"), lambda h, s: FakeExecutor())
    backups = list((tmp_path / "backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT value FROM keep_me").fetchone() == ("preserve",)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT value FROM keep_me").fetchone() == ("preserve",)


def test_citation_must_be_original_and_historical(studio):
    client, _, _ = studio
    pid = project(client)
    sid = client.post(f"/api/studio/projects/{pid}/sources", json={"title": "Nguồn", "text": "Đoạn nguyên văn"}).json()["id"]
    result = client.post(f"/api/studio/projects/{pid}/scenes", json={"title": "Cảnh", "citations": [{"source_id": sid, "quote": "Bịa thêm"}]})
    assert result.status_code == 409
    wrapped = client.post(f"/api/studio/projects/{pid}/sources", json={"title": "Xuống dòng", "text": "Đoạn\nnguyên   văn"}).json()["id"]
    assert scene(client, pid, citations=[{"source_id": wrapped, "quote": "Đoạn nguyên văn"}])["warnings"] == []
    saved = scene(client, pid, citations=[{"source_id": sid, "quote": "Đoạn nguyên văn"}])
    assert not saved["warnings"]
    other = project(client)
    result = client.post(f"/api/studio/projects/{other}/scenes", json={"title": "Sai dự án", "citations": [{"source_id": sid, "quote": "Đoạn nguyên văn"}]})
    assert result.status_code == 409


def test_approval_requires_review_and_optimistic_revision(studio):
    client, _, _ = studio
    pid = project(client)
    s = scene(client, pid)
    url = f"/api/studio/scenes/{s['id']}/approve"
    assert client.post(url, json={"target": "script", "revision": s["revision"]}).status_code == 409
    s = scene(client, pid, review_note="Người biên tập đã kiểm chứng riêng.")
    url = f"/api/studio/scenes/{s['id']}/approve"
    result = client.post(url, json={"target": "script", "revision": s["revision"]})
    assert result.status_code == 200
    assert result.json()["script_approved"]
    assert client.post(url, json={"target": "script", "revision": s["revision"]}).status_code == 409


def test_narration_change_keeps_image_but_invalidates_speech(studio):
    client, service, _ = studio
    pid = project(client)
    s = scene(client, pid)
    with service.sessions() as session:
        row = session.get(Scene, s["id"])
        row.data = {**row.data, "keyframe_id": "keep", "keyframe_approved": True, "speech_id": "old", "duration": 5, "clip_approved": True}
        session.commit()
    body = {k: s[k] for k in ["title", "chapter", "narration", "visual_prompt", "camera", "character_ids", "reference_ids", "citations", "seed", "steps", "review_note", "revision"]}
    body["narration"] = "Lời đọc mới"
    result = client.patch(f"/api/studio/scenes/{s['id']}", json=body)
    assert result.status_code == 200
    assert result.json()["keyframe_id"] == "keep"
    assert "speech_id" not in result.json()
    assert "duration" not in result.json()
    assert not result.json()["clip_approved"]


def test_pdf_scan_is_explicitly_unreadable(studio):
    client, _, _ = studio
    pid = project(client)
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    stream = io.BytesIO()
    writer.write(stream)
    result = client.post(f"/api/studio/projects/{pid}/upload?name=scan.pdf", content=stream.getvalue(), headers={"content-type": "application/octet-stream"})
    assert result.status_code == 201, result.text
    assert result.json()[0]["status"] == "needs_ocr"
    assert not result.json()[0]["selected"]


def test_image_is_visual_and_checksum_detects_tampering(studio):
    client, service, _ = studio
    pid = project(client)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32)).save(buffer, format="PNG")
    result = client.post(f"/api/studio/projects/{pid}/upload?name=ref.png&role=historical", content=buffer.getvalue(), headers={"content-type": "application/octet-stream"})
    assert result.status_code == 201
    value = result.json()[0]
    assert value["role"] == "visual"
    path = service.artifact_path(value["artifact_id"])
    path.write_bytes(b"tampered")
    assert client.get(f"/api/studio/artifacts/{value['artifact_id']}/file").status_code == 409


@pytest.mark.parametrize("name,body,headers,expected", [
    ("script.html", b"<script>alert(1)</script>", {"content-type": "application/octet-stream"}, 415),
    ("bad.png", b"not an image", {"content-type": "application/octet-stream"}, 422),
    ("empty.txt", b"", {"content-type": "application/octet-stream"}, 422),
    ("text.txt", b"text", {"content-type": "text/plain"}, 415),
    ("text.txt", b"text", {"content-type": "application/octet-stream", "origin": "https://evil.example"}, 403),
])
def test_upload_boundary(studio, name, body, headers, expected):
    client, _, _ = studio
    pid = project(client)
    assert client.post(f"/api/studio/projects/{pid}/upload?name={name}", content=body, headers=headers).status_code == expected


def test_render_is_not_reported_ready_without_gpu(studio):
    client, _, _ = studio
    pid = project(client)
    s = scene(client, pid, review_note="Đã kiểm chứng")
    client.post(f"/api/studio/scenes/{s['id']}/approve", json={"target": "script", "revision": 1})
    result = client.post(f"/api/studio/projects/{pid}/jobs", json={"kind": "keyframe", "scene_id": s["id"]})
    assert result.status_code == 409
    assert client.get("/api/studio/jobs").json() == []
    assert not client.get("/api/studio/status").json()["ai_ready"]


def test_recovery_retains_remote_prompt_and_blocks_edit(studio):
    client, service, jobs = studio
    pid = project(client)
    with service.sessions() as session:
        row = Job(project_id=pid, kind="clip", status="running", input_hash="a"*64, snapshot={},
                  result={"submissions": {"clip-0": {"prompt_id": "keep-this-id"}}})
        session.add(row)
        session.commit()
        id = row.id
    jobs.recover()
    recovered = service.require(Job, id)
    assert recovered.status == "reconciling"
    assert recovered.result["submissions"]["clip-0"]["prompt_id"] == "keep-this-id"
    assert client.post(f"/api/studio/projects/{pid}/scenes", json={"title": "Blocked"}).status_code == 409


def test_queue_cancel_survives_reload(studio):
    client, service, _ = studio
    pid = project(client)
    with service.sessions() as session:
        row = Job(project_id=pid, kind="clip", status="interrupted", input_hash="b"*64, snapshot={})
        session.add(row)
        session.commit()
        id = row.id
    assert client.get("/api/studio/jobs").json()[0]["status"] == "interrupted"
    # Exercise queued cancellation without a background dispatch race.
    service.require(Job, id)
    with service.sessions() as session:
        job = session.get(Job, id)
        job.status = "queued"
        session.commit()
        # TestClient worker can dispatch; use a reconciling host-independent blocker.
        block = Job(project_id=pid, kind="clip", status="reconciling", input_hash="c"*64, snapshot={})
        session.add(block)
        session.commit()
    assert client.post(f"/api/studio/jobs/{id}/cancel", json={}).json()["status"] == "cancelled"


def make_media(directory: Path):
    audio = directory / "voice.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000)
    video = directory / "shot.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=320x180:r=16:d=1.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)])
    return audio, video


def test_actual_local_export_and_reference_extraction(tmp_path):
    audio, video = make_media(tmp_path)
    frames = reference_frames(video, tmp_path / "frames")
    assert len(frames) == 8
    outputs = render_film([{"audio": str(audio), "clips": [str(video)], "narration": "Thử phụ đề tiếng Việt"}], tmp_path / "out")
    info = probe(outputs["video"])
    assert info["width"] == 1280 and info["height"] == 720
    assert info["audio"] and abs(info["duration"] - 1) < .5
    assert "Thử phụ đề tiếng Việt" in outputs["subtitles"].read_text(encoding="utf-8-sig")
    assert shot_count(12) == 3


def test_export_refuses_insufficient_video(tmp_path):
    audio, _ = make_media(tmp_path)
    with pytest.raises(ValueError, match="đủ clip"):
        render_film([{"audio": str(audio), "clips": [], "narration": "x"}], tmp_path / "out")


def test_export_queue_writes_verified_artifacts(studio):
    client, service, _ = studio
    pid = project(client)
    s = scene(client, pid, review_note="Đã kiểm chứng")
    directory = service.job_directory("12345678-abcd")
    audio, video = make_media(directory)
    audio_id = service.artifact(audio, pid, "speech.wav")["id"]
    video_id = service.artifact(video, pid, "shot.mp4")["id"]
    with service.sessions() as session:
        row = session.get(Scene, s["id"])
        row.data = {**row.data, "speech_id": audio_id, "clip_ids": [video_id], "clip_approved": True, "script_approved": True}
        session.commit()
    result = client.post(f"/api/studio/projects/{pid}/jobs", json={"kind": "export"})
    assert result.status_code == 201, result.text
    id = result.json()["id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = service.require(Job, id)
        if job.status in {"completed", "failed"}:
            break
        time.sleep(.05)
    assert job.status == "completed", job.error
    # Export includes explicit source/export resolution and subtitle alignment metadata.
    assert len(job.result["artifact_ids"]) == 5
    metadata_paths = [service.artifact_path(aid) for aid in job.result["artifact_ids"]]
    metadata = [json.loads(path.read_text(encoding="utf-8")) for path in metadata_paths
                if path.suffix == '.json']
    assert any('source_render_resolutions' in item and 'subtitle_alignment' in item
               for item in metadata)
    for id in job.result["artifact_ids"]:
        assert service.artifact_path(id).is_file()
        assert client.get(f"/api/studio/artifacts/{id}/file?download=true").status_code == 200


def test_graph_is_allowlisted_and_seed_is_stable():
    graph = graph_for("wan_i2v", "camera pan", 42, "final", ["input.png"], "studio/test", 1)
    assert graph["98"]["inputs"]["width"] == 1280
    assert graph["86"]["inputs"]["steps"] == 2
    assert graph["86"]["inputs"]["noise_seed"] == 42
    with pytest.raises(ValueError):
        graph_for("arbitrary", "", 1, "draft", [], "x")


def test_artifact_path_cannot_escape_storage(studio):
    _, service, _ = studio
    outside = service.root.parent / "outside.txt"
    outside.write_text("secret")
    with service.sessions() as session:
        artifact = Artifact(relative_path="../outside.txt", sha256="a"*64, name="outside.txt", media_type="text/plain")
        session.add(artifact)
        session.commit()
        id = artifact.id
    with pytest.raises(ValueError, match="local"):
        service.artifact_path(id)
