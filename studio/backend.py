"""Pinned ComfyUI transport. Persist prompt intent before POST to prevent duplicate renders."""
import asyncio
import json
import shlex
import time
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid5

import httpx

from ghm.executors.http import comfy_client
from studio.media import probe
from studio.packs import graph_for


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
        queue = (await client.get("/queue")).json()
        if queue.get("queue_running") or queue.get("queue_pending"):
            raise ValueError("ComfyUI đang có job khác. Không giải phóng model hoặc chiếm GPU.")
        response = await client.post("/free", json={"unload_models": True, "free_memory": True})
        response.raise_for_status()

    async def generate(self, job, name: str, prompt: str, images: list[Path],
                       seed: int, quality: str, log, checkpoint, stage: str, steps=None, shot=0) -> Path:
        target_dir = self.service.job_directory(job.id)
        prompt_id = str(uuid5(UUID(job.id), stage))
        async with self.connection(job.host_id) as (_, client):
            names = []
            prior = job.result.get("submissions", {}).get(stage)
            history = await client.get(f"/history/{prompt_id}")
            history.raise_for_status()
            item = history.json().get(prompt_id)
            queue = await client.get("/queue")
            queue.raise_for_status()
            queued = any(len(row) > 1 and row[1] == prompt_id for key in ["queue_running", "queue_pending"] for row in queue.json().get(key, []))
            if not item and not queued and prior:
                raise ReconcileRequired("Không tìm thấy prompt đã gửi trong queue/history. Không tự gửi lại để tránh render trùng; kiểm tra máy GPU rồi tạo lượt mới.")
            if not item and not queued:
                if queue.json().get("queue_running") or queue.json().get("queue_pending"):
                    raise ValueError("GPU đang chạy công việc bên ngoài Studio. Chờ queue ComfyUI rỗng.")
                for index, path in enumerate(images):
                    from studio.formats import resolve_format
                    fmt = resolve_format(quality, job.snapshot.get("project", {}))
                    if not fmt["legacy"]:
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
                graph = graph_for(name, prompt, seed, quality, names, f"studio/{job.id}/{stage}", steps, shot,
                                  project_settings=job.snapshot.get("project", {}))
                schema_response = await client.get("/object_info")
                schema_response.raise_for_status()
                schema = schema_response.json()
                missing = sorted({n["class_type"] for n in graph.values()} - set(schema))
                if missing:
                    raise ValueError("Thiếu node từ workflow pack: " + ", ".join(missing))
                # Record before the network write: on an ambiguous response we only reconcile.
                checkpoint(stage, {"prompt_id": prompt_id, "state": "submitting"})
                try:
                    response = await client.post("/prompt", json={"prompt": graph, "prompt_id": prompt_id, "client_id": job.id})
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise ReconcileRequired("Kết nối gián đoạn khi gửi prompt. Cần đối chiếu trước khi thử lại.") from exc
                if response.json().get("prompt_id") != prompt_id or response.json().get("node_errors"):
                    raise ValueError("ComfyUI không chấp nhận workflow. Kiểm tra node/model của bộ cài.")
                checkpoint(stage, {"prompt_id": prompt_id, "state": "submitted"})
                log(f"Đã gửi {stage}; đang chờ GPU xử lý.")
            deadline = time.monotonic() + 3600
            while not item:
                if time.monotonic() > deadline:
                    raise ReconcileRequired("Prompt chưa hoàn tất sau 60 phút. Giữ ID để kiểm tra tiếp, không gửi lại.")
                await asyncio.sleep(2)
                response = await client.get(f"/history/{prompt_id}")
                response.raise_for_status()
                item = response.json().get(prompt_id)
            status = item.get("status", {})
            if status.get("status_str") != "success" or not status.get("completed"):
                raise ValueError("ComfyUI xử lý thất bại. Có thể thiếu VRAM/model; không tự giảm chất lượng.")
            outputs = []
            for output in item.get("outputs", {}).values():
                for key in ["images", "gifs", "videos"]:
                    outputs.extend(output.get(key, []))
            suffixes = {".mp4", ".webm"} if name == "wan_i2v" else {".png", ".jpg", ".webp"}
            output = next((o for o in outputs if Path(o.get("filename", "")).suffix.lower() in suffixes), None)
            if not output:
                raise ValueError("Workflow không tạo output đúng loại; không đánh dấu thành công.")
            filename = output["filename"]
            if PurePosixPath(filename).name != filename or ".." in PurePosixPath(output.get("subfolder", "")).parts:
                raise ValueError("Tên artifact trả về không an toàn.")
            target = target_dir / (stage + Path(filename).suffix)
            part = target.with_suffix(target.suffix + ".part")
            async with client.stream("GET", "/view", params={k: output[k] for k in ["filename", "subfolder", "type"] if k in output}) as response:
                response.raise_for_status()
                count = 0
                with part.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        count += len(chunk)
                        if count > 2_000_000_000:
                            raise ValueError("Output vượt giới hạn 2 GB cho một clip.")
                        handle.write(chunk)
            if not count:
                raise ValueError("Output tải về rỗng.")
            part.replace(target)
            if name == "wan_i2v":
                if probe(target)["duration"] <= 0:
                    raise ValueError("Output không phải video đọc được.")
            else:
                from PIL import Image
                with Image.open(target) as image:
                    image.verify()
            checkpoint(stage, {"prompt_id": prompt_id, "state": "downloaded"})
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
                    return
                await asyncio.sleep(2)
            raise ReconcileRequired("Chưa xác nhận prompt đã dừng; giữ GPU ở trạng thái cần đối chiếu.")

    async def language(self, job, prompt: str, images: list[Path], log) -> dict:
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
