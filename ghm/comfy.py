"""Real ComfyUI API validation over an ephemeral private SSH forward."""
import asyncio
import time
from uuid import uuid4

import httpx

from ghm.executors.http import comfy_client


async def health(client: httpx.AsyncClient) -> str:
    response = await client.get("/system_stats")
    response.raise_for_status()
    data = response.json()
    version = data.get("system", {}).get("comfyui_version")
    if not isinstance(version, str) or not isinstance(data.get("devices"), list):
        raise ValueError("Endpoint is not a valid ComfyUI /system_stats response.")  # noqa: TRY004 -- invalid remote data, not caller type
    return version


async def smoke(client: httpx.AsyncClient, graph: dict, timeout: int, log) -> str:
    prompt_id = str(uuid4())
    # A unique supplied ID lets us cancel only our own prompt, never someone else's job.
    response = await client.post("/prompt", json={"prompt": graph, "prompt_id": prompt_id})
    if response.status_code != 200:
        raise ValueError("ComfyUI rejected the workflow. Check installed nodes, models and API-format inputs.")
    data = response.json()
    if data.get("prompt_id") != prompt_id or data.get("node_errors"):
        raise ValueError("ComfyUI did not accept the complete smoke workflow.")
    log(f"Smoke prompt queued: {prompt_id}")
    finished = False
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = await client.get(f"/history/{prompt_id}")
            response.raise_for_status()
            item = response.json().get(prompt_id)
            if item:
                status = item.get("status", {})
                if status.get("status_str") == "error":
                    raise ValueError("Smoke workflow failed on the GPU. Check ComfyUI logs and model compatibility.")
                if status.get("completed") is True and status.get("status_str") == "success":
                    if not item.get("outputs") or not any(item["outputs"].values()):
                        raise ValueError("Smoke workflow completed without output. Use an output/save node.")
                    log("Smoke workflow completed successfully with output.")
                    finished = True
                    return prompt_id
            await asyncio.sleep(1)
        raise ValueError("Smoke workflow timed out; the host is not ready.")
    finally:
        if not finished:
            # Best effort and bounded. These endpoints are prompt-ID scoped in the pinned release.
            try:
                await client.post("/queue", json={"delete": [prompt_id]}, timeout=5)
                await client.post("/interrupt", json={"prompt_id": prompt_id}, timeout=5)
            except (httpx.HTTPError, asyncio.CancelledError):
                pass


async def check_backend(executor, options, log, with_smoke: bool) -> str:
    if with_smoke and not options.workflow:
        raise ValueError("Save a real API-format workflow under Host configuration before the smoke test.")
    async with comfy_client(executor, options.remote_port, timeout=15) as client:
        version = await health(client)
        log(f"ComfyUI {version}: health check passed.")
        if with_smoke:
            await smoke(client, options.workflow, options.smoke_timeout, log)
        return version
