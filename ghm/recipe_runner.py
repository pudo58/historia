"""Durable recipe snapshots, verified resume, immediate cancellation and live redacted logs."""
import asyncio
import json
import re
import shlex
from collections.abc import AsyncIterator

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ghm.executors.base import CommandResult
from ghm.models import RecipeLog, RecipeRun, RecipeStepRun, RunSnapshot, HostRuntime, Host
from ghm.manifests import load_models, load_nodes, selected_models, ModelAsset, NodeAsset, node_command
from ghm.recipes import Recipe, RecipeCatalog, RecipeStep
from ghm.schemas import RunRead, RunStepRead, HostOptions
from ghm.services.hosts import HostService
from ghm.profiles import load_profiles, choose_profile
from ghm.comfy import check_backend
from ghm.remote_service import manage
from ghm.model_download import download


TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class RecipeRunner:
    def __init__(self, sessions: sessionmaker[Session], catalog: RecipeCatalog, hosts: HostService):
        self._sessions, self._catalog, self._hosts = sessions, catalog, hosts
        self._tasks = {}
        self.studio_idle = lambda host_id: None

    def recover(self):
        with self._sessions() as session:
            session.execute(update(RecipeRun).where(RecipeRun.status.in_(["pending", "running"]))
                            .values(status="interrupted"))
            session.execute(update(RecipeStepRun).where(RecipeStepRun.status.in_(["checking", "running"]))
                            .values(status="interrupted", message="Server restarted; explicit resume required."))
            session.execute(update(HostRuntime).values(local_url=None, tunnel_status="stopped",
                                                       health_passed=False, smoke_passed=False))
            session.execute(update(Host).where(Host.state == "ready").values(state="needs_setup"))
            session.commit()

    def assert_idle(self, host_id: str):
        self.studio_idle(host_id)
        self.assert_recipes_idle(host_id)

    def assert_recipes_idle(self, host_id: str):
        """Check recipe ownership without rejecting other queued Studio jobs."""
        with self._sessions() as session:
            active = session.scalar(select(RecipeRun.id).where(
                RecipeRun.host_id == host_id, RecipeRun.status.in_(["pending", "running"])))
        if active:
            raise ValueError("A run is active on this host. Cancel it and wait before changing configuration.")

    def list(self, host_id: str | None = None) -> list[RunRead]:
        with self._sessions() as session:
            query = select(RecipeRun).order_by(RecipeRun.created_at.desc())
            if host_id:
                query = query.where(RecipeRun.host_id == host_id)
            ids = [r.id for r in session.scalars(query.limit(100))]
        return [self.get(id) for id in ids]

    def _snapshot(self, run_id):
        with self._sessions() as session:
            row = session.get(RunSnapshot, run_id)
            if not row:
                raise ValueError("This legacy run has no safe snapshot. Start a new run.")
            return json.loads(row.payload)

    @staticmethod
    def _identity(host):
        return [host.address, host.port, host.username, host.auth_kind, host.pinned_fingerprint]

    async def start(self, host_id: str, recipe_name: str) -> RunRead:
        self.assert_idle(host_id)
        host = self._hosts._require_host(host_id)
        if not host.pinned_fingerprint:
            raise ValueError("Confirm the SSH host key before running a recipe.")
        recipe = self._catalog.get(recipe_name)
        options = self._hosts.options_for(host_id)
        context = {**recipe.variables, "root": options.root, "comfy": options.root + "/ComfyUI",
                   "venv": options.root + "/venv", "python": options.root + "/venv/bin/python",
                   "port": str(options.remote_port)}
        profile = None
        if recipe.requires_preflight:
            report = self._hosts.latest_preflight(host_id)
            if report is None or report.status == "fail":
                raise ValueError("Run a passing or warning-only preflight first.")
            profiles = load_profiles(self._catalog.manifest_file("manifests/profiles.yaml"))
            profile = choose_profile(profiles, options.profile, report)
            context.update(torch_index=profile.torch_index, torch_version=profile.torch_version,
                           cuda_version=profile.cuda_version, min_disk_gb=str(profile.min_disk_gb))
        if options.adopt_existing and any(s.action in {"start_service", "stop_service", "install_nodes", "install_models"} for s in recipe.steps):
            raise ValueError("Adopt mode is read-only except the approved smoke workflow. Use Verify backend.")
        if recipe.validates_backend and not options.workflow:
            raise ValueError("Save an API-format smoke workflow in host configuration before verification.")
        model_assets, node_assets = [], []
        for step in recipe.steps:
            if step.action == "install_models":
                model_assets = selected_models(load_models(self._catalog.manifest_file(step.manifest_path)),
                                               profile.id if profile else options.profile)
                if any(m.gated for m in model_assets) and not self._hosts.setting("hf_token"):
                    raise ValueError("Gated models need a Hugging Face token in Settings.")
            if step.action == "install_nodes":
                node_assets = load_nodes(self._catalog.manifest_file(step.manifest_path))
        snapshot = dict(recipe=recipe.model_dump(), options=options.model_dump(), context=context,
                        identity=self._identity(host), profile=profile.model_dump() if profile else None,
                        models=[a.model_dump(mode="json") for a in model_assets],
                        nodes=[a.model_dump(mode="json") for a in node_assets])
        run = RecipeRun(host_id=host_id, recipe_name=recipe_name)
        with self._sessions() as session:
            session.add(run)
            session.flush()
            session.add(RunSnapshot(run_id=run.id, payload=json.dumps(snapshot)))
            for step in recipe.ordered_steps():
                session.add(RecipeStepRun(run_id=run.id, step_id=step.id))
            session.commit()
        if recipe.requires_preflight or any(s.action == "stop_service" for s in recipe.steps):
            self._hosts.invalidate(host_id)
            if profile:
                self._hosts.set_runtime(host_id, profile=profile.id)
        self._schedule(run.id)
        return self.get(run.id)

    def _schedule(self, run_id):
        if run_id in self._tasks and not self._tasks[run_id].done():
            return
        task = asyncio.create_task(self.execute(run_id))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))

    async def resume(self, run_id):
        run = self._require_run(run_id)
        if run.status == "completed":
            return self.get(run_id)
        self.assert_idle(run.host_id)
        snapshot = self._snapshot(run_id)
        host = self._hosts._require_host(run.host_id)
        if snapshot["identity"] != self._identity(host):
            raise ValueError("SSH identity changed. Start a new run.")
        if snapshot["options"] != self._hosts.options_for(host.id).model_dump():
            raise ValueError("Host configuration changed. Start a new run with the updated configuration.")
        run.status, run.cancel_requested = "pending", False
        self._save_run(run)
        self._schedule(run_id)
        return self.get(run_id)

    def cancel(self, run_id):
        run = self._require_run(run_id)
        if run.status in TERMINAL:
            return self.get(run_id)
        run.cancel_requested = True
        self._save_run(run)
        task = self._tasks.get(run_id)
        if task:
            task.cancel()
        self._finish(run_id, "cancelled", "Cancelled. Remote command channel closed; verify before resuming.")
        return self.get(run_id)

    async def skip_step(self, run_id, step_id):
        run = self._require_run(run_id)
        if run.status not in {"failed", "interrupted", "cancelled"}:
            raise ValueError("Only stopped runs can skip an optional step.")
        recipe = Recipe.model_validate(self._snapshot(run_id)["recipe"])
        step = next((s for s in recipe.steps if s.id == step_id), None)
        if not step:
            raise KeyError(step_id)
        if not step.optional or step.action in {"health", "smoke"}:
            raise ValueError("This step is required; it cannot be skipped.")
        state = self._ensure_step_state(run_id, step_id)
        self._set_step(run_id, step_id, "skipped", state.attempts, "Manually skipped optional step.")
        return await self.resume(run_id)

    async def close(self):
        tasks = list(self._tasks.items())
        for _, task in tasks:
            task.cancel()
        await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)
        for run_id, _ in tasks:
            if self._require_run(run_id).status != "completed":
                self._finish(run_id, "interrupted", "Server stopped. Resume explicitly to re-verify completed steps.")

    async def execute(self, run_id):
        executor = None
        current = None
        try:
            run = self._require_run(run_id)
            if run.cancel_requested:
                self._finish(run_id, "cancelled", "Run cancelled.")
                return
            snapshot = self._snapshot(run_id)
            recipe = Recipe.model_validate(snapshot["recipe"])
            host = self._hosts._require_host(run.host_id)
            executor = self._hosts.executor_for(host)
            secrets = [self._hosts.secret_for(host), self._hosts.setting("hf_token") or ""]
            options = HostOptions.model_validate(snapshot["options"])
            self._set_status(run_id, "running")
            self._log(run_id, None, "info", f"Running immutable snapshot: {recipe.name}.")
            for step in recipe.ordered_steps():
                current = step.id
                state = self._ensure_step_state(run_id, step.id)
                if state.status == "skipped" and state.message == "Manually skipped optional step." and step.optional:
                    continue
                success = await self._execute_step(run_id, step, executor, secrets, snapshot, options)
                if not success:
                    self._finish(run_id, "failed", f"Step '{step.id}' failed; inspect logs and retry.")
                    return
            if recipe.validates_backend:
                self._hosts.mark_install_ready(host.id, self._hosts.runtime_for(host.id).comfy_version)
            self._finish(run_id, "completed", "Recipe completed and all required steps verified.")
        except asyncio.CancelledError:
            if current:
                state = self._ensure_step_state(run_id, current)
                self._set_step(run_id, current, "interrupted", state.attempts, "Command interrupted; resume re-verifies.")
            self._finish(run_id, "cancelled", "Run cancelled; no readiness granted.")
        except Exception as exc:
            # Only controlled validation errors are returned. Transport errors may contain secrets.
            message = str(exc) if isinstance(exc, ValueError) else f"Execution failed ({type(exc).__name__}). Check SSH connectivity and server logs."
            if current:
                state = self._ensure_step_state(run_id, current)
                self._set_step(run_id, current, "failed", state.attempts, self._redact(message, locals().get("secrets", [])))
            self._finish(run_id, "failed", self._redact(message, locals().get("secrets", [])))
        finally:
            if executor:
                await executor.close()

    async def _execute_step(self, run_id, step, executor, secrets, snapshot, options):
        state = self._ensure_step_state(run_id, step.id)
        context = snapshot["context"]
        run = self._require_run(run_id)
        log = lambda value: self._log(run_id, step.id, "info", self._redact(value, secrets))
        self._set_step(run_id, step.id, "checking", state.attempts, None)
        if step.action == "shell":
            checked = await self._command(run_id, step.id, "check", step.check, step.timeout, executor, secrets, context)
            if checked.rc == 0:
                verified = await self._command(run_id, step.id, "verify", step.verify, step.timeout, executor, secrets, context)
                if verified.rc == 0:
                    self._set_step(run_id, step.id, "completed", state.attempts, "Existing state re-verified.")
                    return True
        for attempt in range(1, step.retries + 2):
            self._set_step(run_id, step.id, "running", state.attempts + attempt, None)
            try:
                if step.action in {"health", "smoke"}:
                    version = await check_backend(executor, options, log, step.action == "smoke")
                    values = dict(health_passed=True, comfy_version=version)
                    if step.action == "smoke":
                        values["smoke_passed"] = True
                    self._hosts.set_runtime(run.host_id, **values)
                elif step.action in {"start_service", "stop_service"}:
                    log(await manage(executor, options, "start" if step.action == "start_service" else "stop",
                                     (snapshot["profile"] or {}).get("args", [])))
                elif step.action == "install_models":
                    for data in snapshot["models"]:
                        asset = ModelAsset.model_validate(data)
                        log(f"Verifying/downloading {asset.name} with pinned SHA256.")
                        log(await download(executor, asset, context["comfy"], self._hosts.setting("hf_token"), step.timeout))
                    if not snapshot["models"]:
                        log("No models selected for this profile. Smoke verification is still required.")
                elif step.action == "install_nodes":
                    for data in snapshot["nodes"]:
                        command = node_command(NodeAsset.model_validate(data), context["comfy"], context["python"])
                        result = await self._command(run_id, step.id, "node", command, step.timeout, executor, secrets, {})
                        if result.rc:
                            raise ValueError("Pinned node installation failed. Local changes are preserved.")
                    if not snapshot["nodes"]:
                        log("No reviewed custom nodes configured. Add required pinned repositories to manifests/nodes.yaml.")
                else:
                    result = await self._command(run_id, step.id, "run", step.run, step.timeout, executor, secrets, context)
                    if result.rc:
                        raise ValueError(f"Remote command exited {result.rc}.")
                    result = await self._command(run_id, step.id, "verify", step.verify, step.timeout, executor, secrets, context)
                    if result.rc:
                        raise ValueError("Step verification failed.")
                self._set_step(run_id, step.id, "completed", state.attempts + attempt, "Verified.")
                return True
            except (ValueError, TimeoutError) as exc:
                self._log(run_id, step.id, "warn", self._redact(f"Attempt {attempt}: {exc}", secrets))
                if attempt <= step.retries:
                    await asyncio.sleep(min(attempt * 0.1, 1))
        self._set_step(run_id, step.id, "failed", state.attempts + step.retries + 1, "Retries exhausted; see logs.")
        return False

    async def _command(self, run_id, step_id, kind, command, timeout, executor, secrets, context):
        rendered = self._render(command, context)
        self._log(run_id, step_id, "info", self._redact(f"{kind}: {rendered}", secrets))
        pending = {"stdout": "", "stderr": ""}
        def output(channel, chunk):
            pending[channel] += chunk
            while "\n" in pending[channel]:
                line, pending[channel] = pending[channel].split("\n", 1)
                if line:
                    self._log(run_id, step_id, "warn" if channel == "stderr" else "info", self._redact(line, secrets))
        try:
            return await executor.run(rendered, timeout, on_output=output)
        finally:
            for channel, value in pending.items():
                if value:
                    self._log(run_id, step_id, "warn" if channel == "stderr" else "info", self._redact(value, secrets))

    @staticmethod
    def _render(command, context):
        # Replace only known variable tokens; shell/Python/JSON braces remain literal.
        def replace(match):
            key = match.group(1)
            if key not in context:
                raise ValueError(f"Unknown recipe variable: {key}")
            return shlex.quote(str(context[key]))
        return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", replace, command) if context else command

    @staticmethod
    def _redact(value, secrets):
        if isinstance(secrets, str):
            secrets = [secrets]
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"hf_[a-zA-Z0-9]+", "[REDACTED]", value)
        return value[:4000]

    def _require_run(self, run_id: str) -> RecipeRun:
        with self._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise KeyError(run_id)
            session.expunge(run)
            return run

    def _save_run(self, run: RecipeRun) -> None:
        with self._sessions() as session:
            session.merge(run)
            session.commit()

    def _set_status(self, run_id: str, status: str) -> None:
        run = self._require_run(run_id)
        run.status = status
        self._save_run(run)

    def _finish(self, run_id: str, status: str, message: str) -> None:
        self._set_status(run_id, status)
        self._log(run_id, None, "info", message)

    def _cancel_requested(self, run_id: str) -> bool:
        return self._require_run(run_id).cancel_requested

    def _step_state(self, run_id: str, step_id: str) -> RecipeStepRun | None:
        with self._sessions() as session:
            state = session.scalar(
                select(RecipeStepRun).where(RecipeStepRun.run_id == run_id, RecipeStepRun.step_id == step_id)
            )
            if state is not None:
                session.expunge(state)
            return state

    def _ensure_step_state(self, run_id: str, step_id: str) -> RecipeStepRun:
        state = self._step_state(run_id, step_id)
        if state is not None:
            return state
        state = RecipeStepRun(run_id=run_id, step_id=step_id)
        with self._sessions() as session:
            session.add(state)
            session.commit()
            session.refresh(state)
            session.expunge(state)
        return state

    def _set_step(self, run_id: str, step_id: str, status: str, attempts: int, message: str | None) -> None:
        with self._sessions() as session:
            state = session.scalar(
                select(RecipeStepRun).where(RecipeStepRun.run_id == run_id, RecipeStepRun.step_id == step_id)
            )
            if state is None:
                state = RecipeStepRun(run_id=run_id, step_id=step_id)
                session.add(state)
            state.status = status
            state.attempts = attempts
            state.message = message
            session.commit()

    def _log(self, run_id: str, step_id: str | None, level: str, message: str) -> None:
        with self._sessions() as session:
            session.add(RecipeLog(run_id=run_id, step_id=step_id, level=level, message=message[:4000]))
            session.commit()

    def get(self, run_id: str) -> RunRead:
        run = self._require_run(run_id)
        try:
            recipe = Recipe.model_validate(self._snapshot(run_id)["recipe"])
            order = {step.id: i for i, step in enumerate(recipe.ordered_steps())}
            optional = {step.id: step.optional for step in recipe.steps}
        except ValueError:
            order, optional = {}, {}
        with self._sessions() as session:
            steps = list(
                session.scalars(select(RecipeStepRun).where(RecipeStepRun.run_id == run_id).order_by(RecipeStepRun.id))
            )
        return RunRead(
            id=run.id,
            host_id=run.host_id,
            recipe_name=run.recipe_name,
            status=run.status,
            cancel_requested=run.cancel_requested,
            created_at=run.created_at,
            steps=[RunStepRead(step_id=s.step_id, status=s.status, attempts=s.attempts, message=s.message,
                               optional=optional.get(s.step_id, False))
                   for s in sorted(steps, key=lambda step: order.get(step.step_id, 999))],
        )

    async def events(self, run_id: str, after_id: int = 0) -> AsyncIterator[dict]:
        cursor = after_id
        while True:
            with self._sessions() as session:
                logs = list(
                    session.scalars(
                        select(RecipeLog).where(RecipeLog.run_id == run_id, RecipeLog.id > cursor).order_by(RecipeLog.id)
                    )
                )
            for log in logs:
                cursor = log.id
                yield {"event": "log", "id": log.id, "step_id": log.step_id, "level": log.level, "message": log.message}
            run = self.get(run_id)
            yield {"event": "state", "status": run.status, "steps": [step.model_dump() for step in run.steps]}
            if run.status in TERMINAL:
                return
            await asyncio.sleep(0.5)
