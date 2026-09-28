"""Speech device policy. No GPU inference."""
import asyncio
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from ghm.api import create_app
from ghm.config import Settings
from ghm.executors.fake import FakeExecutor
from ghm.security import SecretStore
from studio.backend import worker_failure
from studio.jobs import StudioJobs
from studio.models import Job
from studio.schemas import ProjectInput, SceneInput
from studio.tts_device import new_tts_device, snapshot_tts_device


def test_missing_snapshot_stays_cpu_and_new_submission_defaults_cuda():
    assert snapshot_tts_device({}) == "cpu"
    assert snapshot_tts_device({"tts_device": "cpu"}) == "cpu"
    assert snapshot_tts_device({"tts_device": "cuda"}) == "cuda"
    assert new_tts_device({}) == "cuda"
    assert new_tts_device({"tts_device": "cpu"}) == "cpu"


def test_cuda_failures_are_specific_and_not_relabeled_as_disconnect():
    def failure(text, rc=1):
        return worker_failure(SimpleNamespace(rc=rc, stdout="", stderr=text))

    assert "Không tự chuyển về CPU" in failure("CUDA không khả dụng trong môi trường giọng đọc. Không tự chuyển về CPU.")
    assert "không nằm trên CUDA" in failure("Backbone giọng đọc không nằm trên CUDA")
    assert "PyTorch" in failure("CUDA error: no kernel image is available")
    assert "VRAM" in failure("CUDA out of memory")
    assert failure("connection reset", rc=255) != failure("CUDA không khả dụng. Không tự chuyển về CPU.")
    assert "khóa" in failure("", rc=73)


def test_new_speech_records_cuda_without_invalidating_existing_audio(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'tts.db'}", studio_root=tmp_path / "data"),
                     SecretStore("tts-device-test"), lambda h, s: FakeExecutor())
    client = TestClient(app)
    service, jobs = app.state.studio, app.state.studio_jobs
    host = client.post("/api/hosts", json={"label": "GPU", "address": "gpu.example.test",
        "username": "root", "auth_kind": "password", "secret": "secret"}).json()
    stored = jobs.hosts._require_host(host["id"])
    stored.pinned_fingerprint = "SHA256:test"
    jobs.hosts._save(stored)
    project = service.create_project(ProjectInput(title="Film", topic="History", host_id=host["id"]))
    scene = service.add_scene(project["id"], SceneInput(title="Scene", narration="Một câu tiếng Việt.", review_note="Reviewed"))
    service.approve(scene["id"], 1, "script", True)
    jobs.installations.component_proven = lambda *args: True
    created = client.post(f"/api/studio/projects/{project['id']}/jobs", json={"kind": "speech", "scene_id": scene["id"]})
    assert created.status_code == 201
    job = service.require(Job, created.json()["id"])
    assert job.snapshot["project"]["tts_device"] == "cuda"
    before = StudioJobs.input_identity(job.snapshot["project"], job.snapshot["scene"], "speech")
    changed = {**job.snapshot["project"], "tts_device": "cpu"}
    assert StudioJobs.input_identity(changed, job.snapshot["scene"], "speech") == before


def test_confirmed_cuda_error_clears_pending_and_is_not_a_disconnect(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path / 'tts-fail.db'}", studio_root=tmp_path / "data"),
                     SecretStore("tts-device-test"), lambda h, s: FakeExecutor())
    service, jobs = app.state.studio, app.state.studio_jobs
    project = service.create_project(ProjectInput(title="Film", topic="History"))
    scene = service.add_scene(project["id"], SceneInput(title="Scene", narration="Một câu.", review_note="Reviewed"))
    full = service.project(project["id"])
    saved = next(item for item in full["scenes"] if item["id"] == scene["id"])
    with service.sessions() as session:
        row = Job(project_id=project["id"], scene_id=scene["id"], kind="speech", status="running",
                  snapshot={"project": {**full, "tts_device": "cuda"}, "scene": saved}, input_hash="x" * 64)
        session.add(row)
        session.commit()
        job_id = row.id

    async def fail(*_args, **_kwargs):
        raise ValueError("CUDA không khả dụng trong môi trường giọng đọc. Không tự chuyển về CPU.")

    jobs.backend.speech = fail
    job = service.require(Job, job_id)
    with pytest.raises(ValueError, match="Không tự chuyển về CPU"):
        asyncio.run(jobs.execute(job))
    saved_job = service.require(Job, job_id)
    assert saved_job.result["speech_pending"] is False
    assert jobs.remote_pending(saved_job.result) is False
