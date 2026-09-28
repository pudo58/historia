"""Reviewed, profile-aware model/node manifests. No guessed checksums or moving pins."""
import re
import shlex
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


def _safe_subfolder(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or not value:
        raise ValueError("Manifest destination must be a relative subfolder without '..'.")
    return str(path)


class ModelAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-zA-Z0-9._-]+$")
    repo: str | None = Field(default=None, pattern=r"^[\w.-]+/[\w.-]+$")
    filename: str | None = None
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    url: HttpUrl | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    destination: str
    gated: bool = False
    enabled: bool = True
    license_accepted: bool = False
    profiles: list[str] = []
    notes: str = ""
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_paths(self):
        _safe_subfolder(self.destination)
        if self.filename:
            _safe_subfolder(self.filename)
        if not self.repo and not self.url:
            raise ValueError("Model requires a Hugging Face repo or a reviewed HTTPS URL.")
        if self.url and (self.url.scheme != "https" or self.url.username or self.url.password):
            raise ValueError("Model URLs must be HTTPS without credentials.")
        return self

    def resolved(self) -> bool:
        return bool(self.sha256 and (self.repo and self.filename and self.revision or self.url))

    def download_url(self) -> str:
        if not self.resolved():
            raise ValueError(f"{self.name}: resolve revision, filename and SHA256 before installation.")
        if self.repo:
            return f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{quote(self.filename, safe='/')}"
        return str(self.url)

    def target(self, comfy: str) -> str:
        folder = self.destination.removeprefix("models/")
        filename = PurePosixPath(self.filename).name if self.filename else self.name
        return f"{comfy}/models/{_safe_subfolder(folder)}/{filename}"


class NodeAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-zA-Z0-9._-]+$")
    repo: HttpUrl
    commit: str = Field(pattern=r"^[a-fA-F0-9]{40}$")
    requirements: list[str] = []

    @model_validator(mode="after")
    def validate_requirements(self):
        for value in self.requirements:
            _safe_subfolder(value)
        if self.repo.scheme != "https" or self.repo.username or self.repo.password:
            raise ValueError("Node repositories must be HTTPS without credentials.")
        return self


def _read_list(path, key: str) -> list[dict]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if isinstance(raw, dict):
        raw = raw.get(key, [])
    if not isinstance(raw, list):
        raise ValueError(f"Manifest must contain a '{key}' list.")
    return raw


def load_models(path) -> list[ModelAsset]:
    models = [ModelAsset.model_validate(item) for item in _read_list(path, "models")]
    if len({m.name for m in models}) != len(models):
        raise ValueError("Duplicate model IDs.")
    return models


def load_nodes(path) -> list[NodeAsset]:
    return [NodeAsset.model_validate(item) for item in _read_list(path, "nodes")]


def selected_models(models: list[ModelAsset], profile: str) -> list[ModelAsset]:
    selected = [m for m in models if m.enabled and (not m.profiles or profile in m.profiles)]
    targets = set()
    for model in selected:
        model.download_url()
        target = model.target("/ComfyUI")
        if target in targets:
            raise ValueError("Two selected models have the same destination.")
        targets.add(target)
        if model.gated and not model.license_accepted:
            raise ValueError(f"{model.name}: accept its license on Hugging Face and in the manifest first.")
    return selected


def model_commands(path, root: str, hf_token_variable=None) -> list[str]:
    """Legacy ungated command generator; runtime uses stdin downloader for gated assets."""
    commands = []
    for asset in load_models(path):
        if not asset.enabled:
            continue
        if asset.gated:
            raise ValueError("Gated assets require the secure stdin downloader.")
        target = asset.target(root)
        part = target + ".part"
        folder = str(PurePosixPath(target).parent)
        q = shlex.quote
        checksum = f"printf '%s  %s\\n' {q(asset.sha256 or '')} {q(target)} | sha256sum -c -"
        commands.append(
            f"mkdir -p {q(folder)} && ( {checksum} || ( aria2c -c --file-allocation=none "
            f"-d {q(folder)} -o {q(PurePosixPath(part).name)} {q(asset.download_url())} && "
            f"printf '%s  %s\\n' {q(asset.sha256)} {q(part)} | sha256sum -c - && mv {q(part)} {q(target)} ) )"
        )
    return commands


def node_command(asset: NodeAsset, comfy: str, python: str) -> str:
    q = shlex.quote
    destination = f"{comfy}/custom_nodes/{asset.name}"
    command = (
        f"mkdir -p {q(comfy + '/custom_nodes')} && "
        f"if [ -d {q(destination + '/.git')} ]; then "
        f"test -z \"$(git -C {q(destination)} status --porcelain)\" && "
        f"test \"$(git -C {q(destination)} remote get-url origin)\" = {q(str(asset.repo))} && "
        f"git -C {q(destination)} fetch --depth 1 origin {q(asset.commit)}; "
        f"else git clone --no-checkout {q(str(asset.repo))} {q(destination)}; fi && "
        f"git -C {q(destination)} checkout --detach {q(asset.commit)} && "
        f"test \"$(git -C {q(destination)} rev-parse HEAD)\" = {q(asset.commit)}"
    )
    for requirement in asset.requirements:
        command += f" && {q(python)} -m pip install -r {q(destination + '/' + requirement)}"
    return command


def node_commands(path, comfy: str, python: str) -> list[str]:
    return [node_command(asset, comfy, python) for asset in load_nodes(path)]


def resolve_model(asset: ModelAsset, token: str | None, api=None) -> ModelAsset:
    from huggingface_hub import HfApi
    if not asset.repo or not asset.filename:
        raise ValueError(f"{asset.name}: choose an exact repository filename first.")
    api = api or HfApi(endpoint="https://huggingface.co", token=token or False)
    info = api.model_info(asset.repo, revision=asset.revision, files_metadata=True, timeout=20)
    if not re.fullmatch(r"[a-f0-9]{40}", info.sha or ""):
        raise ValueError("Hub did not return an immutable commit SHA.")
    file = next((item for item in info.siblings if item.rfilename == asset.filename), None)
    sha = getattr(getattr(file, "lfs", None), "sha256", None)
    if not sha or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError(f"{asset.name}: exact file or LFS SHA256 is missing; no hash was guessed.")
    if asset.sha256 and asset.sha256.lower() != sha:
        raise ValueError(f"{asset.name}: checksum mismatch against the reviewed manifest.")
    return asset.model_copy(update=dict(revision=info.sha, sha256=sha, gated=bool(info.gated),
                                         size_bytes=file.size))
