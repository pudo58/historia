import asyncio
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from studio.importer import EXTENSIONS, import_file
from studio.models import Artifact, Installation, Job, JobEvent, Project
from studio.packs import pack_info
from studio.thumbnail import video_thumbnail
from studio.schemas import (
    Approval,
    BenchmarkInput,
    ChainFrameApproval,
    CharacterInput,
    DialogueTestInput,
    KnowledgePackInput,
    VoiceAuditionInput,
    InstallConsent,
    JobInput,
    KeyframeBatchApproval,
    OutlineApproval,
    PendingClipConfig,
    ProductionInput,
    ProductionRunInput,
    ProjectInput,
    RuntimeConfig,
    SceneInput,
    SceneUpdate,
    ScriptProviderInput,
    SourceUpdate,
    TextSourceInput,
)


class AbandonInput(BaseModel):
    # Deliberately untyped values: jobs.abandon() accepts only the JSON literal true (409 otherwise).
    confirmed: object = False
    remote_state_unknown: object = False


def router(service, jobs, host_lock):
    api = APIRouter(prefix="/api/studio")
    from studio.event_log import router as journal_router
    # Journal router has its own prefix; include below without double-prefixing.

    @api.get("/status")
    def status():
        with service.sessions() as session:
            ready = bool(session.scalar(select(Installation.host_id).where(Installation.status == 'verified')))
        return {"name": "Historical Video Studio", "version": "0.4.0", "ai_ready": ready,
                "message": "Cài bộ AI trong Bộ AI & kiểm chứng (SSH gốc hoặc Runpod Basic SSH). Chỉ mở tác vụ AI sau khi output kiểm chứng pass.",
                "billing_notice": "Dừng render hoặc đóng web KHÔNG dừng tính tiền GPU thuê."}

    @api.get('/quality-policy')
    def quality_policy():
        from studio.storyboard import QUALITY_STATUS, BENCHMARK_CASES
        return {'profiles': QUALITY_STATUS, 'benchmark_cases': BENCHMARK_CASES,
                'gpu_acceptance': 'pending_visual_review', 'automatic_execution': False}

    @api.get('/script-provider')
    def script_provider():
        import json
        saved = json.loads(jobs.hosts.setting('studio_script_provider') or '{}')
        return {'configured': bool(saved), 'url': saved.get('url', ''),
                'model': saved.get('model', ''), 'has_key': bool(saved.get('api_key'))}

    @api.put('/script-provider')
    def save_script_provider(payload: ScriptProviderInput):
        import json
        if payload.url:
            jobs.hosts.save_setting('studio_script_provider', json.dumps(payload.model_dump()))
        else:
            jobs.hosts.save_setting('studio_script_provider', '')
        return script_provider()

    from ghm.schemas import HostOptions

    @api.get('/hosts/{id}/installation')
    def installation(id: str):
        return jobs.installations.state(id)

    @api.get('/hosts/{id}/optional-models')
    def optional_models(id: str):
        from studio.packs import optional_names
        return {name: jobs.optional_ready(id, name) for name in optional_names()}

    @api.get('/projects/{id}/pod-idle')
    async def pod_idle(id: str):
        from datetime import UTC, datetime

        from studio.models import ProductionRun
        project = service.project(id)
        host_id = project.get('host_id')
        if not host_id:
            return {'state': 'unknown', 'reason': 'Chưa chọn Pod.'}
        with service.sessions() as session:
            from ghm.models import Host
            me = session.get(Host, host_id)
            # GPU lanes of one Pod share the rental: the Pod is idle only when every lane is.
            siblings = {host_id} | ({h.id for h in session.scalars(select(Host).where(
                Host.address == me.address, Host.port == me.port, Host.username == me.username))} if me else set())
            rows = list(session.scalars(select(Job).where(Job.host_id.in_(siblings))))
            runs = [run for run in session.scalars(select(ProductionRun))
                    if run.snapshot.get('host_id') in siblings or
                    siblings & set((run.consent or {}).get('parallel_host_ids') or [])]
        def unresolved(result):
            return bool(result.get('maintenance_pending') or result.get('speech_pending') or
                result.get('outline_pending') or result.get('chapter_pending') is not None or
                any(v.get('state') != 'downloaded' or not v.get('artifact_id')
                    for v in result.get('submissions', {}).values()))
        active = any(j.status in {'queued', 'running', 'cancelling', 'reconciling'} or
                     (j.status in {'paused', 'failed', 'interrupted', 'abandoned'} and unresolved(j.result))
                     or (j.kind in {'install', 'optional_model', 'verify'} and j.status in {'interrupted', 'abandoned'})
                     for j in rows)
        active |= any(r.status in {'running', 'pause_requested', 'reconciling'} for r in runs)
        if active:
            return {'state': 'busy', 'reason': 'Historia còn job hoặc prompt cần đối chiếu.'}
        try:
            async with jobs.backend.connection(host_id) as (_, client):
                response = await client.get('/queue', timeout=15)
                response.raise_for_status()
                queue = response.json()
        except Exception:  # noqa: BLE001 -- read-only health check, never infer idleness
            return {'state': 'unknown', 'reason': 'Không đọc được queue ComfyUI; trạng thái Pod chưa xác minh.'}
        if queue.get('queue_running') or queue.get('queue_pending'):
            return {'state': 'busy', 'reason': 'Queue ComfyUI chưa rỗng.'}
        timestamps = [datetime.fromisoformat(value) for value in
                      [*(j.updated_at for j in rows), *(r.updated_at for r in runs)] if value]
        if not timestamps:
            return {'state': 'unknown', 'reason': 'Chưa có mốc hoạt động để xác nhận 10 phút nhàn rỗi.'}
        seconds = max(0, (datetime.now(UTC) - max(timestamps)).total_seconds())
        hourly = project.get('hourly_usd')
        return {'state': 'idle' if seconds >= 600 else 'recent', 'idle_seconds': seconds, 'host_id': host_id,
                'hourly_usd': hourly, 'next_ten_minutes_usd': hourly / 6 if hourly is not None else None,
                'reason': 'ComfyUI rỗng và Historia không còn job/prompt cần đối chiếu.'}

    @api.post('/hosts/{id}/optional-models/{name}')
    def install_optional_model(id: str, name: str):
        return jobs.install_optional(id, name)

    @api.get('/hosts/{id}/runtime')
    def runtime_state(id: str):
        return jobs.runtime.state(id)

    @api.post('/hosts/{id}/runtime/inspect')
    async def inspect_runtime(id: str):
        async with host_lock(id):
            return await jobs.runtime.inspect(id)

    @api.post('/hosts/{id}/runtime/apply')
    async def apply_runtime(id: str, payload: RuntimeConfig):
        async with host_lock(id):
            return jobs.runtime.start(id, payload.model_dump())

    @api.post('/hosts/{id}/runtime/install-sage')
    async def install_sage(id: str, payload: RuntimeConfig):
        async with host_lock(id):
            return jobs.runtime.start(id, payload.model_dump(), 'install-sage')

    @api.post('/hosts/{id}/runtime/recover')
    async def recover_runtime(id: str, payload: RuntimeConfig):
        async with host_lock(id):
            return jobs.runtime.start(id, payload.model_dump(), 'recover')

    @api.post('/hosts/{id}/installation/discover')
    async def discover_installation(id: str):
        async with host_lock(id):
            return await jobs.installations.discover(id)

    @api.post('/hosts/{id}/installation/prepare')
    async def prepare_installation(id: str, payload: HostOptions):
        if payload.workflow is not None:
            raise HTTPException(422, 'Bộ cài chỉ dùng workflow đóng gói; không nhận graph tùy ý.')
        async with host_lock(id):
            return await jobs.installations.prepare(id, payload)

    @api.post('/hosts/{id}/installation/start')
    async def install(id: str, payload: InstallConsent):
        async with host_lock(id):
            return jobs.installations.start(id, payload.plan_id, payload.license_accepted)

    @api.post('/hosts/{id}/installation/video-test')
    async def video_test(id: str):
        async with host_lock(id):
            return jobs.installations.video_test(id)

    @api.post('/hosts/{id}/installation/verify')
    async def verify_installation(id: str):
        async with host_lock(id):
            return jobs.installations.verify(id)

    @api.get("/packs")
    def packs():
        with service.sessions() as session:
            installations = [service.read(i) for i in session.scalars(select(Installation))]
        return {"packs": [pack_info()], "installations": installations}

    @api.get("/projects")
    def projects():
        return service.projects()

    @api.post("/projects", status_code=201)
    def create(payload: ProjectInput):
        if payload.host_id:
            jobs.hosts._require_host(payload.host_id)
        return service.create_project(payload)

    @api.delete("/projects/{id}", status_code=204)
    def delete_project(id: str):
        service.delete_project(id)

    @api.get("/projects/{id}")
    def get_project(id: str):
        return service.project(id)

    @api.patch("/projects/{id}")
    def update_project(id: str, payload: ProjectInput):
        if payload.host_id:
            jobs.hosts._require_host(payload.host_id)
        return service.update_project(id, payload)

    @api.post("/projects/{id}/sources", status_code=201)
    def add_text(id: str, payload: TextSourceInput):
        return service.add_text(id, payload)

    @api.patch("/sources/{id}")
    def update_source(id: str, payload: SourceUpdate):
        return service.update_source(id, payload.selected, payload.description)

    @api.post("/projects/{id}/upload", status_code=201)
    async def upload(id: str, request: Request, name: str = Query(min_length=1, max_length=200), role: str = "historical"):
        service.require(Project, id)
        service.assert_idle(id)
        if role not in {"historical", "visual"}:
            raise HTTPException(422, "Chọn nguồn lịch sử hoặc tham khảo mỹ thuật.")
        suffix = Path(name).suffix.lower()
        if suffix not in EXTENSIONS:
            raise HTTPException(415, "Chỉ nhận TXT, Markdown, PDF, ảnh, video hoặc nhạc.")
        directory = service.root / "uploads" / str(uuid4())
        directory.mkdir(parents=True)
        path = directory / ("original" + suffix)
        count = 0
        try:
            with path.open("wb") as handle:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > 250_000_000:
                        raise HTTPException(413, "Tệp vượt giới hạn 250 MB.")
                    handle.write(chunk)
            if not count:
                raise HTTPException(422, "Tệp rỗng.")
            if suffix in {".txt", ".md", ".pdf"} and count > 25_000_000:
                raise HTTPException(413, "Tài liệu vượt giới hạn 25 MB.")
            return await asyncio.to_thread(import_file, service, id, path, Path(name).name, role)
        except HTTPException:
            path.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 -- untrusted file parsers must not leak internals
            path.unlink(missing_ok=True)
            raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else "Không đọc được tệp. Kiểm tra định dạng hoặc tệp bị hỏng.") from None

    @api.post("/projects/{id}/characters", status_code=201)
    def add_character(id: str, payload: CharacterInput):
        return service.add_character(id, payload)

    @api.post("/projects/{id}/scenes", status_code=201)
    def add_scene(id: str, payload: SceneInput):
        return service.add_scene(id, payload)

    @api.patch("/scenes/{id}")
    def update_scene(id: str, payload: SceneUpdate):
        return service.update_scene(id, payload)

    @api.post("/scenes/{id}/approve")
    def approve(id: str, payload: Approval):
        return service.approve(id, payload.revision, payload.target, payload.approved)

    @api.post("/projects/{id}/outline/approve")
    def approve_outline(id: str, payload: OutlineApproval):
        service.assert_idle(id)
        with service.sessions() as session:
            row = session.get(Project, id)
            if not row:
                raise KeyError(id)
            row.data = {**row.data, "outline": payload.model_dump()["outline"], "outline_approved": True}
            session.commit()
        return service.project(id)

    @api.get('/projects/{id}/production')
    def production(id: str):
        return jobs.production(id)

    @api.post('/projects/{id}/production', status_code=201)
    def start_production(id: str, payload: ProductionInput):
        # Plain def: runs in the threadpool, so hashing artifacts never blocks the event loop.
        return jobs.start_production(id, payload.stage)

    @api.get('/projects/{id}/production-runs')
    def production_runs(id: str):
        return jobs.runs.list(id)

    @api.get('/projects/{id}/performance')
    def project_performance(id: str, run_id: str | None = None):
        from studio.performance import performance
        return performance(jobs, id, run_id)

    @api.post('/projects/{id}/benchmarks', status_code=201)
    def start_benchmark(id: str, payload: BenchmarkInput):
        from studio.benchmark import start
        return start(jobs, id, payload)

    @api.get('/benchmarks/compare')
    def compare_benchmarks(baseline_id: str, candidate_id: str):
        from studio.benchmark import compare
        return compare(jobs, baseline_id, candidate_id)

    @api.post('/projects/{id}/production-runs', status_code=201)
    def create_production_run(id: str, payload: ProductionRunInput):
        return jobs.runs.create(id, payload)

    @api.get('/production-runs/{id}')
    def production_run(id: str):
        return jobs.runs.get(id)

    @api.post('/production-runs/{id}/pause')
    def pause_production_run(id: str):
        return jobs.runs.action(id, 'pause')

    @api.post('/production-runs/{id}/resume')
    def resume_production_run(id: str):
        return jobs.runs.action(id, 'resume')

    @api.post('/production-runs/{id}/accept-duration')
    def accept_production_duration(id: str):
        return jobs.runs.action(id, 'accept-duration')

    @api.post('/production-runs/{id}/approve-keyframes')
    def approve_run_keyframes(id: str, payload: KeyframeBatchApproval):
        return jobs.runs.approve_keyframes(id, payload.ids())

    @api.post('/production-runs/{id}/pending-clip-config')
    def configure_run_clips(id: str, payload: PendingClipConfig):
        return jobs.configure_pending(payload.model_dump(exclude_unset=True), run_id=id)

    @api.post('/jobs/{id}/pending-clip-config')
    def configure_job_clips(id: str, payload: PendingClipConfig):
        return jobs.configure_pending(payload.model_dump(exclude_unset=True), job_id=id)

    @api.get("/jobs")
    def list_jobs(project_id: str | None = None):
        return jobs.list(project_id, summary=True)

    @api.post("/projects/{id}/jobs", status_code=201)
    def submit(id: str, payload: JobInput):
        return jobs.submit(id, payload)

    @api.post('/projects/{id}/dialogue-test', status_code=201)
    def dialogue_test(id: str, payload: DialogueTestInput):
        return jobs.dialogue_test(id, payload.scene_id, payload.text, payload.voice)

    @api.post('/projects/{id}/voice-audition', status_code=201)
    def voice_audition(id: str, payload: VoiceAuditionInput):
        return jobs.voice_audition(id, payload.text)

    @api.get('/knowledge')
    def knowledge_list():
        from studio import knowledge
        return knowledge.listing(service.root)

    @api.get('/knowledge/{pack_id}')
    def knowledge_get(pack_id: str):
        from studio import knowledge
        pack = knowledge.load(service.root, pack_id)
        if not pack:
            raise KeyError(pack_id)
        return pack

    @api.put('/knowledge/{pack_id}')
    def knowledge_save(pack_id: str, payload: KnowledgePackInput):
        from studio import knowledge
        if payload.id != pack_id:
            raise ValueError('Mã gói không khớp đường dẫn.')
        return knowledge.save(service.root, payload.model_dump())

    @api.get("/jobs/{id}/events")
    def events(id: str, after: int = Query(default=0, ge=0), tail: bool = False):
        service.require(Job, id)
        with service.sessions() as session:
            rows = [service.read(e) for e in session.scalars(select(JobEvent).where(JobEvent.job_id == id, JobEvent.id > after).order_by(JobEvent.id.desc() if tail else JobEvent.id).limit(500))]
            return list(reversed(rows)) if tail else rows

    @api.post("/jobs/{id}/cancel")
    async def cancel(id: str):
        return await jobs.cancel(id)

    @api.post('/jobs/{id}/pause')
    async def pause(id: str):
        return jobs.pause(id)

    @api.post('/jobs/{id}/abandon')
    def abandon(id: str, payload: AbandonInput):
        return jobs.abandon(id, confirmed=payload.confirmed, remote_state_unknown=payload.remote_state_unknown)

    @api.post("/jobs/{id}/resume")
    async def resume(id: str):
        return jobs.resume(id)

    @api.post('/jobs/{id}/approve-chain-frame')
    def approve_chain_frame(id: str, payload: ChainFrameApproval):
        return jobs.approve_chain_frame(id, payload.index, payload.artifact_id)

    @api.get("/projects/{id}/artifacts")
    def artifacts(id: str):
        service.require(Project, id)
        with service.sessions() as session:
            return [service.read(a) for a in session.scalars(select(Artifact).where(Artifact.project_id == id))]

    @api.get("/artifacts/{id}/thumbnail")
    def artifact_thumbnail(id: str):
        row = service.require(Artifact, id)
        if not row.media_type.startswith("video/"):
            raise HTTPException(415, "Thumbnail chỉ hỗ trợ video.")
        # Verify the original on every request, including cache hits: a changed
        # source must never be hidden by a previously generated thumbnail.
        path = service.artifact_path(id)
        preview = video_thumbnail(path, service.root, row.sha256)
        return FileResponse(preview, media_type="image/jpeg", filename="thumbnail.jpg",
                            content_disposition_type="inline",
                            headers={"Content-Security-Policy": "default-src 'none'; sandbox",
                                     "X-Content-Type-Options": "nosniff",
                                     "Cache-Control": "private, max-age=3600"})

    @api.get("/artifacts/{id}/file")
    def artifact(id: str, download: bool = False):
        row = service.require(Artifact, id)
        path = service.artifact_path(id)
        # Never inline user-supplied HTML, SVG, scripts, or text.
        inline = row.media_type.startswith(("image/", "video/", "audio/")) and row.media_type != "image/svg+xml"
        return FileResponse(path, media_type=row.media_type, filename=row.name,
                            content_disposition_type="inline" if inline and not download else "attachment",
                            headers={"Content-Security-Policy": "default-src 'none'; sandbox"})

    root = APIRouter()
    root.include_router(api)
    root.include_router(journal_router(service))
    return root
