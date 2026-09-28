"""Small dependency-free local web UI for the Phase 1 pipeline."""

import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from vv.config import load_config
from vv.manifest import Manifest, sha256_file
from vv.stages import encode, extract, probe

ROOT = Path(__file__).parent / "web_static"
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def _run_job(job_id: str, video: Path, config_path: Path) -> None:
    try:
        cfg = load_config(config_path)
        run_id = uuid4().hex[:12]
        run_dir = cfg.runs_dir / run_id
        manifest = Manifest(run_dir / "manifest.json")
        source_hash = sha256_file(video)
        metadata = probe.run(video)
        manifest.mark("probe", "complete", source_hash, metadata=metadata)
        frames_dir = run_dir / "frames"
        count = extract.run(video, frames_dir)
        manifest.mark("extract_frames", "complete", source_hash, frame_count=count)
        output = run_dir / "output.mp4"
        encode.run(frames_dir, video, output, metadata, cfg.encode.crf)
        manifest.mark("encode", "complete", f"{source_hash}:{count}:{cfg.encode.crf}", output=str(output), frame_count=count)
        with LOCK:
            JOBS[job_id].update(status="complete", run_id=run_id, output=str(output), metadata=metadata)
    except Exception as exc:  # surfaced in the UI; the server stays alive
        with LOCK:
            JOBS[job_id].update(status="failed", error=str(exc), traceback=traceback.format_exc())


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/":
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif route == "/api/jobs":
            with LOCK:
                payload = list(JOBS.values())
            self._send(200, json.dumps(payload).encode())
        elif route.startswith("/api/jobs/"):
            job_id = route.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
            self._send(200 if job else 404, json.dumps(job or {"error": "job not found"}).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/jobs":
            self._send(404, b'{"error":"not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            video = Path(payload["video_path"]).expanduser()
            config = Path(payload.get("config_path", "configs/example.yaml")).expanduser()
            if not video.is_file():
                raise ValueError(f"Video does not exist: {video}")
            if not config.is_file():
                raise ValueError(f"Config does not exist: {config}")
            job_id = uuid4().hex[:12]
            with LOCK:
                JOBS[job_id] = {"job_id": job_id, "status": "running", "video": str(video)}
            threading.Thread(target=_run_job, args=(job_id, video, config), daemon=True).start()
            self._send(202, json.dumps(JOBS[job_id]).encode())
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode())

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"vv web UI: http://{host}:{port}")
    server.serve_forever()

