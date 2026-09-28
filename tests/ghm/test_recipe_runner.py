import asyncio

import pytest

from ghm.database import make_session_factory
from ghm.executors.base import CommandResult
from ghm.executors.fake import FakeExecutor
from ghm.recipe_runner import RecipeRunner
from ghm.recipes import RecipeCatalog
from ghm.schemas import HostCreate
from ghm.security import SecretStore
from ghm.services.hosts import HostService


async def wait_for_terminal(runner: RecipeRunner, run_id: str) -> str:
    for _ in range(50):
        status = runner.get(run_id).status
        if status in {"completed", "failed", "cancelled"}:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("run did not finish")


def write_recipe(tmp_path, name: str = "test-recipe") -> None:
    content = "\n".join(
        [
            f"name: {name}",
            "description: Test-only recipe",
            "steps:",
            "  - id: install",
            "    description: Retry a deterministic command",
            "    check: check-install",
            "    run: run-install",
            "    verify: verify-install",
            "    retries: 1",
            "  - id: already-ready",
            "    description: Verify resume skips complete dependencies",
            "    check: check-ready",
            "    run: run-ready",
            "    verify: verify-ready",
            "    depends_on: [install]",
        ]
    )
    (tmp_path / f"{name}.yaml").write_text(content)


@pytest.mark.asyncio
async def test_recipe_runner_retries_and_resumes_idempotently(tmp_path) -> None:
    write_recipe(tmp_path)
    fake = FakeExecutor(
        results={
            "check-install": CommandResult(1, "not installed", ""),
            "run-install": [CommandResult(1, "", "first failure"), CommandResult(0, "", "")],
            "verify-install": CommandResult(0, "", ""),
            "check-ready": CommandResult(0, "", ""),
        }
    )
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'runs.db'}")
    hosts = HostService(sessions, SecretStore("test-key"), lambda host, secret: fake)
    host = hosts.create_host(
        HostCreate(label="Test", address="host", username="user", auth_kind="password", secret="credential")
    )
    host.pending_fingerprint = "SHA256:test"
    hosts._save(host)
    hosts.confirm_key(host.id, "SHA256:test")
    runner = RecipeRunner(sessions, RecipeCatalog(tmp_path), hosts)

    run = await runner.start(host.id, "test-recipe")
    assert await wait_for_terminal(runner, run.id) == "completed"
    final = runner.get(run.id)
    states = {step.step_id: step for step in final.steps}
    assert states["install"].attempts == 2
    assert states["install"].status == "completed"
    # An idempotent check is completed, not a user-skipped mandatory step.
    assert states["already-ready"].status == "completed"
    command_count = len(fake.commands)
    resumed = await runner.resume(run.id)
    assert resumed.status == "completed"
    assert len(fake.commands) == command_count


@pytest.mark.asyncio
async def test_recipe_runner_honors_cancel_before_first_step(tmp_path) -> None:
    write_recipe(tmp_path, "cancel-test")
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'cancel.db'}")
    hosts = HostService(sessions, SecretStore("test-key"), lambda host, secret: FakeExecutor())
    host = hosts.create_host(
        HostCreate(label="Test", address="host", username="user", auth_kind="password", secret="credential")
    )
    host.pending_fingerprint = "SHA256:test"
    hosts._save(host)
    hosts.confirm_key(host.id, "SHA256:test")
    runner = RecipeRunner(sessions, RecipeCatalog(tmp_path), hosts)
    run = await runner.start(host.id, "cancel-test")
    runner.cancel(run.id)
    assert await wait_for_terminal(runner, run.id) == "cancelled"


@pytest.mark.asyncio
async def test_recipe_runner_records_failed_step_after_retries(tmp_path) -> None:
    write_recipe(tmp_path, "failure-test")
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'failure.db'}")
    fake = FakeExecutor(
        results={
            "check-install": CommandResult(1, "", ""),
            "run-install": CommandResult(1, "", "broken"),
        }
    )
    hosts = HostService(sessions, SecretStore("test-key"), lambda host, secret: fake)
    host = hosts.create_host(
        HostCreate(label="Test", address="host", username="user", auth_kind="password", secret="credential")
    )
    host.pending_fingerprint = "SHA256:test"
    hosts._save(host)
    hosts.confirm_key(host.id, "SHA256:test")
    runner = RecipeRunner(sessions, RecipeCatalog(tmp_path), hosts)
    run = await runner.start(host.id, "failure-test")
    assert await wait_for_terminal(runner, run.id) == "failed"
    assert runner.get(run.id).steps[0].status == "failed"
