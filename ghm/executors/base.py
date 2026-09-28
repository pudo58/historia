from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass(frozen=True)
class CommandResult:
    rc: int
    stdout: str
    stderr: str


class Executor(Protocol):
    async def inspect_host_key(self) -> str: ...

    async def run(self, command: str, timeout: float | None = None, on_output=None) -> CommandResult: ...

    async def stream(self, command: str, timeout: float | None = None) -> AsyncIterator[str]: ...

    async def upload(self, source: str, destination: str) -> None: ...

    async def download(self, source: str, destination: str) -> None: ...

    async def close(self) -> None: ...

    async def forward_local_port(self, local_port: int, remote_port: int): ...

    async def run_input(self, command: str, data: str, timeout: float = 60, on_output=None) -> CommandResult: ...
