"""Local-only SSH fixture for manually exercising the Phase 1 UI."""

import argparse
import asyncio

import asyncssh

from ghm.preflight import COMMANDS


class TestSSHServer(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == "tester" and password == "test-password"


async def serve_command(process: asyncssh.SSHServerProcess[str]) -> None:
    responses = {
        "false": None,
        "true": "",
        COMMANDS["os"]: 'PRETTY_NAME="Ubuntu 24.04.2 LTS"\n',
        COMMANDS["identity"]: "1000\n",
        COMMANDS["container"]: "docker\n",
        COMMANDS["gpu"]: "NVIDIA GeForce RTX 5090, 32607, 570.86.15, 12.0\n",
        COMMANDS["cuda"]: "Cuda compilation tools, release 12.8, V12.8.61\n",
        COMMANDS["memory"]: "Mem: 68719476736 0 0 0 0 0\n",
        COMMANDS["disk"]: "Filesystem 1B-blocks Used Available Use% Mounted on\noverlay 536870912000 0 429496729600 20% /\n",
        COMMANDS["python"]: "Python 3.11.9\n",
        COMMANDS["network"]: "",
    }
    if process.command == "false":
        process.exit(1)
        return
    response = responses.get(process.command)
    if response is None:
        process.stderr.write("Unsupported command on local test fixture.\n")
        process.exit(127)
        return
    process.stdout.write(response)
    process.exit(0)


async def serve(port: int) -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.listen(
        "127.0.0.1",
        port,
        server_factory=TestSSHServer,
        process_factory=serve_command,
        server_host_keys=[host_key],
    )
    print(f"Local test SSH server listening on 127.0.0.1:{server.get_port()}")
    print("Credentials: tester / test-password")
    try:
        await asyncio.Future()
    finally:
        server.close()
        await server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local-only GHM SSH test fixture.")
    parser.add_argument("--port", type=int, default=2222)
    args = parser.parse_args()
    asyncio.run(serve(args.port))


if __name__ == "__main__":
    main()
