from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HostCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128)
    auth_kind: Literal["password", "private_key"]
    secret: str = Field(min_length=1)


class RunPodConnect(BaseModel):
    mode: Literal["auto", "direct", "proxy"] = "auto"
    ssh_command: str = Field(default="", max_length=500)


class RunPodAction(BaseModel):
    action: Literal["stop", "start", "terminate"]
    confirm_name: str = Field(default="", max_length=200)
    force: bool = False


class HostKeyConfirmation(BaseModel):
    fingerprint: str = Field(min_length=1, max_length=255)


class HostUpdate(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def disallow_null(cls, values):
        if any(value is None for value in values.values()):
            raise ValueError("Omit unchanged fields; null is not a valid host value.")
        return values

    label: str | None = Field(default=None, min_length=1, max_length=120)
    address: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    auth_kind: Literal["password", "private_key"] | None = None
    secret: str | None = Field(default=None, min_length=1)


class HostRead(BaseModel):
    id: str
    label: str
    address: str
    port: int
    username: str
    auth_kind: str
    state: str
    pending_fingerprint: str | None
    pinned_fingerprint: str | None
    last_error: str | None
    created_at: datetime
    local_url: str | None = None
    tunnel_status: str = "stopped"
    gpu: GPUInfo | None = None
    vram_gb: float | None = None
    ram_gb: float | None = None
    comfy_version: str | None = None
    profile: str | None = None
    health_passed: bool = False
    smoke_passed: bool = False


class KeyInspection(BaseModel):
    fingerprint: str
    host: str
    port: int


class CheckResult(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    message: str


class GPUInfo(BaseModel):
    name: str
    vram_gb: float
    driver_version: str
    compute_capability: str | None = None
    cuda_version: str | None = None


class DiskInfo(BaseModel):
    mount: str
    available_gb: float


class PreflightReport(BaseModel):
    status: Literal["pass", "warn", "fail"]
    ssh_mode: str = "exec"
    os_name: str | None = None
    container_kind: str | None = None
    is_root: bool | None = None
    gpu: GPUInfo | None = None
    ram_gb: float | None = None
    disks: list[DiskInfo] = []
    python_version: str | None = None
    checks: list[CheckResult]


class StoredPreflightReport(PreflightReport):
    created_at: datetime


class RecipeInfo(BaseModel):
    name: str
    description: str


class RunStart(BaseModel):
    recipe_name: str = Field(min_length=1, max_length=120)


class RunStepRead(BaseModel):
    step_id: str
    status: str
    attempts: int
    message: str | None
    optional: bool = False


class HostOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str = "/opt/ghm"
    remote_port: int = Field(default=8188, ge=1024, le=65535)
    profile: str = "auto"
    adopt_existing: bool = False
    comfy_path: str | None = None
    workflow: dict | None = None
    smoke_timeout: int = Field(default=300, ge=10, le=1800)

    @property
    def comfy_root(self) -> str:
        return self.comfy_path or self.root + '/ComfyUI'

    @field_validator("root", "comfy_path")
    @classmethod
    def validate_root(cls, value):
        if value is None:
            return None
        import re
        from pathlib import PurePosixPath
        if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", value) or ".." in PurePosixPath(value).parts:
            raise ValueError("Use an absolute Linux path without whitespace or '..'.")
        if value.rstrip("/") in {"", "/opt", "/usr", "/etc", "/home", "/root", "/var", "/tmp"}:
            raise ValueError("Choose a dedicated install directory, for example /workspace/ghm.")
        return value.rstrip("/")

    @model_validator(mode='after')
    def separate_comfy_path(self):
        if self.comfy_path and not self.adopt_existing and self.comfy_path != self.root + '/ComfyUI':
            raise ValueError('Đường dẫn ComfyUI riêng chỉ dùng ở chế độ tái sử dụng.')
        return self

    @field_validator("workflow")
    @classmethod
    def validate_workflow(cls, value):
        import json
        if value is None:
            return None
        if len(json.dumps(value)) > 1_000_000:
            raise ValueError("Workflow exceeds 1 MB.")
        if not value or any(not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
                            or not isinstance(node.get("inputs"), dict) for node in value.values()):
            raise ValueError("Paste the API-format node graph, not the UI workflow or a prompt wrapper.")
        return value


class TokenUpdate(BaseModel):
    token: str = Field(max_length=1024)


class RunRead(BaseModel):
    id: str
    host_id: str
    recipe_name: str
    status: str
    cancel_requested: bool
    created_at: datetime
    steps: list[RunStepRead]
