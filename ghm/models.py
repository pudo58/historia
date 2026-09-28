from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ghm.database import Base


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    label: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(128))
    auth_kind: Mapped[str] = mapped_column(String(16))
    encrypted_secret: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="needs_setup")
    pending_fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pinned_fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PreflightSnapshot(Base):
    __tablename__ = "preflight_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecipeRun(Base):
    __tablename__ = "recipe_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), index=True)
    recipe_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RecipeStepRun(Base):
    __tablename__ = "recipe_step_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("recipe_runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class RecipeLog(Base):
    __tablename__ = "recipe_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("recipe_runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HostRuntime(Base):
    __tablename__ = "host_runtime"

    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True)
    local_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tunnel_status: Mapped[str] = mapped_column(String(16), default="stopped")
    health_passed: Mapped[bool] = mapped_column(default=False)
    smoke_passed: Mapped[bool] = mapped_column(default=False)
    comfy_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    profile: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class HostConfiguration(Base):
    __tablename__ = "host_configuration"
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)


class RunSnapshot(Base):
    __tablename__ = "run_snapshots"
    run_id: Mapped[str] = mapped_column(ForeignKey("recipe_runs.id", ondelete="CASCADE"), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)


class LocalSetting(Base):
    __tablename__ = "local_settings"
    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text)
