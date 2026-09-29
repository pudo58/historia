from contextlib import asynccontextmanager
import asyncio
import json
import os
from pathlib import Path
import tempfile
from typing import Callable

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from ghm.config import Settings
from ghm.database import make_session_factory
from ghm.executors.base import Executor
from ghm.executors.ssh import SSHExecutor
from ghm.models import Host
from ghm.recipe_runner import RecipeRunner
from ghm import runpod
from ghm.recipes import RecipeCatalog
from ghm.tunnel import TunnelManager
from ghm.schemas import HostCreate, HostUpdate, HostRead, HostKeyConfirmation, RunPodConnect, RunStart, HostOptions, TokenUpdate
from ghm.security import SecretStore
from ghm.services.hosts import HostService
from ghm.manifests import load_models, load_nodes, resolve_model
from ghm.profiles import load_profiles
from studio.api import router as studio_router
from studio.service import StudioService
from studio.jobs import StudioJobs
from sqlalchemy.engine import make_url


class ModelChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    license_accepted: bool | None = None
    filename: str | None = None


def _to_read(host, service):
    value = HostRead.model_validate(host, from_attributes=True)
    report, runtime = service.latest_preflight(host.id), service.runtime_for(host.id)
    return value.model_copy(update={
        "local_url": runtime.local_url if runtime and host.state == "ready" else None,
        "tunnel_status": runtime.tunnel_status if runtime else "stopped",
        "health_passed": runtime.health_passed if runtime else False,
        "smoke_passed": runtime.smoke_passed if runtime else False,
        "gpu": report.gpu if report else None,
        "vram_gb": report.gpu.vram_gb if report and report.gpu else None,
        "ram_gb": report.ram_gb if report else None,
        "comfy_version": runtime.comfy_version if runtime else None,
        "profile": runtime.profile if runtime else None,
    })


def create_app(settings: Settings | None = None, secret_store: SecretStore | None = None,
               executor_factory: Callable[[Host, str], Executor] | None = None):
    config = settings or Settings()
    sessions = make_session_factory(config.database_url)
    secrets = secret_store or SecretStore.from_environment()
    factory = executor_factory or (lambda h, s: SSHExecutor(h.address, h.port, h.username, h.auth_kind, s, h.pinned_fingerprint))
    service = HostService(sessions, secrets, factory)
    catalog = RecipeCatalog(config.recipes_dir)
    runner = RecipeRunner(sessions, catalog, service)
    database_path = make_url(config.database_url).database
    studio_root = config.studio_root or Path(database_path or "ghm.db").resolve().parent / "studio-data"
    studio = StudioService(sessions, studio_root)
    studio_jobs = StudioJobs(studio, service, runner)
    runner.studio_idle = studio_jobs.host_idle
    tunnels = TunnelManager(service)
    locks = {}
    manifest_lock = asyncio.Lock()

    def host_lock(id):
        return locks.setdefault(id, asyncio.Lock())

    @asynccontextmanager
    async def lifespan(app):
        runner.recover()
        studio_jobs.start()
        yield
        await studio_jobs.close()
        await runner.close()
        await tunnels.close_all()

    app = FastAPI(title="Historical Video Studio", version="0.3.0", lifespan=lifespan)
    app.state.studio, app.state.studio_jobs = studio, studio_jobs
    app.include_router(studio_router(studio, studio_jobs, host_lock))
    app.state.host_service, app.state.recipe_runner, app.state.tunnel_manager = service, runner, tunnels
    origins = {config.local_origin, "http://127.0.0.1:8000", "http://localhost:8000"}
    # Ephemeral Cloudflare quick tunnels (trycloudflare.com) used for remote UI access.
    # Host stays loopback via cloudflared --http-host-header; only Origin is public HTTPS.
    trycloudflare_origin_re = r"https://[a-z0-9-]+\.trycloudflare\.com"
    extra_origins = {o.strip() for o in os.environ.get("GHM_EXTRA_ORIGINS", "").split(",") if o.strip()}
    origins |= extra_origins
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"] + (["testserver"] if executor_factory else []))
    app.add_middleware(CORSMiddleware, allow_origins=list(origins), allow_origin_regex=trycloudflare_origin_re,
                       allow_methods=["GET","POST","PATCH","DELETE"],
                       allow_headers=["content-type", "last-event-id"])

    @app.middleware("http")
    async def local_only(request, call_next):
        import re
        origin = request.headers.get("origin")
        if origin and origin not in origins and not re.fullmatch(trycloudflare_origin_re, origin):
            return JSONResponse({"detail": "Untrusted browser origin."}, status_code=403)
        upload = request.url.path.startswith("/api/studio/projects/") and request.url.path.endswith("/upload")
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid content length."}, status_code=400)
        if upload and request.method == "POST":
            if request.headers.get("content-type") != "application/octet-stream":
                return JSONResponse({"detail": "Binary upload required."}, status_code=415)
            if length > 250_000_000:
                return JSONResponse({"detail": "Upload exceeds 250 MB."}, status_code=413)
        elif request.method in {"POST", "PATCH"} and (length > 0 or request.headers.get("transfer-encoding")):
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "JSON content type required."}, status_code=415)
            if length > 2_000_000:
                return JSONResponse({"detail": "Request exceeds 2 MB."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default input field can reflect passwords/tokens.
        return JSONResponse({"detail": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        # Controlled application errors only. No request values or secret-store errors.
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "Host, recipe, run or asset not found."}, status_code=404)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(request, exc):
        return JSONResponse({"detail": "Backend health check failed. Check SSH/ComfyUI and run Verify backend."}, status_code=502)

    @app.get("/api/status")
    def status():
        return {"version": "0.2.0", "local_only": True, "billing_notice": "Stopping ComfyUI or its tunnel does not stop GPU rental billing."}

    @app.get("/api/hosts", response_model=list[HostRead])
    def list_hosts():
        return [_to_read(h, service) for h in service.list_hosts()]

    @app.post("/api/hosts", response_model=HostRead, status_code=201)
    def create_host(payload: HostCreate):
        return _to_read(service.create_host(payload), service)

    @app.get("/api/hosts/{host_id}", response_model=HostRead)
    def get_host(host_id: str):
        return _to_read(service._require_host(host_id), service)

    @app.patch("/api/hosts/{host_id}", response_model=HostRead)
    async def update_host(host_id: str, payload: HostUpdate):
        async with host_lock(host_id):
            runner.assert_idle(host_id)
            await tunnels.stop(host_id)
            return _to_read(service.update_host(host_id, payload), service)

    @app.delete("/api/hosts/{host_id}", status_code=204)
    async def delete_host(host_id: str):
        async with host_lock(host_id):
            service._require_host(host_id)
            runner.assert_idle(host_id)
            service.delete_host(host_id)

    @app.post("/api/hosts/{host_id}/inspect-key")
    async def inspect_key(host_id: str):
        async with host_lock(host_id):
            runner.assert_idle(host_id)
            try:
                h = await service.inspect_key(host_id)
                return {"fingerprint": h.pending_fingerprint, "host": h.address, "port": h.port,
                        "changed": bool(h.pinned_fingerprint and h.pinned_fingerprint != h.pending_fingerprint)}
            except RuntimeError as exc:
                raise HTTPException(502, str(exc)) from exc

    @app.post("/api/hosts/{host_id}/confirm-key", response_model=HostRead)
    async def confirm_key(host_id: str, payload: HostKeyConfirmation):
        async with host_lock(host_id):
            runner.assert_idle(host_id)
            await tunnels.stop(host_id)
            return _to_read(service.confirm_key(host_id, payload.fingerprint), service)

    @app.get("/api/hosts/{host_id}/preflight")
    def latest_preflight(host_id: str):
        return service.latest_preflight(host_id)

    @app.post("/api/hosts/{host_id}/preflight")
    async def preflight(host_id: str):
        async with host_lock(host_id):
            runner.assert_idle(host_id)
            try:
                report = await service.preflight(host_id)
                if report.status == "fail":
                    await tunnels.stop(host_id)
                return report
            except RuntimeError as exc:
                await tunnels.stop(host_id)
                raise HTTPException(502, str(exc)) from exc

    @app.get("/api/hosts/{host_id}/options")
    def options(host_id: str):
        return service.options_for(host_id)

    @app.patch("/api/hosts/{host_id}/options")
    async def save_options(host_id: str, payload: HostOptions):
        async with host_lock(host_id):
            runner.assert_idle(host_id)
            profiles = load_profiles(catalog.manifest_file("manifests/profiles.yaml"))
            if payload.profile not in {"auto", *(p.id for p in profiles)}:
                raise ValueError("Unknown GPU profile.")
            await tunnels.stop(host_id)
            return service.save_options(host_id, payload)

    @app.get("/api/recipes")
    def recipes():
        return [r.model_dump() for r in catalog.list()]

    @app.get("/api/profiles")
    def profiles():
        return load_profiles(catalog.manifest_file("manifests/profiles.yaml"))

    @app.get("/api/runs")
    def runs(host_id: str | None = None):
        if host_id:
            service._require_host(host_id)
        return runner.list(host_id)

    @app.post("/api/hosts/{host_id}/runs", status_code=202)
    async def start_run(host_id: str, payload: RunStart):
        async with host_lock(host_id):
            runner.assert_idle(host_id)
            # Never leave a published URL pointing to a service being modified.
            await tunnels.stop(host_id)
            return await runner.start(host_id, payload.recipe_name)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return runner.get(run_id)

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str):
        run = runner.get(run_id)
        async with host_lock(run.host_id):
            await tunnels.stop(run.host_id)
            return await runner.resume(run_id)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        return runner.cancel(run_id)

    @app.post("/api/runs/{run_id}/steps/{step_id}/skip", status_code=202)
    async def skip_step(run_id: str, step_id: str):
        return await runner.skip_step(run_id, step_id)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after_id: int = Query(default=0, ge=0)):
        runner.get(run_id)
        try:
            cursor = max(after_id, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor.")
        async def stream():
            async for event in runner.events(run_id, cursor):
                if await request.is_disconnected():
                    break
                kind = event.pop("event")
                id_line = f"id: {event['id']}\n" if "id" in event else ""
                yield f"{id_line}event: {kind}\ndata: {json.dumps(event)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/hosts/{host_id}/tunnel", response_model=HostRead)
    async def start_tunnel(host_id: str, local_port: int = Query(default=0, ge=0, le=65535)):
        async with host_lock(host_id):
            runner.assert_idle(host_id)
            try:
                await tunnels.start(host_id, local_port=local_port)
            except (ValueError, KeyError, httpx.HTTPError):
                raise
            except Exception as exc:
                raise HTTPException(502, "Could not establish the SSH forward.") from exc
            return _to_read(service._require_host(host_id), service)

    @app.delete("/api/hosts/{host_id}/tunnel", status_code=204)
    async def stop_tunnel(host_id: str):
        async with host_lock(host_id):
            service._require_host(host_id)
            await tunnels.stop(host_id)

    @app.get("/api/hosts/{host_id}/backend")
    async def backend(host_id: str):
        return await tunnels.backend(host_id)

    @app.get("/api/settings")
    def get_settings():
        return {"hf_token_configured": bool(service.setting("hf_token")),
                "runpod_configured": bool(service.setting("runpod_api_key")),
                "nodes_editable_in_ui": False, "manifests_directory": str(config.recipes_dir.parent / "manifests")}

    @app.post("/api/settings/hf-token")
    def save_token(payload: TokenUpdate):
        if payload.token and (not payload.token.startswith("hf_") or any(c.isspace() for c in payload.token)):
            raise HTTPException(422, "Enter a valid read-only Hugging Face token, or an empty value to remove it.")
        service.save_setting("hf_token", payload.token)
        return {"hf_token_configured": bool(payload.token)}

    @app.get("/api/runpod/pods")
    async def runpod_pods():
        key = service.setting("runpod_api_key")
        if not key:
            return {"configured": False, "pods": []}
        try:
            pods = await runpod.fetch_pods(key)
        except runpod.RunPodError as error:
            return {"configured": True, "pods": [], "error": str(error)}
        with sessions() as session:
            hosts = [{"id": h.id, "label": h.label, "address": h.address, "port": h.port, "username": h.username}
                     for h in session.query(Host).all()]
        summary = runpod.summarize(pods, hosts)
        labels = {h["id"]: h["label"] for h in hosts}
        for pod in summary["pods"]:
            linked = service.setting(f"runpod_pod_host:{pod['id']}")
            pod["linked_host_id"] = linked if linked in labels and not pod["host_id"] else None
            pod["linked_host_label"] = labels.get(pod["linked_host_id"])
        return {"configured": True, "ssh_key_path": service.setting("runpod_ssh_key_path") or "", **summary}

    def normalize_key_path(raw: str) -> str:
        path = os.path.abspath(os.path.expanduser(raw.strip()))
        if not os.path.isfile(path):
            raise HTTPException(422, "Không tìm thấy file khóa SSH riêng tại đường dẫn này (trên máy chạy Historia).")
        return path

    @app.post("/api/settings/runpod-ssh-key")
    def save_runpod_ssh_key(payload: TokenUpdate):
        path = normalize_key_path(payload.token) if payload.token.strip() else ""
        service.save_setting("runpod_ssh_key_path", path)
        return {"ssh_key_path": path}

    @app.post("/api/runpod/pods/{pod_id}/connect")
    async def connect_runpod_pod(pod_id: str, payload: RunPodConnect | None = None):
        """Create (or re-point) the Historia host for a running Pod: direct SSH when the Pod exposes TCP 22,
        otherwise RunPod's Basic-SSH gateway. Never trusts the host key by itself."""
        payload = payload or RunPodConnect()
        key = service.setting("runpod_api_key")
        key_path = service.setting("runpod_ssh_key_path")
        if not key:
            raise HTTPException(409, "Chưa nhập RunPod API key.")
        if not key_path:
            raise HTTPException(409, "Chưa chọn file khóa SSH riêng để đăng nhập Pod.")
        try:
            pods = await runpod.fetch_pods(key)
        except runpod.RunPodError as error:
            raise HTTPException(502, str(error)) from error
        raw = next((p for p in pods if p.get("id") == pod_id), None)
        if raw is None:
            raise HTTPException(404, "Không thấy Pod này trên RunPod.")
        pod = runpod.summarize([raw], [])["pods"][0]
        if pod["status"] != "RUNNING":
            raise HTTPException(409, "Pod chưa chạy. Bật Pod rồi thử lại.")
        if payload.mode == "direct" or (payload.mode == "auto" and pod["ssh_ready"]):
            if not pod["ssh_ready"]:
                raise HTTPException(409, "Pod chưa có IP/cổng SSH (TCP 22). Chờ khởi động xong hoặc nối qua proxy.")
            address, port, username = pod["public_ip"], int(pod["ssh_port"]), "root"
        else:
            username = runpod.proxy_username(raw, payload.ssh_command)
            if not username:
                raise HTTPException(409, "Cần lệnh SSH ở tab Connect của Pod (dạng ssh <pod-id>-<mã>@ssh.runpod.io ...) để nối qua proxy.")
            address, port = runpod.PROXY_HOST, 22
        with sessions() as session:
            existing = next((h.id for h in session.query(Host).all()
                             if h.address == address and h.port == port and h.username == username), None)
        if existing:
            service.save_setting(f"runpod_pod_host:{pod_id}", existing)
            return {"host": _to_read(service._require_host(existing), service), "action": "already_connected"}
        linked = service.setting(f"runpod_pod_host:{pod_id}")
        if linked and service.get_host(linked):
            async with host_lock(linked):
                runner.assert_idle(linked)
                await tunnels.stop(linked)
                host = service.update_host(linked, HostUpdate(address=address, port=port, username=username))
            return {"host": _to_read(host, service), "action": "address_updated"}
        host = service.create_host(HostCreate(label=pod["name"] or pod_id, address=address, port=port,
                                              username=username, auth_kind="private_key", secret=key_path))
        service.save_setting(f"runpod_pod_host:{pod_id}", host.id)
        return {"host": _to_read(host, service), "action": "created"}

    @app.post("/api/settings/runpod-key")
    def save_runpod_key(payload: TokenUpdate):
        if payload.token and (len(payload.token) < 16 or any(c.isspace() for c in payload.token)):
            raise HTTPException(422, "Nhập RunPod API key hợp lệ, hoặc để trống để xóa.")
        service.save_setting("runpod_api_key", payload.token)
        return {"runpod_configured": bool(payload.token)}

    def model_path():
        return catalog.manifest_file("manifests/models.yaml")

    def save_models(models):
        path = model_path()
        payload = yaml.safe_dump({"models": [m.model_dump(mode="json", exclude_none=True) for m in models]}, sort_keys=False, allow_unicode=True)
        # Same-directory atomic replacement; interrupted metadata resolution never truncates the manifest.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
            handle.write(payload)
            temp = handle.name
        try:
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    @app.get("/api/manifests")
    def manifests():
        models = load_models(model_path())
        return {"models": [{**m.model_dump(mode="json"), "resolved": m.resolved()} for m in models],
                "nodes": [n.model_dump(mode="json") for n in load_nodes(catalog.manifest_file("manifests/nodes.yaml"))]}

    @app.patch("/api/models/{name}")
    async def choose_model(name: str, payload: ModelChoice):
        async with manifest_lock:
            models = load_models(model_path())
            asset = next((m for m in models if m.name == name), None)
            if asset is None:
                raise KeyError(name)
            changes = payload.model_dump(exclude_none=True)
            if "filename" in changes and changes["filename"] != asset.filename:
                changes.update(revision=None, sha256=None, size_bytes=None)
            updated = type(asset).model_validate({**asset.model_dump(), **changes})
            models[models.index(asset)] = updated
            save_models(models)
            return updated

    @app.post("/api/models/{name}/resolve")
    async def resolve(name: str):
        async with manifest_lock:
            models = load_models(model_path())
            asset = next((m for m in models if m.name == name), None)
            if asset is None:
                raise KeyError(name)
            try:
                updated = await asyncio.to_thread(resolve_model, asset, service.setting("hf_token"))
            except ValueError:
                raise
            except Exception as exc:
                raise HTTPException(502, "Hugging Face metadata lookup failed. Check repo, exact filename and access token.") from exc
            models[models.index(asset)] = updated
            save_models(models)
            return updated

    if config.frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=config.frontend_dist, html=True), name="web")
    return app


def run():
    uvicorn.run("ghm.api:create_app", factory=True, host="127.0.0.1", port=8000)
