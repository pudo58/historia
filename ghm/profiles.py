from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field
import yaml


class GPUProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    label: str
    gpu_pattern: str
    min_vram_gb: float
    min_ram_gb: float
    min_disk_gb: float
    min_driver: int
    torch_index: str
    torch_version: str
    cuda_version: str
    args: list[str] = []


def load_profiles(path: Path) -> list[GPUProfile]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    profiles = [GPUProfile.model_validate(row) for row in data["profiles"]]
    if len({p.id for p in profiles}) != len(profiles):
        raise ValueError("Duplicate profile IDs.")
    return profiles


def choose_profile(profiles: list[GPUProfile], requested: str, report) -> GPUProfile:
    if not report or not report.gpu:
        raise ValueError("Run preflight to detect the GPU before choosing a profile.")
    candidates = [p for p in profiles if p.id == requested] if requested != "auto" else [
        p for p in profiles if re.search(p.gpu_pattern, report.gpu.name, re.I)
    ]
    if len(candidates) != 1:
        raise ValueError("No unique compatible profile. Add a reviewed profile to manifests/profiles.yaml.")
    profile = candidates[0]
    if report.gpu.vram_gb < profile.min_vram_gb or (report.ram_gb or 0) < profile.min_ram_gb:
        raise ValueError("GPU VRAM or system RAM is below the selected profile requirements.")
    if int(report.gpu.driver_version.split(".")[0]) < profile.min_driver:
        raise ValueError(f"Profile requires NVIDIA driver {profile.min_driver}+; update it outside this app.")
    return profile
