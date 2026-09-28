from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///ghm.db"
    known_hosts_path: Path = Path(".ghm/known_hosts")
    local_origin: str = "http://127.0.0.1:5173"
    recipes_dir: Path = Path("recipes")
    frontend_dist: Path = Path("ghm/frontend/dist")
    studio_root: Path | None = None
