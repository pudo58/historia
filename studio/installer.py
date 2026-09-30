"""One reviewed install path; no driver changes, no forced checkout, no stop-on-rerun."""
import asyncio
import hashlib
import json
import re
import shlex
import time

import httpx

from ghm.comfy import check_backend
from ghm.executors.http import comfy_client
from ghm.manifests import ModelAsset
from ghm.model_download import CACHE_HELPERS, DEFAULT_WORKERS, VERIFIED_CACHE, download_many, model_item, snapshot_items
from ghm.remote_service import manage
from ghm.schemas import HostOptions
from studio.install_checks import inventory, summarize
from studio.packs import COMFY_COMMIT, check_model_access, check_remote_model_access
from studio.python_env import NO_PYTHON_EXIT, ensure_python


def private_python(python):
    """Ignore template pip/ Python overrides for this child, never edit the host."""
    ignored = ('PIP_CONSTRAINT', 'PIP_BUILD_CONSTRAINT', 'PIP_REQUIREMENT', 'PIP_TARGET',
               'PIP_PREFIX', 'PIP_USER', 'PYTHONPATH', 'PYTHONHOME')
    return 'env ' + ' '.join('-u ' + key for key in ignored) + ' PIP_CONFIG_FILE=/dev/null ' + shlex.quote(python)


def install_failure(result):
    """Classify without persisting arbitrary pip text, tokens or signed URLs."""
    text = (result.stdout + '\n' + result.stderr).lower()
    if 'resolutionimpossible' in text or 'conflicting dependencies' in text:
        return 'Xung đột phiên bản dependency (pip ResolutionImpossible).'
    if 'no space left' in text or 'insufficient disk' in text:
        return 'Ổ đĩa không đủ chỗ cho dependency/cache.'
    if 'no matching distribution' in text or 'could not find a version' in text:
        return 'Không tìm thấy wheel phù hợp với Python/CUDA hoặc phiên bản đã ghim.'
    if 'certificate_verify_failed' in text:
        return 'Không xác minh được chứng chỉ máy chủ tải package.'
    if 'timed out' in text or 'connectionerror' in text:
        return 'Kết nối tới kho package bị gián đoạn.'
    if result.rc == 44:
        return 'Môi trường đã có dữ liệu không thuộc Studio; không ghi đè.'
    if result.rc == NO_PYTHON_EXIT:
        return ('Pod chưa có Python 3.11/3.12 và Historia không tự cài được (cần mạng tới pypi.org và github.com, '
                'hoặc quyền ghi vào thư mục cài). Cài python3.12 trên Pod rồi bấm cài lại.')
    if 'permission denied' in text or 'read-only file system' in text:
        return 'Không có quyền ghi vào thư mục cài (quyền hoặc ổ chỉ đọc).'
    tail = redacted_tail(result.stdout + '\n' + result.stderr)
    return f'Lệnh cài trả mã lỗi {result.rc}.' + (f' Cuối log (đã ẩn token/URL): {tail}' if tail else '')


def redacted_tail(text: str, lines: int = 6, limit: int = 700) -> str:
    """Last few output lines with anything secret-shaped removed."""
    kept = []
    for line in text.replace('\r', '\n').splitlines():
        line = line.strip()
        if not line or 'downloaded bytes' in line.lower():
            continue
        line = re.sub(r'hf_[A-Za-z0-9]{8,}', 'hf_***', line)
        line = re.sub(r'(?i)(bearer|token|authorization|api[_-]?key|password)([=: ]+)\S+', r'\1\2***', line)
        line = re.sub(r'https?://\S+', '<url>', line)
        kept.append(line[:240])
    return ' | '.join(kept[-lines:])[-limit:]

# "24 GB" cards report slightly less through nvidia-smi (RTX 4090 ≈23.99, RTX PRO 4000 Blackwell ≈23.89 GiB).
MIN_VRAM_GIB = 23.5

SNAPSHOT_SCRIPT = r"""
import hashlib, json, os, pathlib, sys
from huggingface_hub import hf_hub_download

c = json.load(sys.stdin)
__CACHE__
cache = {} if c.get("recheck") else cache_load(pathlib.Path(c["root"]) / c["cache"])
for item in c["snapshots"]:
    destination = pathlib.Path(c["root"]) / "service-models" / item["repo"]
    destination.mkdir(parents=True, exist_ok=True)
    for f in item['files']:
        path = destination / f['filename']
        def valid():
            if not path.is_file() or path.stat().st_size != f['size_bytes']: return False
            if cache_ok(cache, path.resolve(), f['digest']): return True
            h = hashlib.sha256() if f['algorithm']=='sha256' else hashlib.sha1()
            if f['algorithm']=='git-sha1': h.update(('blob '+str(path.stat().st_size)+'\0').encode())
            with path.open('rb') as handle:
                for block in iter(lambda:handle.read(8*1024*1024),b''): h.update(block)
            return h.hexdigest()==f['digest']
        if valid(): continue
        if path.exists(): raise RuntimeError('Existing service model checksum mismatch; refusing overwrite')
        hf_hub_download(repo_id=item['repo'], filename=f['filename'], revision=item['revision'],
                        token=c.get('token') or False, local_dir=destination)
        if not valid(): raise RuntimeError('Service model checksum mismatch')
    print("Verified service model " + item["name"], flush=True)
"""


class InstallExecutor:
    """Each mutation holds a remote flock, inherited by pip/download subprocesses.

    Disconnect does not authorize a second installer while an old child still runs.
    Raw pip/HF output is not persisted: it may contain signed download URLs/tokens.
    """
    def __init__(self, executor, root, scope=None):
        self.executor = executor
        key = hashlib.sha256(root.encode()).hexdigest()[:24]
        # Model downloads use their own lock so they can overlap dependency installs,
        # while an orphaned downloader still blocks a second downloader.
        self.lock_path = '/tmp/historia-install-' + key + ('-' + scope if scope else '') + '.lock'

    async def run(self, command, timeout=None, on_output=None, input_data=None):
        value = 'flock -n -E 73 ' + shlex.quote(self.lock_path) + ' sh -c ' + shlex.quote(command)
        pending = ''
        last_bytes = 0
        def safe_progress(channel, text):
            nonlocal pending, last_bytes
            pending += text
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                line = line.strip()
                match = re.fullmatch(r'Downloaded bytes: (\d{1,16})(?: of (\d{1,16}))?', line)
                if match and int(match[1]) - last_bytes >= 256*1024**2:
                    last_bytes = int(match[1])
                    if match[2] and int(match[2]):
                        on_output('stdout', f'Đã tải {last_bytes/1024**3:.2f} / {int(match[2])/1024**3:.2f} GiB model.')
                    else:
                        on_output('stdout', f'Đã tải {last_bytes/1024**3:.2f} GiB của model hiện tại.')
                ready = re.fullmatch(r'Model ready: ([\w.@/+-]{1,200})', line)
                if ready:
                    on_output('stdout', 'Đã kiểm tra checksum: ' + ready[1])
            pending = pending[-4096:]
        callback = safe_progress if on_output else None
        if input_data is None:
            result = await self.executor.run(value, timeout, on_output=callback)
        else:
            result = await self.executor.run_input(value, input_data, timeout=timeout, on_output=callback)
        if result.rc == 73:
            raise ValueError('Một tiến trình cài/tải model trước đó vẫn đang chạy trên Pod và giữ khóa '
                             '(thường tự dừng trong 1–2 phút). Đợi rồi bấm cài lại; Historia không chạy hai bộ cài '
                             'cùng lúc để tránh hỏng file. File đã tải dở được giữ và tải tiếp.')
        return result

    async def run_input(self, command, data, timeout=60, on_output=None):
        return await self.run(command, timeout, on_output=on_output, input_data=data)

    def __getattr__(self, name):
        return getattr(self.executor, name)


PARALLEL_PROBE = 'sleep 2; echo historia-channel-ok'


async def stop_remote_downloader(executor, pidfile: str) -> None:
    """Best effort: terminate the batch downloader whose PID it wrote itself. Never raises."""
    q = shlex.quote(pidfile)
    command = (f"if [ -f {q} ]; then pid=$(cat {q}); "
               f"case \"$pid\" in ''|*[!0-9]*) ;; *) kill \"$pid\" 2>/dev/null;; esac; rm -f {q}; fi")
    try:
        await asyncio.wait_for(executor.run(command, 20), 25)
    except Exception:  # noqa: BLE001 -- cleanup must not mask the original failure
        pass


async def parallel_channels(executor) -> bool:
    """True only if two remote commands really run at the same time on this connection.

    Full SSH multiplexes channels; a terminal-only gateway may refuse or serialize them.
    Read-only probe; any doubt falls back to the sequential order.
    """
    started = time.monotonic()
    try:
        results = await asyncio.wait_for(asyncio.gather(
            executor.run(PARALLEL_PROBE, 20), executor.run(PARALLEL_PROBE, 20)), 30)
    except Exception:  # noqa: BLE001 -- probe failure only disables the optimisation
        return False
    return (all(r.rc == 0 and 'historia-channel-ok' in r.stdout for r in results)
            and time.monotonic() - started < 3.5)


async def run_together(*steps):
    """Run steps concurrently; the first failure cancels the others (partial files stay resumable)."""
    tasks = [asyncio.ensure_future(step) for step in steps]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def install(hosts, job, lock: dict, log) -> dict:
    host = hosts._require_host(job.host_id)
    options = HostOptions.model_validate(job.snapshot['options'])
    report = hosts.latest_preflight(host.id)
    if not report or not report.gpu or report.status == "fail":
        raise ValueError("Chạy preflight thành công trước khi cài bộ Video lịch sử.")
    driver = int(report.gpu.driver_version.split(".")[0])
    if driver < 570:
        raise ValueError("Bộ cài yêu cầu NVIDIA driver 570+ cho CUDA 12.8; Studio không tự sửa driver.")
    if report.gpu.vram_gb < MIN_VRAM_GIB:
        raise ValueError("Bộ nền này chưa hỗ trợ GPU dưới 24 GB. Không tự đổi sang model/chất lượng khác.")
    log('Đang kiểm tra quyền tải từng file model trên Hugging Face; chưa tải hoặc cài dependency.')
    await asyncio.to_thread(check_model_access, lock, hosts.setting('hf_token'))
    log('Đã xác minh quyền tải toàn bộ file model của bộ cài.')
    base = hosts.executor_for(host)
    executor = InstallExecutor(base, options.root)
    model_executor = InstallExecutor(base, options.root, scope='models')
    root, comfy = options.root, options.comfy_root
    q = shlex.quote
    python = root + "/venv/bin/python"
    async def command(value: str, timeout=1800, stage='Cài dependency'):
        result = await executor.run(value, timeout, on_output=lambda channel, line: log(line))
        if result.rc:
            raise ValueError(stage + ': ' + install_failure(result) + ' Các tệp cũ được giữ nguyên.')
        return result

    async def create_environment(directory, stage='Tạo môi trường Python'):
        # Adopt mode must not quietly overwrite an unrelated pre-existing venv.
        marker = directory + '/.studio-env-owned'
        d, py = q(directory), q(directory + '/bin/python')
        host_python = q(py3)
        get_pip = ("import sys,urllib.request;exec(urllib.request.urlopen("
                   "'https://bootstrap.pypa.io/get-pip.py',timeout=120).read())")
        await command(
            f"if [ -d {d} ] && [ -n \"$(ls -A {d})\" ] && [ ! -f {q(marker)} ]; then exit 44; fi; "
            f"mkdir -p {d} && touch {q(marker)} && trap 'touch {marker}' EXIT && "
            f"echo \"python: {host_python} $({host_python} -V 2>&1)\"; "
            f"if {host_python} -m venv --clear {d} && {py} -m pip --version; then :; else "
            f"echo 'step 2: installing the versioned venv package'; "
            f"V=$({host_python} -c 'import sys;print(f\"{{sys.version_info[0]}}.{{sys.version_info[1]}}\")'); "
            f"if command -v apt-get >/dev/null; then S=''; [ \"$(id -u)\" = 0 ] || S='sudo -n'; "
            f"$S env DEBIAN_FRONTEND=noninteractive apt-get install -y python$V-venv python3-pip >/dev/null 2>&1 || true; fi; "
            f"if {host_python} -m venv --clear {d} && {py} -m pip --version; then :; else "
            f"echo 'step 3: bootstrap pip into a bare venv'; "
            f"{host_python} -m venv --clear --without-pip {d} && {py} -c {q(get_pip)} --no-warn-script-location && {py} -m pip --version; fi; fi "
            f"&& touch {q(marker)}", stage=stage + ' ' + directory.rsplit('/', 1)[-1])
    try:
        await check_remote_model_access(executor, lock, hosts.setting('hf_token'))
        log('Đã kiểm tra quyền tải HEAD từ Pod; chưa cài hoặc tải model.')
        log('Đang kiểm tra checksum các model đã có; có thể mất vài phút, không tải lại file hợp lệ.')
        current = summarize(await inventory(executor, options, lock), options)
        if current['blockers']:
            raise ValueError(' '.join(current['blockers']))
        # If an existing backend is serving jobs, do not modify its environment.
        running = False
        try:
            async with comfy_client(executor, options.remote_port, timeout=5) as client:
                response = await client.get("/queue")
                if response.is_success and isinstance(response.json().get('queue_running'), list) and isinstance(response.json().get('queue_pending'), list):
                    running = True
                    if response.json().get("queue_running") or response.json().get("queue_pending"):
                        raise ValueError("ComfyUI đang có job. Không cài/sửa dependency khi GPU đang chạy.")
        except (httpx.HTTPError, ConnectionError):
            pass
        if current['inventory']['port_busy'] and not running:
            raise ValueError('Port đang có dịch vụ nhưng không xác nhận được queue ComfyUI. Không sửa môi trường đó; kiểm tra dịch vụ hoặc chọn port khác.')
        log("Kiểm tra volume, Python, quyền cài đặt và môi trường có sẵn.")
        required = current.get('service_required_bytes', current['required_bytes'])
        await command(f"mkdir -p {q(root)}", 30)
        # Python 3.11/3.12 for the virtualenvs: the Pod's own if suitable, else one installed next to it.
        py3 = await ensure_python(command, root, log)
        await command(f"{q(py3)} -c 'import sys,shutil; assert sys.version_info >= (3,11); assert shutil.disk_usage(sys.argv[1]).free > int(sys.argv[2]), \"Insufficient disk space\"' {q(root)} {required}", 30)
        if not options.adopt_existing:
            await command('touch ' + q(root + '/.studio-owned'), 15)
        deps = await executor.run("command -v git && command -v ffmpeg && command -v espeak-ng && " + q(py3) + " -c 'import venv,ensurepip'", 30)
        if deps.rc:
            log('Bổ sung công cụ hệ thống còn thiếu: git/FFmpeg/espeak-ng/venv. Không thay đổi môi trường Python của ComfyUI.')
            await command("if command -v apt-get >/dev/null; then "
                          "if [ \"$(id -u)\" = 0 ]; then apt-get update && env DEBIAN_FRONTEND=noninteractive apt-get install -y git ffmpeg espeak-ng python3-venv build-essential; "
                          "else sudo -n apt-get update && sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y git ffmpeg espeak-ng python3-venv build-essential; fi; "
                          "else echo 'Install git ffmpeg espeak-ng Python3.11+ venv build tools manually for this distribution'; exit 40; fi")
        base_pending = False
        if not options.adopt_existing:
            check = await executor.run(f"test -x {q(python)} && test -f {q(root + '/.studio-base-' + COMFY_COMMIT)} && test \"$(git -C {q(comfy)} rev-parse HEAD)\" = {COMFY_COMMIT}", 30)
            if check.rc:
                if running:
                    raise ValueError("ComfyUI đang chạy nhưng chưa đúng bộ môi trường. Dùng Adopt hoặc chọn root riêng; không tự dừng dịch vụ.")
                log("Cài ComfyUI và PyTorch trong virtualenv riêng.")
                # Checkout first: it needs an empty directory and creates models/,
                # so model downloads can start while PyTorch/requirements install.
                await command(f"if [ -d {q(comfy + '/.git')} ]; then test -z \"$(git -C {q(comfy)} status --porcelain)\" && test \"$(git -C {q(comfy)} remote get-url origin)\" = https://github.com/Comfy-Org/ComfyUI.git; "
                              f"else mkdir -p {q(comfy)} && test -z \"$(ls -A {q(comfy)})\" && git -C {q(comfy)} init && git -C {q(comfy)} remote add origin https://github.com/Comfy-Org/ComfyUI.git; fi && "
                              f"git -C {q(comfy)} fetch --depth 1 origin {COMFY_COMMIT} && git -C {q(comfy)} checkout --detach {COMFY_COMMIT}")
                base_pending = True
        else:
            await command("test -f " + q(comfy + "/main.py"), 30)
            log("Adopt: giữ nguyên ComfyUI và dependency hiện có; chỉ bổ sung model còn thiếu và môi trường dịch vụ riêng.")

        async def install_dependencies():
            if base_pending:
                await create_environment(root + '/venv')
                wheel_index = "cu130" if driver >= 580 else "cu128"
                await command(f"{private_python(python)} -m pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/{wheel_index}")
                await command(f"{private_python(python)} -m pip install -r {q(comfy + '/requirements.txt')} && {private_python(python)} -m pip check && "
                              f"{q(python)} -c 'import torch; assert torch.cuda.is_available(); (torch.ones(1,device=\"cuda\")+1).cpu()' && touch {q(root + '/.studio-base-' + COMFY_COMMIT)}")
            for environment, packages in lock['environments'].items():
                env_hash = hashlib.sha256(packages.encode()).hexdigest()[:16]
                marker = root + "/" + environment + "-venv/.studio-installed-" + env_hash
                exists = await executor.run("test -f " + q(marker), 15)
                if exists.rc:
                    log("Cài môi trường riêng: " + environment)
                    py = root + "/" + environment + "-venv/bin/python"
                    directory = root + '/' + environment + '-venv'
                    await create_environment(directory)
                    log(environment + ': cài PyTorch CUDA trong venv riêng, bỏ constraint kế thừa từ template cho lệnh này.')
                    await command(f"{private_python(py)} -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128", stage=environment + ' / PyTorch')
                    log(environment + ': cài thư viện ứng dụng và kiểm tra dependency.')
                    await command(f"{private_python(py)} -m pip install {packages} --index-url https://pypi.org/simple && {private_python(py)} -m pip check && {private_python(py)} -m pip freeze > {q(root + '/' + environment + '-resolved.txt')} && touch {q(marker)}", stage=environment + ' / thư viện ứng dụng')

        token = hosts.setting('hf_token')
        assets = [ModelAsset.model_validate(item) for item in lock['models']]
        for asset in assets:
            if asset.gated and not token:
                raise ValueError(f"{asset.name}: add a read-only Hugging Face token in Settings.")
        items = [model_item(asset, comfy) for asset in assets] + snapshot_items(lock.get('snapshots', []), root)

        async def download_models():
            total = sum(item['size_bytes'] or 0 for item in items) / 1024**3
            log(f'Tải {len(items)} file model ({total:.1f} GiB), tối đa {DEFAULT_WORKERS} file cùng lúc; '
                'SHA256 kiểm tra ngay khi tải, file hợp lệ được giữ.')
            pidfile = root + '/.historia-download.pid'
            if options.recheck_models:
                log('Kiểm tra lại checksum toàn bộ model (bỏ qua bộ nhớ đệm); bước này đọc lại mọi file.')
            try:
                await download_many(model_executor, items, [comfy + '/models', root + '/service-models'], token,
                                    28800, on_output=lambda channel, line: log(line),
                                    cache=root + '/' + VERIFIED_CACHE, recheck=options.recheck_models, pidfile=pidfile)
            except asyncio.CancelledError:
                # Another install step failed: stop the remote downloader too, so it does not keep
                # the model lock (partial .part files stay and resume on the next install).
                await asyncio.shield(stop_remote_downloader(base, pidfile))
                raise
            log('Đã tải và kiểm tra checksum toàn bộ file model.')

        if await parallel_channels(base):
            log('Tải model song song với cài dependency.')
            await run_together(download_models(), install_dependencies())
        else:
            log('Kết nối SSH không chạy được hai lệnh cùng lúc; tải model xong rồi mới cài dependency.')
            await download_models()
            await install_dependencies()

        result = await executor.run_input(q(root + "/llm-venv/bin/python") + " -c " + q(SNAPSHOT_SCRIPT.replace('__CACHE__', CACHE_HELPERS)),
                                          json.dumps({"root": root, "snapshots": lock["snapshots"],
                                                      "cache": VERIFIED_CACHE, "recheck": options.recheck_models,
                                                      "token": hosts.setting("hf_token")}), timeout=14400,
                                          on_output=lambda channel, line: log(line))
        if result.rc:
            raise ValueError("Kiểm tra model LLM/TTS thất bại.")
        log('Đã kiểm tra checksum toàn bộ file model. Đang kiểm tra ComfyUI; chưa đánh dấu workflow đã kiểm chứng.')
        if not running and not options.adopt_existing:
            log(await manage(executor, options, "start", []))
        for attempt in range(12):
            try:
                version = await check_backend(executor, options, log, False)
                return {"comfy_version": version, "verified": False,
                        "message": "Đã cài. Chạy Kiểm chứng & benchmark để kiểm tra ảnh, video và giọng đọc thật."}
            except httpx.HTTPError:
                if attempt == 11:
                    raise ValueError("ComfyUI chưa khởi động được; xem log trên host.")
                await asyncio.sleep(5)
        raise ValueError("ComfyUI không phản hồi.")
    finally:
        await executor.close()
