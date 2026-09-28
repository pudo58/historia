# Historical Video Studio / GPU Host Manager / vv

## Studio mới — địa chỉ localhost thống nhất

```powershell
.\.venv\Scripts\python.exe -m ghm.cli serve --port 8000
```

Mở **http://127.0.0.1:8000**. Phần dự án/tư liệu/storyboard đã được nối vào UI tiếng Việt;
quản lý GPU cũ nằm ở mục riêng. Bộ AI tự cài và render trên GPU thật **chưa nghiệm thu**.
Xem [trạng thái và phần còn thiếu](STUDIO_STATUS.md) trước khi thử cài lên máy thuê.
Giao diện cũ ở cổng 8765 thuộc VV bên dưới, không phải Studio.

---

## VV legacy

Phase 1 skeleton for a resumable, GPU-agnostic video editing pipeline.

## Local setup

```powershell
uv sync --extra dev
uv run pytest
uv run vv init --path configs/example.yaml
uv run vv web
```

FFmpeg and ffprobe must be installed and available on `PATH` for video commands.

## Phase 1 commands

```text
vv init
vv probe input.mp4
vv run input.mp4 --config configs/example.yaml
vv resume RUN_ID --config configs/example.yaml
vv web
```

Open http://127.0.0.1:8765 after starting `vv web` to use the local dashboard.

The current phase implements configuration, manifests, probing, lossless PNG frame
extraction, and encoding with an exact frame-count assertion. GPU/backend work is
intentionally not included yet.

## GPU Host Manager (Phase 1)

`ghm` is a separate local-only FastAPI and React/Vite application for registering
SSH GPU hosts. It stores credentials encrypted at rest, requires explicit SSH
fingerprint confirmation before a host can be used, and has no ComfyUI installer
logic yet.

```powershell
$env:GHM_MASTER_KEY = "set-a-long-private-master-key"
python -m uvicorn ghm.api:app --host 127.0.0.1 --port 8000
Set-Location ghm/frontend
npm run dev
```

Open http://127.0.0.1:5173. If `GHM_MASTER_KEY` is omitted, GHM attempts to keep
its master key in the operating-system credential store through `keyring`.

After a successful ComfyUI install and smoke test, start the private tunnel from
the host card. The REST contract then exposes `local_url`, and the backend can be
exported with:

```powershell
python -m ghm.cli export-backend HOST_ID
```
