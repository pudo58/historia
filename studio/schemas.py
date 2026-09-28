from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def target_duration(data: dict) -> int:
    """Read old JSON projects without a database migration."""
    return int(data.get("duration_seconds") or data.get("duration_minutes", 10) * 60)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InstallConsent(StrictModel):
    plan_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    license_accepted: bool = False


class ProjectInput(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    topic: str = Field(min_length=1, max_length=4000)
    era: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=300)
    duration_minutes: Literal[1, 1.5, 3, 5, 10, 15, 20] = 10
    duration_seconds: Literal[60, 90, 180, 300, 600, 900, 1200] | None = None

    @model_validator(mode="after")
    def normalize_duration(self):
        if self.duration_seconds is None:
            self.duration_seconds = int(self.duration_minutes * 60)
        else:
            self.duration_minutes = self.duration_seconds / 60
        return self
    style: str = Field(default="Tái hiện điện ảnh chân thực", max_length=800)
    quality: Literal["draft", "final"] = "draft"
    render_profile: Literal["draft", "standard"] | None = None
    output_resolution: Literal["720p", "1080p", "1440p"] | None = None
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:5"] = "16:9"
    transition: Literal["none", "natural", "dissolve"] = "natural"
    upscale_method: Literal["lanczos", "ai"] = "lanczos"

    @model_validator(mode="after")
    def validate_upscale(self):
        if self.upscale_method == "ai":
            raise ValueError("AI upscale chưa được benchmark; chọn lanczos.")
        return self

    # Null render/output settings retain legacy quality mapping.
    voice: str = Field(default="default", max_length=120)
    tts_device: Literal["cuda", "cpu"] | None = None
    host_id: str | None = None
    hourly_usd: float | None = Field(default=None, ge=0, le=1000)
    pronunciation: str = Field(default="", max_length=4000)


class TextSourceInput(StrictModel):
    title: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=200_000)
    role: Literal["historical", "visual"] = "historical"
    citation: str = Field(default="", max_length=2000)


class SourceUpdate(StrictModel):
    selected: bool
    description: str = Field(default="", max_length=4000)


class CharacterInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["character", "location", "costume"] = "character"
    description: str = Field(min_length=1, max_length=4000)
    reference_ids: list[str] = Field(default_factory=list, max_length=3)


class Citation(StrictModel):
    source_id: str
    quote: str = Field(min_length=1, max_length=1500)


class SceneInput(StrictModel):
    chapter: str = Field(default="Chương 1", max_length=200)
    title: str = Field(min_length=1, max_length=160)
    narration: str = Field(default="", max_length=4000)
    visual_prompt: str = Field(default="", max_length=4000)
    camera: str = Field(default="Chuyển động chậm, tự nhiên", max_length=1000)
    character_ids: list[str] = Field(default_factory=list, max_length=10)
    reference_ids: list[str] = Field(default_factory=list, max_length=3)
    citations: list[Citation] = Field(default_factory=list, max_length=20)
    seed: int = Field(default=42, ge=0, le=2**53-1)
    steps: int | None = Field(default=None, ge=1, le=50)
    review_note: str = Field(default="", max_length=2000)


class SceneUpdate(SceneInput):
    revision: int = Field(ge=1)


class Approval(StrictModel):
    revision: int = Field(ge=1)
    target: Literal["script", "keyframe", "clip"]
    approved: bool = True


class OutlineChapter(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=4000)
    source_ids: list[str] = Field(default_factory=list, max_length=100)


class OutlineApproval(StrictModel):
    outline: list[OutlineChapter] = Field(min_length=1, max_length=12)


class JobInput(StrictModel):
    kind: Literal["outline", "script", "analyze_reference", "keyframe", "speech", "clip",
                  "export", "install", "verify", "benchmark"]
    host_id: str | None = None
    scene_id: str | None = None
    source_id: str | None = None
    force: bool = False
    license_accepted: bool = False


class ProductionRunInput(StrictModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    scene_revisions: dict[str, int]
    script_approved: bool = False
    gpu_consent: bool = False
    unsourced_consent: bool = False


class ProductionInput(StrictModel):
    stage: Literal["assets", "clips", "export"]


class BatchInput(StrictModel):
    kind: Literal["keyframe", "speech", "clip"]
    scene_ids: list[str] = Field(min_length=1, max_length=500)


class ExportInput(StrictModel):
    burn_subtitles: bool = False
    music_artifact_id: str | None = None
