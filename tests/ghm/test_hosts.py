from pathlib import Path

import pytest

from ghm.database import make_session_factory
from ghm.executors.fake import FakeExecutor
from ghm.schemas import HostCreate
from ghm.security import SecretStore
from ghm.services.hosts import HostService


def make_service(tmp_path: Path) -> HostService:
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'hosts.db'}")
    return HostService(sessions, SecretStore("test-key"), lambda host, secret: FakeExecutor())


@pytest.mark.asyncio
async def test_host_key_must_be_inspected_then_confirmed(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    host = service.create_host(HostCreate(label="Test GPU", address="10.0.0.1", username="ubuntu", auth_kind="password", secret="pw"))
    inspected = await service.inspect_key(host.id)
    assert inspected.pending_fingerprint == "SHA256:fake-host-key"
    confirmed = service.confirm_key(host.id, "SHA256:fake-host-key")
    assert confirmed.pinned_fingerprint == "SHA256:fake-host-key"
    assert confirmed.pending_fingerprint is None


def test_wrong_fingerprint_cannot_be_pinned(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    host = service.create_host(HostCreate(label="Test", address="host", username="user", auth_kind="password", secret="pw"))
    with pytest.raises(ValueError, match="Fingerprint"):
        service.confirm_key(host.id, "SHA256:attacker")
