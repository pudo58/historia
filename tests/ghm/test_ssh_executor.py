import asyncssh
import pytest

from ghm.executors.ssh import SSHExecutor


class PasswordServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == "tester" and password == "test-password"


async def run_test_command(process: asyncssh.SSHServerProcess[str]) -> None:
    if process.command == "true":
        process.exit(0)
    else:
        process.stderr.write("unsupported command\n")
        process.exit(127)


@pytest.mark.asyncio
async def test_ssh_executor_requires_pinned_key_and_connects_to_real_server() -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=PasswordServer,
        process_factory=run_test_command,
        server_host_keys=[host_key],
    )
    try:
        port = server.get_port()
        unpinned = SSHExecutor("127.0.0.1", port, "tester", "password", "test-password")
        fingerprint = await unpinned.inspect_host_key()
        with pytest.raises(RuntimeError, match="confirm the SSH fingerprint"):
            await unpinned.run("true")

        pinned = SSHExecutor(
            "127.0.0.1", port, "tester", "password", "test-password", fingerprint
        )
        result = await pinned.run("true")
        assert result.rc == 0
        await pinned.close()
    finally:
        server.close()
        await server.wait_closed()
