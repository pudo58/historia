from collections.abc import AsyncIterator

from ghm.executors.base import CommandResult


class FakeListener:
    def __init__(self, port: int) -> None:
        self.port = port
        self.closed = False

    def get_port(self) -> int:
        return self.port

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class FakeExecutor:
    def __init__(
        self,
        fingerprint: str = "SHA256:fake-host-key",
        results: dict[str, CommandResult | list[CommandResult]] | None = None,
    ) -> None:
        self.fingerprint = fingerprint
        self.results = results or {}
        self._calls: dict[str, int] = {}
        self._next_port = 39000
        self.commands: list[str] = []

    async def inspect_host_key(self) -> str:
        return self.fingerprint

    async def run(self, command: str, timeout: float | None = None, on_output=None) -> CommandResult:
        self.commands.append(command)
        configured = self.results.get(command, CommandResult(0, "", ""))
        if not isinstance(configured, list):
            if on_output:
                on_output("stdout", configured.stdout)
                on_output("stderr", configured.stderr)
            return configured
        index = self._calls.get(command, 0)
        self._calls[command] = index + 1
        result = configured[min(index, len(configured) - 1)]
        if on_output:
            on_output("stdout", result.stdout)
            on_output("stderr", result.stderr)
        return result

    async def stream(self, command: str, timeout: float | None = None) -> AsyncIterator[str]:
        result = await self.run(command, timeout)
        for line in result.stdout.splitlines():
            yield line

    async def upload(self, source: str, destination: str) -> None:
        return None

    async def download(self, source: str, destination: str) -> None:
        return None

    async def run_input(self, command: str, data: str, timeout: float = 60, on_output=None) -> CommandResult:
        return await self.run(command, timeout, on_output)

    async def close(self) -> None:
        return None

    async def forward_local_port(self, local_port: int, remote_port: int) -> FakeListener:
        listener = FakeListener(local_port or self._next_port)
        self._next_port += 1
        return listener
