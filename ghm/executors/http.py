"""ComfyUI over private SSH: direct forwarding or terminal-only RPC."""
from contextlib import asynccontextmanager

import httpx

from ghm.executors.terminal import TerminalHTTP


@asynccontextmanager
async def comfy_client(executor, port, timeout=30):
    listener = None
    try:
        if getattr(executor, 'terminal_only', False):
            client = httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',
                                      transport=TerminalHTTP(executor, port), timeout=timeout, trust_env=False)
        else:
            listener = await executor.forward_local_port(0, port)
            client = httpx.AsyncClient(base_url=f'http://127.0.0.1:{listener.get_port()}', timeout=timeout, trust_env=False)
        async with client:
            yield client
    finally:
        if listener:
            listener.close()
            await listener.wait_closed()
