"""Trusted declarative recipes stored under the local recipes directory."""

from pathlib import Path

import yaml
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecipeStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    check: str = Field(min_length=1)
    run: str = Field(min_length=1)
    verify: str = Field(min_length=1)
    timeout: float = Field(default=300, gt=0, le=3600)
    retries: int = Field(default=0, ge=0, le=5)
    depends_on: list[str] = []
    action: Literal["shell", "install_nodes", "install_models", "health", "smoke", "start_service", "stop_service"] = "shell"
    manifest_path: str | None = None
    optional: bool = False


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    steps: list[RecipeStep] = Field(min_length=1)
    requires_preflight: bool = False
    variables: dict[str, str] = {}
    validates_backend: bool = False

    @model_validator(mode="after")
    def validate_readiness(self):
        if self.validates_backend:
            actions = {step.action for step in self.steps if not step.optional}
            if not {"health", "smoke"}.issubset(actions):
                raise ValueError("A backend validation recipe requires mandatory health and smoke actions.")
        return self

    def ordered_steps(self) -> list[RecipeStep]:
        by_id = {step.id: step for step in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError(f"Recipe '{self.name}' contains duplicate step IDs.")
        ordered: list[RecipeStep] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step: RecipeStep) -> None:
            if step.id in visited:
                return
            if step.id in visiting:
                raise ValueError(f"Recipe '{self.name}' contains a dependency cycle at '{step.id}'.")
            visiting.add(step.id)
            for dependency in step.depends_on:
                if dependency not in by_id:
                    raise ValueError(f"Step '{step.id}' depends on unknown step '{dependency}'.")
                visit(by_id[dependency])
            visiting.remove(step.id)
            visited.add(step.id)
            ordered.append(step)

        for step in self.steps:
            visit(step)
        return ordered


class RecipeCatalog:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def list(self) -> list[Recipe]:
        if not self._directory.exists():
            return []
        return [self.get(path.stem) for path in sorted(self._directory.glob("*.yaml"))]

    def get(self, name: str) -> Recipe:
        if not name.replace("-", "").replace("_", "").isalnum() or "/" in name or "\\" in name:
            raise ValueError("Invalid recipe name.")
        path = self._directory / f"{name}.yaml"
        if not path.is_file():
            raise KeyError(name)
        with path.open("r", encoding="utf-8") as handle:
            recipe = Recipe.model_validate(yaml.safe_load(handle))
        if recipe.name != name:
            raise ValueError(f"Recipe name does not match its filename: {path.name}")
        recipe.ordered_steps()
        return recipe

    def manifest_file(self, relative_path: str) -> Path:
        path = (self._directory.parent / relative_path).resolve()
        root = self._directory.parent.resolve()
        if root not in path.parents:
            raise ValueError("Manifest path escapes the project directory.")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path
