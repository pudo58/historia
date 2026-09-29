import asyncssh
import pytest

from ghm.executors.ssh import SSHExecutor


@pytest.fixture
def stale_pub_home(tmp_path, monkeypatch):
    ssh = tmp_path / '.ssh'
    ssh.mkdir()
    private, other = asyncssh.generate_private_key('ssh-ed25519'), asyncssh.generate_private_key('ssh-ed25519')
    private.write_private_key(str(ssh / 'id_ed25519'))
    other.write_public_key(str(ssh / 'id_ed25519.pub'))  # stale sidecar that does not match
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    return ssh / 'id_ed25519'


@pytest.mark.asyncio
async def test_host_key_inspection_ignores_default_keys_with_stale_pub(stale_pub_home):
    host_key = asyncssh.generate_private_key('ssh-ed25519')
    server = await asyncssh.create_server(asyncssh.SSHServer, '127.0.0.1', 0, server_host_keys=[host_key])
    try:
        port = server.sockets[0].getsockname()[1]
        fingerprint = await SSHExecutor('127.0.0.1', port, 'u', 'private_key', str(stale_pub_home)).inspect_host_key()
        assert fingerprint == host_key.get_fingerprint('sha256')
    finally:
        server.close()


def test_private_key_loads_despite_stale_pub_and_reports_bad_files(stale_pub_home, tmp_path):
    options = SSHExecutor('h', 22, 'u', 'private_key', str(stale_pub_home))._options()
    assert len(options['client_keys']) == 1
    with pytest.raises(ValueError, match='Không tìm thấy'):
        SSHExecutor('h', 22, 'u', 'private_key', str(tmp_path / 'missing'))._options()
    junk = tmp_path / 'junk'
    junk.write_text('not a key')
    with pytest.raises(ValueError, match='Không đọc được'):
        SSHExecutor('h', 22, 'u', 'private_key', str(junk))._options()
