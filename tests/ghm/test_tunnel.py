import pytest

from ghm.database import make_session_factory
from ghm.executors.fake import FakeExecutor
from ghm.schemas import HostCreate
from ghm.security import SecretStore
from ghm.services.hosts import HostService
from ghm.tunnel import TunnelManager


@pytest.mark.asyncio
async def test_tunnel_lifecycle_is_local_and_persisted(tmp_path, monkeypatch) -> None:
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'tunnel.db'}")
    hosts = HostService(sessions, SecretStore("test-key"), lambda host, secret: FakeExecutor())
    host = hosts.create_host(
        HostCreate(label="Ready", address="gpu", username="user", auth_kind="password", secret="credential")
    )
    host.pending_fingerprint = "SHA256:test"
    hosts._save(host)
    hosts.confirm_key(host.id, "SHA256:test")
    with pytest.raises(ValueError, match="actual workflow smoke"):
        hosts.mark_install_ready(host.id, "v0.36.0")
    hosts.set_runtime(host.id, health_passed=True, smoke_passed=True, comfy_version="v0.36.0")
    hosts.mark_install_ready(host.id, "v0.36.0")
    tunnels = TunnelManager(hosts)
    async def healthy(url):
        assert url.startswith("http://127.0.0.1:")
        return "v0.36.0"
    monkeypatch.setattr(tunnels, "_healthy", healthy)

    url = await tunnels.start(host.id, remote_port=8188)
    assert url == "http://127.0.0.1:39000"
    assert hosts.runtime_for(host.id).tunnel_status == "running"
    await tunnels.stop(host.id)
    runtime = hosts.runtime_for(host.id)
    assert runtime is not None
    assert runtime.tunnel_status == "stopped"
    assert runtime.local_url is None
