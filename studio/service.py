"""Persistent project state and dependency-aware scene approvals."""
import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from studio.media import digest
from studio.models import Artifact, Character, Job, ProductionRun, Project, Scene, Source
from studio.schemas import CharacterInput, ProjectInput, SceneInput, SceneUpdate, TextSourceInput, target_duration


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class StudioService:
    def __init__(self, sessions: sessionmaker[Session], root: Path):
        self.sessions = sessions
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def require(self, model: type, id: str) -> Any:
        with self.sessions() as session:
            item = session.get(model, id)
            if item is None:
                raise KeyError(id)
            return item

    @staticmethod
    def read(row: Any) -> dict:
        result = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        if "data" in result:
            result.update(result.pop("data"))
        if isinstance(row, Project):
            result["duration_seconds"] = target_duration(result)
        return result

    def projects(self) -> list[dict]:
        with self.sessions() as session:
            return [self.read(p) for p in session.scalars(select(Project).order_by(Project.updated_at.desc()))]

    def create_project(self, data: ProjectInput) -> dict:
        project = Project(data={**data.model_dump(), "outline": [], "outline_approved": False})
        with self.sessions() as session:
            session.add(project)
            session.commit()
        return self.read(project)

    def delete_project(self, id: str) -> None:
        """Delete local records only; retain files and never contact a GPU."""
        from sqlalchemy import text
        from studio.jobs import ACTIVE, StudioJobs
        with self.sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            row = session.get(Project, id)
            if row is None:
                raise KeyError(id)
            if session.scalar(select(ProductionRun.id).where(ProductionRun.project_id == id,
                    ProductionRun.status.in_(['running', 'pause_requested', 'paused', 'duration_review', 'reconciling']))):
                raise ValueError('Dự án còn lượt sản xuất đang hoạt động; chưa thể xóa.')
            jobs = list(session.scalars(select(Job).where(Job.project_id == id)))
            if any(job.status in ACTIVE or (job.status not in {'completed', 'cancelled'} and StudioJobs.remote_pending(job.result)) for job in jobs):
                raise ValueError('Dự án còn tác vụ đang chạy/chờ hoặc chưa đối chiếu GPU; chưa thể xóa.')
            session.delete(row)
            session.commit()

    def project(self, id: str) -> dict:
        result = self.read(self.require(Project, id))
        with self.sessions() as session:
            result["sources"] = [self.read(row) for row in session.scalars(select(Source).where(Source.project_id == id))]
            result["characters"] = [self.read(row) for row in session.scalars(select(Character).where(Character.project_id == id))]
            result["scenes"] = [self.read(row) for row in session.scalars(select(Scene).where(Scene.project_id == id).order_by(Scene.position))]
        return result

    def update_project(self, id: str, data: ProjectInput) -> dict:
        self.assert_idle(id)
        with self.sessions() as session:
            row = session.get(Project, id)
            if row is None:
                raise KeyError(id)
            old = row.data
            values = {**old, **data.model_dump()}
            script_changed = any(old.get(k) != values[k] for k in ["topic", "era", "location", "duration_minutes", "duration_seconds"])
            if script_changed:
                values["outline_approved"] = False
            visual_changed = any(old.get(k) != values[k] for k in ["style", "era", "location", "quality", "render_profile", "aspect_ratio"])
            audio_changed = any(old.get(k) != values[k] for k in ["voice", "pronunciation"])
            for scene in session.scalars(select(Scene).where(Scene.project_id == id)):
                scene_data = dict(scene.data)
                if script_changed:
                    scene_data["script_approved"] = False
                if visual_changed:
                    for key in ["keyframe_id", "keyframe_approved", "clip_ids", "clip_approved"]:
                        scene_data.pop(key, None)
                if audio_changed:
                    scene_data.pop("speech_id", None)
                    scene_data.pop("duration", None)
                    scene_data.pop("shot_count", None)
                    scene_data["clip_ids"] = []
                    scene_data["clip_approved"] = False
                if script_changed or visual_changed or audio_changed:
                    scene.data = scene_data
                    scene.revision += 1
            row.data = values
            session.commit()
        return self.project(id)

    def assert_idle(self, project_id: str) -> None:
        with self.sessions() as session:
            if session.scalar(select(ProductionRun.id).where(ProductionRun.project_id == project_id,
                    ProductionRun.status.in_(['running', 'pause_requested', 'reconciling']))):
                raise ValueError('Tạm dừng lượt sản xuất trước khi sửa revision.')
            for job in session.scalars(select(Job).where(Job.project_id == project_id,
                    Job.status.in_(['queued', 'running', 'cancelling', 'reconciling', 'paused']))):
                if job.status == 'paused' and job.snapshot.get('production_run_id'):
                    from studio.jobs import StudioJobs
                    if not StudioJobs.remote_pending(job.result):
                        continue
                raise ValueError("Dự án có tác vụ đang chạy hoặc chờ. Dừng tác vụ trước khi sửa nội dung.")

    def add_text(self, project_id: str, value: TextSourceInput) -> dict:
        self.require(Project, project_id)
        self.assert_idle(project_id)
        row = Source(project_id=project_id, data={**value.model_dump(), "kind": "text", "status": "ready",
                                               "selected": True, "artifact_id": None, "description": ""})
        with self.sessions() as session:
            session.add(row)
            session.commit()
        return self.read(row)

    def artifact(self, path: Path, project_id: str | None, name: str, job_id: str | None = None,
                 metadata: dict | None = None) -> dict:
        path = path.resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Artifact nằm ngoài thư mục dữ liệu.")
        row = Artifact(project_id=project_id, job_id=job_id, relative_path=str(path.relative_to(self.root)),
                       sha256=digest(path), name=name, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                       data=metadata or {})
        with self.sessions() as session:
            session.add(row)
            session.commit()
        return self.read(row)

    def artifact_path(self, id: str) -> Path:
        row = self.require(Artifact, id)
        path = (self.root / row.relative_path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Không tìm thấy artifact trong kho local.")
        if digest(path) != row.sha256:
            raise ValueError("Artifact đã bị thay đổi hoặc hỏng; checksum không khớp.")
        return path

    def job_directory(self, job_id: str) -> Path:
        # Job IDs are generated internally, not supplied as filesystem paths.
        if not job_id or not all(c in "0123456789abcdef-" for c in job_id):
            raise ValueError("Job ID không hợp lệ.")
        path = self.root / "jobs" / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def add_character(self, project_id: str, data: CharacterInput) -> dict:
        self.require(Project, project_id)
        self.assert_idle(project_id)
        self.validate_references(project_id, data.reference_ids)
        row = Character(project_id=project_id, data=data.model_dump())
        with self.sessions() as session:
            session.add(row)
            session.commit()
        return self.read(row)

    def update_source(self, source_id: str, selected: bool, description: str) -> dict:
        row = self.require(Source, source_id)
        self.assert_idle(row.project_id)
        with self.sessions() as session:
            for scene in session.scalars(select(Scene).where(Scene.project_id == row.project_id)):
                used = source_id in scene.data.get("reference_ids", [])
                used |= any(c["source_id"] == source_id for c in scene.data.get("citations", []))
                if used:
                    raise ValueError("Tư liệu đang được dùng trong cảnh. Gỡ liên kết ở cảnh trước khi thay đổi.")
            for character in session.scalars(select(Character).where(Character.project_id == row.project_id)):
                if source_id in character.data.get("reference_ids", []):
                    raise ValueError("Tư liệu đang dùng trong hồ sơ hình ảnh.")
            row.data = {**row.data, "selected": selected, "description": description}
            session.merge(row)
            session.commit()
        return self.read(row)

    def validate_references(self, project_id: str, ids: list[str]) -> None:
        for id in ids:
            source = self.require(Source, id)
            if source.project_id != project_id or source.data.get("kind") not in {"image", "frame"}:
                raise ValueError("Ảnh tham chiếu phải thuộc dự án này.")
            if not source.data.get("selected"):
                raise ValueError("Duyệt/chọn ảnh tham chiếu trước khi sử dụng.")

    def validate_scene(self, project_id: str, data: SceneInput) -> list[str]:
        warnings = []
        self.validate_references(project_id, data.reference_ids)
        for id in data.character_ids:
            if self.require(Character, id).project_id != project_id:
                raise ValueError("Hồ sơ hình ảnh không thuộc dự án.")
        for citation in data.citations:
            source = self.require(Source, citation.source_id)
            if source.project_id != project_id or source.data.get("role") != "historical":
                raise ValueError("Chỉ tư liệu lịch sử trong dự án mới được trích dẫn.")
            quote = " ".join(citation.quote.split())
            text = " ".join(source.data.get("text", "").split())
            if not quote or quote not in text:
                raise ValueError("Đoạn trích không có nguyên văn trong nguồn. Hãy copy một câu có sẵn, không diễn đạt lại.")
        if not data.citations:
            warnings.append("Chưa có nguồn lịch sử: cần bạn kiểm chứng trước khi duyệt.")
        return warnings

    def add_scene(self, project_id: str, data: SceneInput) -> dict:
        self.require(Project, project_id)
        self.assert_idle(project_id)
        warnings = self.validate_scene(project_id, data)
        with self.sessions() as session:
            scenes = list(session.scalars(select(Scene).where(Scene.project_id == project_id)))
            row = Scene(project_id=project_id, position=len(scenes)+1,
                        data={**data.model_dump(), "warnings": warnings, "script_approved": False})
            session.add(row)
            session.commit()
        return self.read(row)

    def update_scene(self, scene_id: str, data: SceneUpdate) -> dict:
        old = self.require(Scene, scene_id)
        self.assert_idle(old.project_id)
        warnings = self.validate_scene(old.project_id, data)
        if data.revision != old.revision:
            raise ValueError("Cảnh đã được cập nhật ở nơi khác. Tải lại trước khi lưu.")
        values = {**old.data, **data.model_dump(exclude={"revision"}), "warnings": warnings}
        visual_fields = ["visual_prompt", "camera", "character_ids", "reference_ids", "seed", "steps"]
        if any(old.data.get(k) != values[k] for k in visual_fields):
            for k in ["keyframe_id", "keyframe_approved", "clip_ids", "clip_approved"]:
                values.pop(k, None)
        if old.data.get("narration") != values["narration"]:
            values.pop("speech_id", None)
            values.pop("duration", None)
            values.pop("shot_count", None)
            values["clip_ids"] = []
            values["clip_approved"] = False
        values["script_approved"] = False
        with self.sessions() as session:
            old.data = values
            old.revision += 1
            changed = session.execute(update(Scene).where(Scene.id == scene_id, Scene.revision == data.revision)
                                      .values(data=values, revision=old.revision).returning(Scene.id)).scalar_one_or_none()
            if changed is None:
                raise ValueError("Cảnh đã thay đổi trong lúc lưu. Tải lại trước khi sửa.")
            session.commit()
        return self.read(old)

    def approve(self, id: str, revision: int, target: str, approved: bool) -> dict:
        row = self.require(Scene, id)
        self.assert_idle(row.project_id)
        if revision != row.revision:
            raise ValueError("Cảnh đã thay đổi. Tải lại trước khi duyệt.")
        if approved and target == "keyframe" and not row.data.get("script_approved"):
            raise ValueError("Duyệt kịch bản trước khi duyệt ảnh.")
        if target == "keyframe" and not row.data.get("keyframe_id"):
            raise ValueError("Chưa có ảnh để duyệt.")
        if target == "clip" and not row.data.get("clip_ids"):
            raise ValueError("Chưa có clip để duyệt.")
        if approved and target == "keyframe":
            self.artifact_path(row.data["keyframe_id"])
        if approved and target == "clip":
            from studio.media import probe
            if not row.data.get("keyframe_approved") or not row.data.get("speech_id") or not row.data.get("script_approved"):
                raise ValueError("Duyệt ảnh, kịch bản và tạo giọng đọc trước khi duyệt clip.")
            duration = probe(self.artifact_path(row.data["speech_id"]))["duration"]
            available = sum(probe(self.artifact_path(id))["duration"] for id in row.data["clip_ids"])
            if duration <= 0 or available + .05 < duration:
                raise ValueError("Clip chưa đủ thời lượng lời đọc; cần tạo thêm shot.")
        if target == "script" and approved and not row.data.get("citations") and not row.data.get("review_note"):
            raise ValueError("Cảnh thiếu nguồn: thêm trích dẫn hoặc ghi chú kiểm chứng của bạn trước khi duyệt.")
        row.data = {**row.data, target + "_approved": approved}
        if not approved and target in {"script", "keyframe"}:
            row.data = {**row.data, "clip_approved": False}
        row.revision += 1
        with self.sessions() as session:
            changed = session.execute(update(Scene).where(Scene.id == id, Scene.revision == revision)
                                      .values(data=row.data, revision=row.revision).returning(Scene.id)).scalar_one_or_none()
            if changed is None:
                raise ValueError("Cảnh đã thay đổi trong lúc duyệt. Tải lại trước khi duyệt.")
            session.commit()
        return self.read(row)
