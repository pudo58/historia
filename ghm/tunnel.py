"""Loopback-only forwarding with live health checks and bounded reconnects."""
import asyncio
from dataclasses import dataclass

import httpx

from ghm.comfy import health
from ghm.services.hosts import HostService


@dataclass
class TunnelSession:
    executor: object
    listener: object
    local_url: str
    remote_port: int
    monitor: asyncio.Task | None = None


class TunnelManager:
    def __init__(self, hosts: HostService):
        self._hosts = hosts
        self._sessions = {}
        self._locks = {}

    async def _healthy(self, url):
        async with httpx.AsyncClient(base_url=url, timeout=5, trust_env=False) as client:
            return await health(client)

    async def start(self, host_id, remote_port=None, local_port=0):
        lock = self._locks.setdefault(host_id, asyncio.Lock())
        async with lock:
            existing = self._sessions.get(host_id)
            if existing:
                await self._healthy(existing.local_url)
                return existing.local_url
            host = self._hosts._require_host(host_id)
            runtime = self._hosts.runtime_for(host_id)
            if host.state != "ready" or not runtime or not runtime.health_passed or not runtime.smoke_passed:
                raise ValueError("Run Verify backend successfully before exposing a local URL.")
            configured = self._hosts.options_for(host_id).remote_port
            if remote_port is not None and remote_port != configured:
                raise ValueError("Tunnel port must match the verified host configuration.")
            executor = self._hosts.executor_for(host)
            listener = None
            try:
                listener = await executor.forward_local_port(local_port, configured)
                url = f"http://127.0.0.1:{listener.get_port()}"
                version = await self._healthy(url)
                if version != runtime.comfy_version:
                    raise ValueError("ComfyUI version changed. Run Verify backend again.")
                entry = TunnelSession(executor, listener, url, configured)
                self._sessions[host_id] = entry
                self._hosts.set_runtime(host_id, local_url=url, tunnel_status="running")
                entry.monitor = asyncio.create_task(self._watch(host_id, entry))
                return url
            except BaseException:
                if listener:
                    listener.close()
                    await listener.wait_closed()
                await executor.close()
                raise

    async def _watch(self, host_id, entry):
        try:
            while True:
                await asyncio.sleep(10)
                try:
                    await self._healthy(entry.local_url)
                    continue
                except Exception:
                    self._hosts.set_runtime(host_id, local_url=None, tunnel_status="reconnecting")
                entry.listener.close()
                await entry.listener.wait_closed()
                await entry.executor.close()
                port = int(entry.local_url.rsplit(":", 1)[1])
                recovered = False
                for attempt in range(3):
                    await asyncio.sleep(2 ** attempt)
                    executor = self._hosts.executor_for(self._hosts._require_host(host_id))
                    listener = None
                    try:
                        listener = await executor.forward_local_port(port, entry.remote_port)
                        version = await self._healthy(entry.local_url)
                        if version != self._hosts.runtime_for(host_id).comfy_version:
                            raise ValueError("Backend version changed.")
                        entry.executor, entry.listener = executor, listener
                        self._hosts.set_runtime(host_id, local_url=entry.local_url, tunnel_status="running")
                        recovered = True
                        break
                    except Exception:
                        if listener:
                            listener.close()
                            await listener.wait_closed()
                        await executor.close()
                if not recovered:
                    self._sessions.pop(host_id, None)
                    self._hosts.invalidate(host_id, "degraded")
                    self._hosts.set_runtime(host_id, tunnel_status="failed")
                    return
        except asyncio.CancelledError:
            return

    async def backend(self, host_id):
        host = self._hosts._require_host(host_id)
        entry = self._sessions.get(host_id)
        runtime = self._hosts.runtime_for(host_id)
        if not entry or host.state != "ready" or not runtime.health_passed or not runtime.smoke_passed:
            raise ValueError("A live tunnel and successful verification are required.")
        version = await self._healthy(entry.local_url)
        if version != runtime.comfy_version:
            await self.stop(host_id)
            self._hosts.invalidate(host_id, "degraded")
            raise ValueError("Backend version changed. Re-verify before exporting.")
        return {"name": host.label, "base_url": entry.local_url, "profile": runtime.profile}

    async def stop(self, host_id):
        entry = self._sessions.pop(host_id, None)
        if entry:
            if entry.monitor:
                entry.monitor.cancel()
                await asyncio.gather(entry.monitor, return_exceptions=True)
            entry.listener.close()
            await entry.listener.wait_closed()
            await entry.executor.close()
        if self._hosts.get_host(host_id):
            self._hosts.set_runtime(host_id, local_url=None, tunnel_status="stopped")

    async def close_all(self):
        for host_id in list(self._sessions):
            await self.stop(host_id)
