from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class EncodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    crf: int = Field(default=18, ge=0, le=51)
    video_codec: str = "libx264"
    pixel_format: str = "yuv420p"


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runs_dir: Path = Path("runs")
    process_fps: Literal["native"] | float = "native"
    encode: EncodeConfig = EncodeConfig()


def load_config(path: Path) -> PipelineConfig:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return PipelineConfig.model_validate(raw)

