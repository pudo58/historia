"""Download to partial files, resume, verify, then promote. Tokens only travel on stdin.

One remote command downloads a whole batch with a small thread pool. Each file is
hashed while it streams (a resumed file hashes its existing prefix first), so a
finished download is not read back a second time before promotion.
"""
import json
import re
import shlex
from pathlib import PurePosixPath
from urllib.parse import quote

DEFAULT_WORKERS = 4

BATCH_SCRIPT = r"""
import hashlib, json, os, pathlib, shutil, sys, threading, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
c = json.load(sys.stdin)
roots = [pathlib.Path(r).resolve() for r in c["roots"]]
items = sorted(c["items"], key=lambda i: -(i.get("size_bytes") or 0))
guard, stop = threading.Lock(), threading.Event()
state = {"done": 0, "notified": 0}
step = int(c.get("progress_bytes") or 32*1024*1024)
total = sum(i.get("size_bytes") or 0 for i in items)
def say(line):
    with guard:
        print(line, flush=True)
def advance(count):
    with guard:
        state["done"] += count
        if state["done"] - state["notified"] >= step:
            state["notified"] = state["done"]
            print("Downloaded bytes: %d of %d" % (state["done"], total), flush=True)
def hasher(item):
    if item["algorithm"] == "sha256":
        return hashlib.sha256()
    h = hashlib.sha1()
    h.update(("blob %d\0" % item["size_bytes"]).encode())
    return h
def feed(h, path):
    count = 0
    with path.open("rb") as f:
        while True:
            block = f.read(8*1024*1024)
            if not block:
                return count
            h.update(block)
            count += len(block)
def paths(item):
    target = pathlib.Path(item["target"]).resolve()
    if not any(target.is_relative_to(root) for root in roots):
        raise RuntimeError("Model destination escapes the allowed model directories.")
    return target, target.with_name(target.name + ".part")
class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise RuntimeError("Refusing an insecure download redirect.")
        result = super().redirect_request(req, fp, code, msg, hdrs, newurl)
        if urllib.parse.urlparse(req.full_url).netloc != urllib.parse.urlparse(newurl).netloc:
            result.remove_header("Authorization")
        return result
opener = urllib.request.build_opener(Redirect())
def fetch(item):
    if stop.is_set():
        raise RuntimeError("Stopped after another download failed.")
    target, part = paths(item)
    want = item["digest"].lower()
    target.parent.mkdir(parents=True, exist_ok=True)
    if part.is_symlink():
        raise RuntimeError("Partial download is a symlink.")
    if target.is_file():
        h = hasher(item)
        advance(feed(h, target))
        if h.hexdigest() == want:
            say("Model ready: " + item["name"])
            return
        raise RuntimeError("Existing model has a different checksum; rename it before retrying.")
    if target.exists():
        raise RuntimeError("Existing model path is not a regular file.")
    h, offset = hasher(item), 0
    if part.exists():
        offset = feed(h, part)
        size = item.get("size_bytes")
        if size is not None and offset > size:
            os.replace(part, part.with_name(part.name + ".invalid-" + str(time.time_ns())))
            h, offset = hasher(item), 0
        elif offset and offset == size and h.hexdigest() == want:
            os.replace(part, target)
            advance(offset)
            say("Model ready: " + item["name"])
            return
    advance(offset)
    headers = {"Range": "bytes=%d-" % offset} if offset else {}
    if item.get("auth") and c.get("token"):
        if urllib.parse.urlparse(item["url"]).netloc != "huggingface.co":
            raise RuntimeError("Refusing to send a Hugging Face token to another domain.")
        headers["Authorization"] = "Bearer " + c["token"]
    with opener.open(urllib.request.Request(item["url"], headers=headers), timeout=60) as response:
        append = bool(offset) and response.status == 206
        if append and not response.headers.get("Content-Range", "").startswith("bytes %d-" % offset):
            raise RuntimeError("Invalid resume Content-Range.")
        if offset and not append:
            advance(-offset)
            h, offset = hasher(item), 0
        with part.open("ab" if append else "wb") as f:
            while True:
                if stop.is_set():
                    raise RuntimeError("Stopped after another download failed.")
                block = response.read(4*1024*1024)
                if not block:
                    break
                f.write(block)
                h.update(block)
                advance(len(block))
    if h.hexdigest() != want:
        os.replace(part, part.with_name(part.name + ".invalid-" + str(time.time_ns())))
        raise RuntimeError("Checksum mismatch. Partial file quarantined; retry starts cleanly.")
    os.replace(part, target)
    say("Model ready: " + item["name"])
# Free space per filesystem for everything still missing, before any transfer starts.
needed = {}
for item in items:
    target, part = paths(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        continue
    have = part.stat().st_size if part.is_file() and not part.is_symlink() else 0
    device = os.stat(target.parent).st_dev
    free = shutil.disk_usage(target.parent).free
    missing = max(0, (item.get("size_bytes") or 0) - have)
    needed[device] = (needed.get(device, (0, free))[0] + missing, free)
if any(missing + 512*1024*1024 > free for missing, free in needed.values()):
    say("Insufficient free space on the selected model volume; nothing was downloaded.")
    sys.exit(1)
failed = []
def run(item):
    try:
        fetch(item)
    except BaseException as exc:
        stop.set()
        failed.append((item["name"], exc))
with ThreadPoolExecutor(max_workers=max(1, min(int(c.get("workers") or 1), 8))) as pool:
    list(pool.map(run, items))
for name, exc in failed:
    if isinstance(exc, RuntimeError) and str(exc).startswith("Stopped"):
        continue
    # Our own RuntimeError texts are safe; library errors may carry URLs, so report only the type.
    say("Model failed: " + name + " (" + (str(exc) if type(exc) is RuntimeError else type(exc).__name__) + ")")
if failed:
    sys.exit(1)
print("All downloads complete; checksums verified.", flush=True)
"""

_SAFE_NAME = re.compile(r"[\w.@/+-]{1,200}")


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("Model file path must be relative without '..'.")
    return str(path)


def model_item(asset, comfy: str) -> dict:
    """A pinned ComfyUI model file for the batch downloader."""
    return {"name": asset.name, "target": asset.target(comfy), "url": asset.download_url(),
            "digest": asset.sha256, "algorithm": "sha256", "size_bytes": asset.size_bytes,
            "auth": bool(asset.gated)}


def snapshot_items(snapshots: list[dict], root: str) -> list[dict]:
    """Pinned service files (LLM/TTS), written where the service snapshot verifier expects them."""
    items = []
    for snapshot in snapshots:
        repo, revision = snapshot["repo"], snapshot["revision"]
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("Service snapshot is not pinned to a repository revision.")
        for f in snapshot["files"]:
            filename = _safe_relative(f["filename"])
            if f["algorithm"] not in {"sha256", "git-sha1"} or f.get("size_bytes") is None:
                raise ValueError("Service model file lacks a checksum or size.")
            items.append({"name": snapshot["name"] + "/" + filename,
                          "target": f"{root}/service-models/{repo}/{filename}",
                          "url": f"https://huggingface.co/{repo}/resolve/{revision}/{quote(filename, safe='/')}",
                          "digest": f["digest"], "algorithm": f["algorithm"],
                          "size_bytes": f["size_bytes"], "auth": True})
    return items


def failed_names(output: str) -> list[str]:
    names = []
    for line in output.splitlines():
        if line.startswith("Model failed: "):
            name = line.removeprefix("Model failed: ").split(" (", 1)[0].strip()
            if _SAFE_NAME.fullmatch(name):
                names.append(name)
    return names


async def download_many(executor, items: list[dict], roots: list[str], token: str | None,
                        timeout: float, on_output=None, workers: int = DEFAULT_WORKERS) -> str:
    """Download several pinned files concurrently in one remote command."""
    if not items:
        return ""
    for item in items:
        if not _SAFE_NAME.fullmatch(item["name"]):
            raise ValueError("Model name is not safe to report.")
    payload = {"roots": roots, "items": items, "workers": workers,
               "token": token if any(i.get("auth") for i in items) else None}
    result = await executor.run_input("python3 -c " + shlex.quote(BATCH_SCRIPT), json.dumps(payload),
                                      timeout=timeout, on_output=on_output)
    if result.rc:
        # Do not return downloader tracebacks, redirects or authorization headers.
        if "Insufficient free space on the selected model volume" in result.stdout:
            raise ValueError("Insufficient free space on the model volume for the missing files; nothing was downloaded.")
        names = failed_names(result.stdout) or [i["name"] for i in items][:1]
        raise ValueError(", ".join(names) + ": download/verification failed. Check free disk, HF access and checksum.")
    return result.stdout


async def download(executor, asset, comfy: str, token: str | None, timeout: float, on_output=None):
    if asset.gated and not token:
        raise ValueError(f"{asset.name}: add a read-only Hugging Face token in Settings.")
    return await download_many(executor, [model_item(asset, comfy)], [comfy + "/models"],
                               token, timeout, on_output=on_output, workers=1)
