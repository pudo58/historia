from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ghm.database import Base


def identifier() -> str:
    return str(uuid4())


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class Project(Base):
    __tablename__ = "studio_projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=timestamp)
    updated_at: Mapped[str] = mapped_column(String(40), default=timestamp, onupdate=timestamp)


class Source(Base):
    __tablename__ = "studio_sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=timestamp)


class Character(Base):
    __tablename__ = "studio_characters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class Scene(Base):
    __tablename__ = "studio_scenes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)


class Job(Base):
    __tablename__ = "studio_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), nullable=True, index=True)
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id", ondelete="RESTRICT"), nullable=True, index=True)
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("studio_scenes.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=timestamp)
    updated_at: Mapped[str] = mapped_column(String(40), default=timestamp, onupdate=timestamp)


class ProductionRun(Base):
    __tablename__ = "studio_production_runs"
    __table_args__ = (UniqueConstraint('project_id', 'idempotency_key'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    stage: Mapped[str] = mapped_column(String(30), default="speech")
    snapshot: Mapped[dict] = mapped_column(JSON)
    consent: Mapped[dict] = mapped_column(JSON)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=timestamp)
    updated_at: Mapped[str] = mapped_column(String(40), default=timestamp, onupdate=timestamp)


class JobEvent(Base):
    __tablename__ = "studio_job_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("studio_jobs.id", ondelete="CASCADE"), index=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=timestamp)


class Artifact(Base):
    __tablename__ = "studio_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("studio_projects.id", ondelete="CASCADE"), nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("studio_jobs.id", ondelete="SET NULL"), nullable=True)
    relative_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(255))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Installation(Base):
    __tablename__ = "studio_installations"
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True)
    pack_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default="not_installed")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String(40), default=timestamp, onupdate=timestamp)
