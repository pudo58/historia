import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"version": 1, "created_at": self._now(), "stages": {}}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def is_complete(self, stage: str, input_hash: str) -> bool:
        record = self.data["stages"].get(stage, {})
        return record.get("status") == "complete" and record.get("input_hash") == input_hash

    def mark(self, stage: str, status: str, input_hash: str, **details: Any) -> None:
        self.data["stages"][stage] = {
            "status": status,
            "input_hash": input_hash,
            "updated_at": self._now(),
            **details,
        }
        self.save()

