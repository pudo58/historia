from collections.abc import Callable

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ghm.executors.base import Executor
from ghm.models import Host, HostRuntime, PreflightSnapshot, HostConfiguration, LocalSetting
from ghm.preflight import collect, evaluate
from ghm.schemas import HostCreate, HostUpdate, PreflightReport, StoredPreflightReport, HostOptions
from ghm.security import SecretStore
from ghm.executors.pty_probe import PreflightTransportError


class HostService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        secrets: SecretStore,
        executor_factory: Callable[[Host, str], Executor],
    ) -> None:
        self._sessions = sessions
        self._secrets = secrets
        self._executor_factory = executor_factory

    def list_hosts(self) -> list[Host]:
        with self._sessions() as session:
            return list(session.scalars(select(Host).order_by(Host.created_at.desc())))

    def get_host(self, host_id: str) -> Host | None:
        with self._sessions() as session:
            return session.get(Host, host_id)

    def create_host(self, data: HostCreate) -> Host:
        host = Host(
            label=data.label,
            address=data.address,
            port=data.port,
            username=data.username,
            auth_kind=data.auth_kind,
            encrypted_secret=self._secrets.encrypt(data.secret),
        )
        with self._sessions() as session:
            session.add(host)
            session.commit()
            session.refresh(host)
            return host

    def update_host(self, host_id: str, data: HostUpdate) -> Host:
        host = self._require_host(host_id)
        changes = data.model_dump(exclude_unset=True)
        secret = changes.pop("secret", None)
        for field, value in changes.items():
            setattr(host, field, value)
        if secret is not None:
            host.encrypted_secret = self._secrets.encrypt(secret)
        if {"address", "port", "username", "auth_kind", "secret"}.intersection(changes.keys() | ({"secret"} if secret else set())):
            host.pinned_fingerprint = None
            host.pending_fingerprint = None
            host.state = "needs_setup"
            self.invalidate(host_id)
        self._save(host)
        return host

    def delete_host(self, host_id: str) -> None:
        """Forget local connection metadata, never terminate a pod or remove remote files."""
        from sqlalchemy import select, text
        from ghm.models import HostRuntime, RecipeRun
        from studio.jobs import ACTIVE, StudioJobs
        from studio.models import Job, Project
        with self._sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            host = session.get(Host, host_id)
            if host is None:
                raise KeyError(host_id)
            jobs = list(session.scalars(select(Job).where(Job.host_id == host_id)))
            if any(job.status in ACTIVE or (job.status not in {'completed', 'cancelled'} and StudioJobs.remote_pending(job.result)) for job in jobs):
                raise ValueError('GPU còn tác vụ Studio đang chạy/chờ hoặc chưa đối chiếu; chưa thể xóa.')
            if session.scalar(select(RecipeRun.id).where(RecipeRun.host_id == host_id,
                    RecipeRun.status.in_(['pending', 'running']))):
                raise ValueError('A recipe run is active on this host.')
            runtime = session.get(HostRuntime, host_id)
            if runtime and (runtime.local_url or runtime.tunnel_status not in {'stopped', 'failed'}):
                raise ValueError('Stop the active tunnel before deleting this GPU connection.')
            # Preserve historical jobs/artifacts while releasing the RESTRICT foreign key.
            for job in jobs:
                job.host_id = None
            for project in session.scalars(select(Project)):
                if project.data.get('host_id') == host_id:
                    project.data = {**project.data, 'host_id': None}
            session.flush()
            session.delete(host)
            session.commit()

    async def preflight(self, host_id: str) -> StoredPreflightReport:
        host = self._require_host(host_id)
        if not host.pinned_fingerprint:
            raise ValueError("Confirm the SSH host key before running preflight.")
        executor = self._executor_factory(host, self._secrets.decrypt(host.encrypted_secret))
        try:
            report = evaluate((await collect(executor)).values)
        except Exception as exc:
            host.state = "unreachable"
            host.last_error = str(exc) if isinstance(exc, PreflightTransportError) else "Không thực hiện được lệnh kiểm tra qua SSH. Chưa thể xác định GPU, RAM hoặc Python."
            self._save(host)
            raise RuntimeError(host.last_error) from exc
        finally:
            await executor.close()
        if report.status == "fail":
            host.state = "degraded"
            self.invalidate(host_id)
        elif host.state != "ready":
            host.state = "needs_setup"
        host.last_error = None
        snapshot = PreflightSnapshot(host_id=host.id, status=report.status, payload=report.model_dump_json())
        with self._sessions() as session:
            session.merge(host)
            session.add(snapshot)
            session.commit()
            session.refresh(snapshot)
        return StoredPreflightReport(**report.model_dump(), created_at=snapshot.created_at)

    def latest_preflight(self, host_id: str) -> StoredPreflightReport | None:
        self._require_host(host_id)
        with self._sessions() as session:
            snapshot = session.scalar(
                select(PreflightSnapshot)
                .where(PreflightSnapshot.host_id == host_id)
                .order_by(PreflightSnapshot.created_at.desc())
            )
        if snapshot is None:
            return None
        report = PreflightReport.model_validate_json(snapshot.payload)
        return StoredPreflightReport(**report.model_dump(), created_at=snapshot.created_at)

    async def inspect_key(self, host_id: str) -> Host:
        host = self._require_host(host_id)
        executor = self._executor_factory(host, self._secrets.decrypt(host.encrypted_secret))
        try:
            host.pending_fingerprint = await executor.inspect_host_key()
            host.last_error = None
            self._save(host)
            return host
        except Exception as exc:
            host.state = "unreachable"
            host.last_error = "Could not reach this host to inspect its SSH key."
            self._save(host)
            raise RuntimeError(host.last_error) from exc
        finally:
            await executor.close()

    def confirm_key(self, host_id: str, fingerprint: str) -> Host:
        host = self._require_host(host_id)
        if host.pending_fingerprint != fingerprint:
            raise ValueError("Fingerprint does not match the currently inspected host key.")
        host.pinned_fingerprint = fingerprint
        host.pending_fingerprint = None
        host.state = "needs_setup"
        self.invalidate(host_id)
        self._save(host)
        return host

    def _require_host(self, host_id: str) -> Host:
        host = self.get_host(host_id)
        if host is None:
            raise KeyError(host_id)
        return host

    def _save(self, host: Host) -> None:
        with self._sessions() as session:
            session.merge(host)
            session.commit()

    def secret_for(self, host: Host) -> str:
        return self._secrets.decrypt(host.encrypted_secret)

    def executor_for(self, host: Host) -> Executor:
        return self._executor_factory(host, self.secret_for(host))

    def runtime_for(self, host_id: str) -> HostRuntime | None:
        with self._sessions() as session:
            runtime = session.get(HostRuntime, host_id)
            if runtime is not None:
                session.expunge(runtime)
            return runtime

    def set_runtime(self, host_id: str, **values: object) -> HostRuntime:
        with self._sessions() as session:
            runtime = session.get(HostRuntime, host_id)
            if runtime is None:
                runtime = HostRuntime(host_id=host_id)
                session.add(runtime)
            for key, value in values.items():
                setattr(runtime, key, value)
            session.commit()
            session.refresh(runtime)
            session.expunge(runtime)
            return runtime

    def mark_install_ready(self, host_id: str, comfy_version: str | None = None) -> None:
        runtime = self.runtime_for(host_id)
        if not runtime or not runtime.health_passed or not runtime.smoke_passed:
            raise ValueError("Health and an actual workflow smoke test must pass before ready.")
        host = self._require_host(host_id)
        host.state = "ready"
        host.last_error = None
        self._save(host)
        self.set_runtime(host_id, comfy_version=comfy_version)

    def invalidate(self, host_id: str, state: str = "needs_setup") -> None:
        host = self._require_host(host_id)
        host.state = state
        self._save(host)
        self.set_runtime(host_id, health_passed=False, smoke_passed=False, local_url=None,
                         tunnel_status="stopped", comfy_version=None)

    def options_for(self, host_id: str) -> HostOptions:
        self._require_host(host_id)
        with self._sessions() as session:
            row = session.get(HostConfiguration, host_id)
            return HostOptions.model_validate_json(row.payload) if row else HostOptions()

    def save_options(self, host_id: str, options: HostOptions) -> HostOptions:
        self._require_host(host_id)
        with self._sessions() as session:
            session.merge(HostConfiguration(host_id=host_id, payload=options.model_dump_json()))
            session.commit()
        self.invalidate(host_id)
        return options

    def setting(self, name: str) -> str | None:
        with self._sessions() as session:
            value = session.get(LocalSetting, name)
            return self._secrets.decrypt(value.encrypted_value) if value else None

    def save_setting(self, name: str, value: str) -> None:
        with self._sessions() as session:
            row = session.get(LocalSetting, name)
            if not value:
                if row:
                    session.delete(row)
            else:
                session.merge(LocalSetting(name=name, encrypted_value=self._secrets.encrypt(value)))
            session.commit()
