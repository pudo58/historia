"""Pinned ComfyUI transport. Persist prompt intent before POST to prevent duplicate renders."""
import asyncio
import json
import logging
import re
import shlex
import time
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid5

import httpx

from ghm.executors.http import comfy_client
from studio.media import probe
from studio.packs import graph_for

RIFE_OUTPUT_FPS = 48   # rife_post.json: 16 fps Wan clip x multiplier 3
VIDEO_WORKFLOWS = {'wan_i2v', 'rife_post', 'wan_s2v'}



def prompt_is_dead(prior, item, queued) -> bool:
    """A submitted prompt that ComfyUI finished as failed/interrupted (not running, not queued, not successful)."""
    if not prior or not item or queued or prior.get('state') not in {'submitting', 'submitted'}:
        return False
    status = item.get('status') or {}
    return status.get('status_str') == 'error' and not status.get('completed')

def comfy_failure_detail(status) -> str:
    """Node and exception ComfyUI itself recorded for a failed prompt (no paths, tokens or tracebacks)."""
    try:
        for entry in status.get("messages") or []:
            if isinstance(entry, list) and len(entry) == 2 and entry[0] == "execution_interrupted":
                return " ComfyUI ghi nhận prompt bị ngắt từ bên ngoài (có lệnh interrupt gửi tới cổng này), không phải do thiếu VRAM."
            if isinstance(entry, list) and len(entry) == 2 and entry[0] == "execution_error" and isinstance(entry[1], dict):
                data = entry[1]
                kind = re.sub(r"[^\w.]", "", str(data.get("exception_type", "")))[:80]
                node = re.sub(r"[^\w. -]", "", str(data.get("node_type", "")))[:80]
                text = re.sub(r"\s+", " ", re.sub(r"(/[\w.@+-]+){2,}", "<path>", str(data.get("exception_message", ""))))[:240]
                return f" Chi tiết từ ComfyUI: {kind or 'lỗi'} tại node {node or '?'}: {text}".rstrip(": ")
    except Exception:  # noqa: BLE001 -- diagnostics must never mask the original failure
        pass
    return ""

def worker_failure(result):
    """Allowlisted diagnostics only: never return tracebacks, signed URLs or tokens."""
    text = ((result.stdout or '') + '\n' + (result.stderr or '')).lower()
    if result.rc == 73:
        return 'Dịch vụ AI cũ còn chạy và giữ khóa; chờ hoặc đối chiếu trước khi thử lại.'
    if any(value in text for value in ('401', '403', 'gatedrepoerror', 'unauthorized', 'forbidden')):
        return 'Model/codec thiếu quyền truy cập. Chấp thuận license và cấu hình token đọc trong ứng dụng; không đổi model hoặc vượt quyền.'
    if 'không tự chuyển về cpu' in text or 'cuda không khả dụng' in text:
        return 'CUDA không khả dụng trong môi trường giọng đọc. Không tự chuyển về CPU.'
    if 'không xác nhận được thiết bị backbone' in text or 'không nằm trên cuda' in text:
        return 'Backbone giọng đọc không nằm trên CUDA. Không ghi kết quả giả.'
    if 'no kernel image' in text or 'cuda error: no kernel' in text:
        return 'Wheel PyTorch trong môi trường giọng đọc không tương thích GPU này. Không tự đổi driver hoặc môi trường ComfyUI.'
    if 'out of memory' in text or 'cuda out of memory' in text:
        return 'Thiếu VRAM cho giọng đọc. Không tự giảm chất lượng hoặc chuyển CPU.'
    if 'silent' in text or 'pcm audio' in text:
        return 'Audio rỗng, im lặng hoặc PCM không hợp lệ; chưa kiểm chứng TTS.'
    if 'preset' in text or 'voice' in text:
        return 'Không khởi tạo được giọng preset; cần kiểm tra VieNeu đúng phiên bản.'
    if any(value in text for value in ('modulenotfounderror', 'importerror', 'unexpected keyword')):
        return 'Dependency hoặc API constructor không tương thích bộ cài đã khóa.'
    return 'Dịch vụ AI chưa chạy được. Kiểm tra bộ cài/model; không có kết quả giả lập.'


class ReconcileRequired(RuntimeError):
    """Submission may exist remotely. Never automatically POST it a second time."""


class RemoteBackend:
    def __init__(self, hosts, service):
        self.hosts, self.service = hosts, service
        self._generation = ContextVar('comfy_generation_session', default=None)

    @asynccontextmanager
    async def generation_session(self, job):
        current = self._generation.get()
        if current and current['job_id'] == job.id and current['host_id'] == job.host_id:
            yield
            return
        async with AsyncExitStack() as stack:
            state = {'job_id': job.id, 'host_id': job.host_id, 'stack': stack,
                     'uploads': {}, 'shots': 0, 'remote_settled': False}
            token = self._generation.set(state)
            try:
                yield
            finally:
                try:
                    # Keep weights between shots, release at completion/review/pause
                    # or a confirmed remote error. Never write after ambiguous POST.
                    if state.get('client') and state['remote_settled']:
                        await self.release_idle_gpu(state['client'], state.get('log'))
                finally:
                    self._generation.reset(token)

    @asynccontextmanager
    async def generation_connection(self, job):
        state = self._generation.get()
        if state is None or state['job_id'] != job.id or state['host_id'] != job.host_id:
            async with self.generation_session(job), self.generation_connection(job) as connection:
                yield connection
            return
        if 'client' not in state:
            state['executor'], state['client'] = await state['stack'].enter_async_context(self.connection(job.host_id))
        yield state['client'], state

    async def process_generation(self, executor, host_id):
        """Read only a matching Comfy PID/start time; never expose process arguments."""
        if executor is None or self.hosts is None:  # In-memory test transports.
            return 'in-memory'
        options = self.hosts.options_for(host_id)
        script = r'''
import json, pathlib, sys
c = json.load(sys.stdin)
matches = []
for entry in pathlib.Path('/proc').iterdir():
    if not entry.name.isdigit(): continue
    try:
        argv = (entry/'cmdline').read_bytes().decode().split('\0')
        if '--port' in argv:
            if argv[argv.index('--port')+1] != str(c['port']): continue
        elif c['port'] != 8188: continue
        cwd = (entry/'cwd').resolve()
        expected = (pathlib.Path(c['comfy'])/'main.py').resolve()
        if not any(a.endswith('main.py') and (cwd/a).resolve() == expected for a in argv): continue
        start = (entry/'stat').read_text().rsplit(')',1)[1].split()[19]
        matches.append(entry.name + ':' + start)
    except (OSError, ValueError, IndexError, UnicodeError): pass
print(json.dumps(matches[0] if len(matches) == 1 else None))
'''
        result = await executor.run_input('python3 -c ' + shlex.quote(script),
            json.dumps({'comfy': options.comfy_root, 'port': options.remote_port}), timeout=20)
        if result.rc:
            return None
        try:
            return json.loads(result.stdout)
        except ValueError:
            return None

    @staticmethod
    async def runtime_metadata(client):
        response = await client.get('/system_stats')
        response.raise_for_status()
        stats = response.json()
        system = stats.get('system', {})
        argv = system.get('argv', [])
        return {'comfy_version': system.get('comfyui_version'), 'python_version': system.get('python_version'),
                'torch_version': system.get('pytorch_version'), 'cuda_version': system.get('cuda_version'),
                'attention_backend': 'sage' if '--use-sage-attention' in argv else 'default',
                'memory_policy': 'highvram' if '--highvram' in argv else 'default',
                'compute_flags': sorted(a for a in argv if a in {
                    '--use-sage-attention', '--use-pytorch-cross-attention', '--use-flash-attention',
                    '--use-split-cross-attention', '--use-quad-cross-attention', '--use-ck-attention',
                    '--disable-xformers', '--highvram', '--lowvram', '--novram', '--gpu-only', '--cpu',
                    '--disable-smart-memory', '--disable-dynamic-vram', '--disable-async-offload',
                    '--cache-none', '--cache-classic', '--force-fp32', '--force-fp16'}),
                'devices': [{k: d.get(k) for k in ('name', 'type', 'index', 'vram_total')}
                            for d in stats.get('devices', [])]}

    async def library_versions(self, executor, generation):
        if executor is None or not generation:
            return {}
        script = r'''
import json, pathlib, subprocess, sys
pid, expected_start = json.load(sys.stdin).split(':')
entry = pathlib.Path('/proc')/str(int(pid))
assert (entry/'stat').read_text().rsplit(')',1)[1].split()[19] == expected_start
argv = (entry/'cmdline').read_bytes().decode().split('\0')
python = pathlib.Path(argv[0])
assert python.is_absolute() and python.is_file()
code = "import importlib.metadata as m,json,torch; print(json.dumps({'torch_version':torch.__version__, 'cuda_version':torch.version.cuda, 'sageattention_version':next((d.version for d in m.distributions() if d.metadata.get('Name','').lower()=='sageattention'),None)}))"
result = subprocess.run([str(python), '-c', code], capture_output=True, text=True, timeout=30)
assert result.returncode == 0
data = json.loads(result.stdout)
print(json.dumps(data))
'''
        result = await executor.run_input('python3 -c ' + shlex.quote(script), json.dumps(generation), timeout=40)
        if result.rc:
            return {}
        try:
            return json.loads(result.stdout)
        except ValueError:
            return {}

    @asynccontextmanager
    async def connection(self, host_id: str):
        host = self.hosts._require_host(host_id)
        executor = self.hosts.executor_for(host)
        try:
            options = self.hosts.options_for(host_id)
            async with comfy_client(executor, options.remote_port) as client:
                yield executor, client
        finally:
            await executor.close()

    async def free_gpu(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/queue")
        response.raise_for_status()
        queue = response.json()
        if not isinstance(queue, dict) or any(not isinstance(queue.get(key), list)
                for key in ('queue_running', 'queue_pending')):
            raise ValueError('Chưa xác nhận queue ComfyUI đã rỗng.')
        if queue.get("queue_running") or queue.get("queue_pending"):
            raise ValueError("ComfyUI đang có job khác. Không giải phóng model hoặc chiếm GPU.")
        response = await client.post("/free", json={"unload_models": True, "free_memory": True})
        response.raise_for_status()

    async def release_idle_gpu(self, client, log=None):
        """Best-effort cleanup must not mask a render result or its original error.

        /free is asynchronous. Observe this process's allocator, rather than
        waiting for the whole GPU to become empty while another lane renders.
        """
        def report(message):
            try:
                if log:
                    log(message)
                else:
                    logging.getLogger(__name__).info(message)
            except Exception:  # noqa: BLE001 -- logging must not discard finished media
                logging.getLogger(__name__).warning('Could not record GPU cleanup log.')
        try:
            await self.free_gpu(client)
            report('VRAM: đã gửi yêu cầu dỡ model và giải phóng cache của tiến trình đã rảnh.')
            for _ in range(10):
                await asyncio.sleep(.5)
                response = await client.get('/system_stats')
                response.raise_for_status()
                devices = response.json().get('devices') or []
                reserved = devices[0].get('torch_vram_total') if devices else None
                if reserved is None:
                    return  # Older/custom servers have no allocator evidence.
                if reserved <= 256 * 1024**2:
                    report(f'VRAM: bộ nhớ PyTorch còn giữ của tiến trình là {reserved / 1024**2:.0f} MiB.')
                    return
            report('VRAM: đã yêu cầu dọn nhưng tiến trình vẫn giữ bộ nhớ; kiểm tra dịch vụ GPU nếu VRAM không giảm.')
        except ValueError:
            report('VRAM: chưa dọn vì queue ComfyUI đang có công việc hoặc phản hồi chưa hợp lệ.')
        except Exception:  # noqa: BLE001 -- preserve inference outcome, hide transport secrets
            report('VRAM: chưa xác nhận được việc dọn bộ nhớ do mất liên lạc với ComfyUI.')

    async def generate(self, job, name: str, prompt: str, images: list[Path],
                       seed: int, quality: str, log, checkpoint, stage: str, steps=None, shot=0) -> Path:
        started = time.monotonic()
        try:
            return await self._generate(job, name, prompt, images, seed, quality, log, checkpoint, stage, steps, shot)
        finally:
            # Accounting only: no request or retry is performed from this finally block.
            if self.service and hasattr(self.service, 'require'):
                from studio.models import Job
                current = self.service.require(Job, job.id).result.get('submissions', {}).get(stage)
                if current:
                    total = current.get('timing', {}).get('total_seconds', 0)
                    checkpoint(stage, {'timing': {'total_seconds': total + time.monotonic() - started}})

    async def _generate(self, job, name: str, prompt: str, images: list[Path],
                        seed: int, quality: str, log, checkpoint, stage: str, steps=None, shot=0) -> Path:
        from studio.comfy_schema import normalize_graph
        from studio.generation import shot_config
        from studio.media import digest
        from studio.metrics import execution_seconds
        from studio.service import canonical_hash
        target_dir = self.service.job_directory(job.id)
        is_video = name in VIDEO_WORKFLOWS
        prior = job.result.get("submissions", {}).get(stage)
        prompt_id = (prior or {}).get('prompt_id') or str(uuid5(UUID(job.id), stage))
        async with self.generation_connection(job) as (client, cache):
            cache['log'] = log
            names = []
            if prior:
                checkpoint(stage, {'attempts': prior.get('attempts', 1) + 1})
            timing = dict((prior or {}).get('timing', {}))
            duration = None
            if name == 'wan_i2v' and job.snapshot.get('generation_version', 2) >= 3:
                duration = probe(self.service.artifact_path(job.snapshot['scene']['speech_id']))['duration']
            config = shot_config(job, stage, duration) if name == 'wan_i2v' else None
            history = await client.get(f"/history/{prompt_id}")
            history.raise_for_status()
            item = history.json().get(prompt_id)
            queue = await client.get("/queue")
            queue.raise_for_status()
            queued = any(len(row) > 1 and row[1] == prompt_id for key in ["queue_running", "queue_pending"] for row in queue.json().get(key, []))
            if prior or queued:
                cache['remote_settled'] = False
            if prompt_is_dead(prior, item, queued):
                cache['remote_settled'] = True
                # ComfyUI still lists the earlier prompt as failed/interrupted. Re-reading it would fail
                # forever, and it is finished, so sending a fresh prompt cannot duplicate a render.
                prompt_id = str(uuid5(UUID(job.id), f"{stage}#{prior.get('attempts', 1)}"))
                log("Prompt trước đã dừng." + comfy_failure_detail(item.get('status', {}))[:240] + " Gửi prompt mới thay vì đọc lại kết quả cũ.")
                item, queued, prior = None, False, None
            if not item and not queued and prior and prior.get('state') in {'remote_completed', 'downloaded'} and prior.get('output'):
                item = {'status': {'status_str': 'success', 'completed': True},
                        'outputs': {'recovered': {'videos' if is_video else 'images': [prior['output']]}}}
            if not item and not queued and prior:
                raise ReconcileRequired("Không tìm thấy prompt đã gửi trong queue/history. Không tự gửi lại để tránh render trùng; kiểm tra máy GPU rồi tạo lượt mới.")
            if not item and not queued:
                if queue.json().get("queue_running") or queue.json().get("queue_pending"):
                    raise ValueError("GPU đang chạy công việc bên ngoài Studio. Chờ queue ComfyUI rỗng.")
                prepared = time.monotonic()
                runtime = await self.runtime_metadata(client)
                generation = await self.process_generation(cache.get('executor'), job.host_id)
                if generation is None or cache.get('process_generation') != generation or cache.get('runtime') != runtime:
                    cache['uploads'].clear()
                    cache.pop('schema', None)
                    cache['shots'] = 0
                cache['process_generation'] = generation
                cache['runtime'] = runtime
                if cache.get('versions_generation') != generation:
                    cache['library_versions'] = await self.library_versions(cache.get('executor'), generation)
                    cache['versions_generation'] = generation
                runtime = {**runtime, **cache.get('library_versions', {})}
                for index, path in enumerate(images):
                    from studio.formats import resolve_format
                    fmt = resolve_format(quality, job.snapshot.get("project", {}))
                    # The clip of a RIFE job and the voice of an S2V job go up unchanged (no image fitting).
                    as_is = name == 'rife_post' or (name == 'wan_s2v' and index == 1)
                    upload_key = (digest(path), 'as-is') if as_is else (digest(path), tuple(fmt['render_size']), fmt['legacy'])
                    if upload_key in cache['uploads']:
                        names.append(cache['uploads'][upload_key])
                        continue
                    if not fmt["legacy"] and not as_is:
                        from PIL import Image, ImageOps
                        fitted = target_dir / f"reference-fit-{index}.png"
                        with Image.open(path) as original:
                            ImageOps.pad(ImageOps.exif_transpose(original).convert("RGB"), fmt["render_size"],
                                         method=Image.Resampling.LANCZOS, color="black").save(fitted)
                        path = fitted
                    with path.open("rb") as handle:
                        response = await client.post("/upload/image", files={"image": (f"{job.id}-{index}{path.suffix}", handle)},
                                                     data={"overwrite": "false", "type": "input"})
                    response.raise_for_status()
                    data = response.json()
                    name_on_host = data["name"]
                    subfolder = data.get("subfolder", "")
                    if PurePosixPath(name_on_host).name != name_on_host or ".." in PurePosixPath(subfolder).parts:
                        raise ValueError("ComfyUI trả tên upload không an toàn.")
                    names.append(f"{subfolder}/{name_on_host}" if subfolder else name_on_host)
                    cache['uploads'][upload_key] = names[-1]
                extra = {}
                if name == 'wan_s2v':
                    from studio.packs import s2v_chunks
                    extra['chunks'] = s2v_chunks(probe(images[1])['duration'])
                graph = graph_for(name, prompt, seed, quality, names, f"studio/{job.id}/{stage}", config['steps'] if config else steps, shot,
                                  project_settings=job.snapshot.get("project", {}),
                                  frames=config['frames'] if config else 81,
                                  generation_version=job.snapshot.get('generation_version', 2), **extra)
                # LoadVideo/LoadAudio list what was just uploaded, so their choices must be read fresh.
                if 'schema' not in cache or name in {'rife_post', 'wan_s2v'}:
                    schema_response = await client.get("/object_info")
                    schema_response.raise_for_status()
                    cache['schema'] = schema_response.json()
                graph = normalize_graph(graph, cache['schema'])
                timing['prepare_upload_seconds'] = time.monotonic() - prepared
                # Record before the network write: on an ambiguous response we only reconcile.
                cache['remote_settled'] = False
                checkpoint(stage, {"prompt_id": prompt_id, "state": "submitting",
                                   'config': config, 'graph_hash': canonical_hash(graph), 'runtime': runtime,
                                   'cold_candidate': cache['shots'] == 0, 'timing': timing,
                                   'attempts': 1})
                cache['shots'] += 1
                try:
                    response = await client.post("/prompt", json={"prompt": graph, "prompt_id": prompt_id, "client_id": job.id})
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise ReconcileRequired("Kết nối gián đoạn khi gửi prompt. Cần đối chiếu trước khi thử lại.") from exc
                if response.json().get("prompt_id") != prompt_id or response.json().get("node_errors"):
                    raise ValueError("ComfyUI không chấp nhận workflow. Kiểm tra node/model của bộ cài.")
                checkpoint(stage, {"prompt_id": prompt_id, "state": "submitted"})
                log(f"Đã gửi {stage}; đang chờ GPU xử lý.")
            waiting = time.monotonic()
            deadline = time.monotonic() + 3600
            try:
                while not item:
                    if time.monotonic() > deadline:
                        raise ReconcileRequired("Prompt chưa hoàn tất sau 60 phút. Giữ ID để kiểm tra tiếp, không gửi lại.")
                    await asyncio.sleep(2)
                    response = await client.get(f"/history/{prompt_id}")
                    response.raise_for_status()
                    item = response.json().get(prompt_id)
            finally:
                timing['remote_wait_seconds'] = timing.get('remote_wait_seconds', 0) + time.monotonic() - waiting
                checkpoint(stage, {'timing': timing})
            status = item.get("status", {})
            cache['remote_settled'] = status.get('status_str') in {'success', 'error'}
            if status.get("status_str") != "success" or not status.get("completed"):
                raise ValueError("ComfyUI xử lý thất bại. Có thể thiếu VRAM/model; không tự giảm chất lượng." +
                                 comfy_failure_detail(status))
            seconds = execution_seconds(item, prompt_id)
            if seconds is not None:
                timing['comfy_execution_seconds'] = seconds
            outputs = []
            for output in item.get("outputs", {}).values():
                for key in ["images", "gifs", "videos"]:
                    outputs.extend(output.get(key, []))
            suffixes = {".mp4", ".webm"} if is_video else {".png", ".jpg", ".webp"}
            # Only files a Save node wrote. Some nodes (LoadVideo) also list the *input* they read as a
            # preview; downloading that would hand back the untouched source as if it were the result.
            output = next((o for o in outputs if o.get("type", "output") == "output"
                           and Path(o.get("filename", "")).suffix.lower() in suffixes), None)
            if not output:
                raise ValueError("Workflow không tạo output đúng loại; không đánh dấu thành công.")
            filename = output["filename"]
            if PurePosixPath(filename).name != filename or ".." in PurePosixPath(output.get("subfolder", "")).parts:
                raise ValueError("Tên artifact trả về không an toàn.")
            target = target_dir / (stage + Path(filename).suffix)
            checkpoint(stage, {'prompt_id': prompt_id, 'state': 'remote_completed',
                               'output': {k: output[k] for k in ('filename', 'subfolder', 'type') if k in output},
                               'timing': timing})
            part = target.with_suffix(target.suffix + ".part")
            download_started = time.monotonic()
            try:
                async with client.stream("GET", "/view", params={k: output[k] for k in ["filename", "subfolder", "type"] if k in output}) as response:
                    response.raise_for_status()
                    count = 0
                    with part.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            count += len(chunk)
                            if count > 2_000_000_000:
                                raise ValueError("Output vượt giới hạn 2 GB cho một clip.")
                            handle.write(chunk)
            finally:
                timing['download_seconds'] = timing.get('download_seconds', 0) + time.monotonic() - download_started
                checkpoint(stage, {'timing': timing})
            if not count:
                raise ValueError("Output tải về rỗng.")
            part.replace(target)
            validation_started = time.monotonic()
            if is_video:
                video_info = probe(target)
                if video_info["duration"] <= 0 or ('width' in video_info and not video_info['width']):
                    raise ValueError("Output không phải video đọc được.")
                if config and name == 'wan_i2v' and ('video_duration' in video_info or 'frames' in video_info):
                    video_seconds = video_info.get('video_duration') or video_info['duration']
                    if video_seconds + 1 / config['fps'] + .02 < config['shot_seconds']:
                        raise ValueError('Clip Wan thiếu frame so với cấu hình shot; giữ prompt ID để kiểm tra output.')
                    measured_fps = video_info.get('fps')
                    if measured_fps and abs(measured_fps - config['fps']) > .5:
                        raise ValueError('FPS clip Wan không khớp cấu hình shot; không lưu artifact sai.')
                if name == 'wan_s2v':
                    from studio.packs import S2V_FPS
                    if video_info.get('fps') and abs(video_info['fps'] - S2V_FPS) > .5:
                        raise ValueError('FPS clip S2V không khớp cấu hình; không lưu artifact sai.')
                    wanted = probe(images[1])['duration']
                    if (video_info.get('video_duration') or video_info['duration']) + .1 < wanted:
                        raise ValueError('Clip S2V ngắn hơn giọng nói; không ghép để tránh lệch tiếng.')
                if name == 'rife_post' and video_info.get('fps') and abs(video_info['fps'] - RIFE_OUTPUT_FPS) > .5:
                    raise ValueError('Clip RIFE không phải 48 fps: nội suy chưa được áp dụng; không lưu clip gốc như kết quả nội suy.')
            else:
                from PIL import Image
                with Image.open(target) as image:
                    image.verify()
            timing['validation_seconds'] = time.monotonic() - validation_started
            checkpoint(stage, {"prompt_id": prompt_id, "state": "downloaded", 'timing': timing})
            return target

    async def cancel(self, job) -> None:
        async with self.connection(job.host_id) as (_, client):
            ids = {value["prompt_id"] for value in job.result.get("submissions", {}).values()}
            response = await client.get("/queue")
            response.raise_for_status()
            state = response.json()
            running = {row[1] for row in state.get("queue_running", []) if len(row) > 1}
            response = await client.post("/queue", json={"delete": sorted(ids)})
            response.raise_for_status()
            for prompt_id in running & ids:
                response = await client.post("/interrupt", json={"prompt_id": prompt_id})
                response.raise_for_status()
            for _ in range(15):
                response = await client.get("/queue")
                response.raise_for_status()
                state = response.json()
                present = {row[1] for key in ("queue_running", "queue_pending") for row in state.get(key, []) if len(row) > 1}
                if not (present & ids):
                    await self.release_idle_gpu(client)
                    return
                await asyncio.sleep(2)
            raise ReconcileRequired("Chưa xác nhận prompt đã dừng; giữ GPU ở trạng thái cần đối chiếu.")

    async def language(self, job, prompt: str, images: list[Path], log) -> dict:
        provider = job.snapshot.get('script_provider')
        if provider and provider.get('kind') == 'api':
            from studio.script_provider import generate
            log('Đang gửi yêu cầu tới API kịch bản đã cấu hình.')
            return await generate(self.hosts, provider, prompt)
        return await self._worker(job, "language", {"prompt": prompt}, images, log)

    async def recover_speech(self, job) -> Path | None:
        """Return a finished remote WAV, None if the attempt is gone, or keep reconciling while it holds the lock."""
        options = self.hosts.options_for(job.host_id)
        remote_dir = f"{options.root}/studio-jobs/{job.id}"
        remote = remote_dir + "/speech.wav"
        lock = options.root + "/.studio-ai.lock"
        async with self.connection(job.host_id) as (executor, _client):
            held = await executor.run("flock -n -E 73 " + shlex.quote(lock) + " true", timeout=20)
            if held.rc == 73:
                raise ReconcileRequired("Tiến trình giọng đọc trên GPU vẫn đang chạy. Chờ thêm rồi bấm Đối chiếu; không tạo lượt mới.")
            if held.rc:
                raise ReconcileRequired("Chưa kiểm tra được khóa giọng đọc trên GPU. Bấm Đối chiếu khi SSH hoạt động lại.")
            exists = await executor.run("test -s " + shlex.quote(remote), timeout=15)
            if exists.rc:
                return None
            target = self.service.job_directory(job.id) / "speech.wav"
            target.parent.mkdir(parents=True, exist_ok=True)
            await executor.download(remote, str(target))
            segments = await executor.run("test -s " + shlex.quote(remote_dir + "/speech.segments.json"), timeout=15)
            if not segments.rc:
                await executor.download(remote_dir + "/speech.segments.json", str(target.with_name("speech.segments.json")))
        if probe(target)["duration"] <= 0:
            raise ValueError("File giọng đọc trên GPU không hợp lệ. Không ghi kết quả giả.")
        return target

    async def speech(self, job, text: str, voice: str, log) -> Path:
        from studio.tts_device import snapshot_tts_device
        device = snapshot_tts_device(job.snapshot.get("project"))
        log("Giọng đọc backbone " + ("GPU" if device == "cuda" else "CPU") + ", codec ONNX trên CPU. Không fallback im lặng.")
        result = await self._worker(job, "speech", {"text": text, "voice": voice, "tts_device": device}, [], log)
        return Path(result["local_audio"])

    async def audition(self, job, text: str, log) -> list[dict]:
        """Synthesize ``text`` in every preset voice on the GPU; returns [{voice, label, path, duration}]."""
        from studio.tts_device import snapshot_tts_device
        device = snapshot_tts_device(job.snapshot.get("project"))
        log("Nghe thử giọng: backbone " + ("GPU" if device == "cuda" else "CPU") + ", đọc cùng một câu bằng mọi giọng có sẵn.")
        result = await self._worker(job, "speech", {"text": text, "audition": True, "tts_device": device}, [], log)
        return result["audition"]

    async def _worker(self, job, mode: str, payload: dict, images: list[Path], log) -> dict:
        options = self.hosts.options_for(job.host_id)
        async with self.connection(job.host_id) as (executor, client):
            await self.free_gpu(client)
            remote_dir = f"{options.root}/studio-jobs/{job.id}"
            result = await executor.run("mkdir -p " + shlex.quote(remote_dir), timeout=15)
            if result.rc:
                raise ValueError("Không tạo được thư mục công việc trên GPU.")
            remote_images = []
            for index, path in enumerate(images):
                destination = f"{remote_dir}/ref-{index}{path.suffix}"
                await executor.upload(str(path), destination)
                remote_images.append(destination)
            config = {**payload, "mode": mode, "images": remote_images, "root": options.root,
                      "output": f"{remote_dir}/speech.wav"}
            worker = Path(__file__).parent / "remote_worker.py"
            await executor.upload(str(worker), remote_dir + "/worker.py")
            environment = "llm" if mode == "language" else "tts"
            command = shlex.quote(f"{options.root}/{environment}-venv/bin/python") + " " + shlex.quote(remote_dir + "/worker.py")
            # Inherited remote lock prevents an interrupted LLM/TTS child overlapping its retry.
            command = 'flock -n -E 73 ' + shlex.quote(options.root + '/.studio-ai.lock') + ' sh -c ' + shlex.quote(command)
            log("Đang chạy " + ("mô hình ngôn ngữ/thị giác" if mode == "language" else "giọng đọc tiếng Việt") + " trong môi trường riêng.")
            result = await executor.run_input(command, json.dumps(config, ensure_ascii=False), timeout=1800)
            if result.rc:
                raise ValueError(worker_failure(result))
            marker = next((line.removeprefix("STUDIO_RESULT=") for line in reversed(result.stdout.splitlines()) if line.startswith("STUDIO_RESULT=")), None)
            if not marker:
                raise ValueError("Dịch vụ AI không trả kết quả có cấu trúc.")
            output = json.loads(marker)
            if mode == "speech" and payload.get("audition"):
                from studio.remote_worker import inspect_wav
                folder = self.service.job_directory(job.id)
                folder.mkdir(parents=True, exist_ok=True)
                for entry in output["audition"]:
                    local = folder / Path(entry["file"]).name
                    await executor.download(entry["file"], str(local))
                    if inspect_wav(local)["sha256"] != entry["sha256"]:
                        raise ValueError("Audio giọng thử tải về không khớp checksum đã đo từ worker.")
                    entry["path"] = str(local)
                return output
            if mode == "speech":
                target = self.service.job_directory(job.id) / "speech.wav"
                await executor.download(config["output"], str(target))
                from studio.remote_worker import inspect_wav
                measured = inspect_wav(target)
                if measured['sha256'] != output.get('audio', {}).get('sha256'):
                    raise ValueError('Audio tải về không khớp checksum đã đo từ worker.')
                segments = output.get('segments', [])
                cursor = 0.0
                for segment in segments:
                    if abs(segment['start'] - cursor) > 1 / measured['sample_rate'] or segment['end'] <= segment['start']:
                        raise ValueError('Timestamp đoạn TTS không liên tục.')
                    cursor = segment['end']
                if not segments or abs(cursor - measured['duration']) > 1 / measured['sample_rate']:
                    raise ValueError('Timestamp TTS không khớp audio thực.')
                output['audio'] = measured
                output["local_audio"] = str(target)
                # Additive sidecar contract: speech() remains Path-compatible for existing callers.
                target.with_suffix('.segments.json').write_text(json.dumps(output, ensure_ascii=False), encoding='utf-8')
            return output
