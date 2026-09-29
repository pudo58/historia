"""Read-only video artifact thumbnails, including checksum and cache boundaries."""
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from studio.media import digest, run_ffmpeg
from studio.models import Artifact


@pytest.fixture
def studio(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'studio.db'}", studio_root=tmp_path / "data"),
                     SecretStore("thumbnail-test-key"), lambda host, secret: FakeExecutor())
    with TestClient(app) as client:
        yield client, app.state.studio


def artifact(service, name: str, content: bytes | None = None) -> tuple[str, Path]:
    path = service.root / name
    if content is not None:
        path.write_bytes(content)
    return service.artifact(path, None, name)["id"], path


def test_project_list_points_at_saved_clip_for_preview(studio):
    client, service = studio
    project = client.post('/api/studio/projects', json={'title':'Bạch Đằng', 'topic':'Phim'}).json()
    video = service.root / 'saved-clip.mp4'
    run_ffmpeg(['-f','lavfi','-i','color=c=blue:s=64x48:d=1','-c:v','mpeg4',str(video)], timeout=15)
    artifact_id = service.artifact(video, project['id'], 'clip-0.mp4')['id']
    listed = next(row for row in client.get('/api/studio/projects').json() if row['id'] == project['id'])
    assert listed['thumbnail_clip_id'] == artifact_id
    assert client.get(f'/api/studio/artifacts/{artifact_id}/thumbnail').status_code == 200


def test_video_frame_is_cached_without_touching_source_or_gpu(studio, monkeypatch):
    client, service = studio
    video = service.root / "clip.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=64x48:r=2:d=2", "-c:v", "mpeg4", str(video)], timeout=15)
    id, source = artifact(service, "clip.mp4")
    original = source.read_bytes()
    import studio.thumbnail as thumbnail
    real_run = thumbnail.subprocess.run
    calls = []

    def local_only(args, **kwargs):
        calls.append(args)
        assert "-threads" in args and args[args.index("-threads") + 1] == "1"
        assert kwargs["timeout"] <= 15
        return real_run(args, **kwargs)

    monkeypatch.setattr(thumbnail.subprocess, "run", local_only)
    with patch.object(service, "job_directory", side_effect=AssertionError("GPU job touched")):
        first = client.get(f"/api/studio/artifacts/{id}/thumbnail")
        second = client.get(f"/api/studio/artifacts/{id}/thumbnail")
    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"
    assert "default-src 'none'" in first.headers["content-security-policy"]
    assert first.content.startswith(b"\xff\xd8\xff") and len(first.content) > 100
    assert first.content == second.content
    assert len(calls) == 1
    assert source.read_bytes() == original and digest(source) == service.require(Artifact, id).sha256
    assert len(list((service.root / "thumbnail-cache").glob("*.jpg"))) == 1
    assert not list((service.root / "thumbnail-cache").glob("thumbnail-*"))


def test_checksum_mismatch_rejected_even_after_cache_hit(studio):
    client, service = studio
    video = service.root / "valid.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=64x48:d=1", "-c:v", "mpeg4", str(video)], timeout=15)
    id, source = artifact(service, "valid.mp4")
    assert client.get(f"/api/studio/artifacts/{id}/thumbnail").status_code == 200
    source.write_bytes(b"changed")
    assert client.get(f"/api/studio/artifacts/{id}/thumbnail").status_code == 409


def test_non_video_and_svg_rejected_without_extraction(studio, monkeypatch):
    client, service = studio
    import studio.thumbnail as thumbnail
    monkeypatch.setattr(thumbnail.subprocess, "run", lambda *a, **k: pytest.fail("unexpected decode"))
    for name, data in [("script.txt", b"text"), ("vector.svg", b"<svg/>")]:
        id, _ = artifact(service, name, data)
        assert client.get(f"/api/studio/artifacts/{id}/thumbnail").status_code == 415
    assert client.get("/api/studio/artifacts/missing/thumbnail").status_code == 404


def test_artifact_path_traversal_is_rejected(studio, tmp_path, monkeypatch):
    client, service = studio
    id, _ = artifact(service, "source.mp4", b"not a movie")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not a movie")
    with service.sessions() as session:
        row = session.get(Artifact, id)
        row.relative_path = str(outside)
        row.sha256 = digest(outside)
        session.commit()
    import studio.thumbnail as thumbnail
    monkeypatch.setattr(thumbnail.subprocess, "run", lambda *a, **k: pytest.fail("unexpected decode"))
    assert client.get(f"/api/studio/artifacts/{id}/thumbnail").status_code == 409


def test_corrupt_video_fails_cleanly_without_cache(studio):
    client, service = studio
    id, source = artifact(service, "broken.mp4", b"not a movie")
    response = client.get(f"/api/studio/artifacts/{id}/thumbnail")
    assert response.status_code == 409
    assert source.read_bytes() == b"not a movie"
    assert not list((service.root / "thumbnail-cache").glob("*.jpg"))
