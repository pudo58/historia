"""Download to a partial file, resume, verify, then promote. Tokens only travel on stdin."""
import json
import shlex

SCRIPT = r"""
import hashlib, json, os, pathlib, sys, time, urllib.request, urllib.parse, shutil
c = json.load(sys.stdin)
root = pathlib.Path(c["comfy"]).resolve() / "models"
target = pathlib.Path(c["target"]).resolve()
if not target.is_relative_to(root.resolve()):
    raise RuntimeError("Model destination escapes the ComfyUI model directory.")
target.parent.mkdir(parents=True, exist_ok=True)
part = target.with_name(target.name + ".part")
if part.is_symlink():
    raise RuntimeError("Partial download is a symlink.")
def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
if target.is_file() and digest(target) == c["sha256"]:
    print("Verified existing model.")
    sys.exit(0)
if target.exists():
    raise RuntimeError("Existing model has a different checksum; rename it before retrying.")
if part.exists() and digest(part) == c["sha256"]:
    os.replace(part, target)
    print("Verified and promoted completed partial file.")
    sys.exit(0)
offset = part.stat().st_size if part.exists() else 0
required = max(0, (c.get("size_bytes") or 0) - offset)
if shutil.disk_usage(target.parent).free < required + 512*1024*1024:
    raise RuntimeError("Insufficient free space on the selected model volume.")
headers = {"Range": f"bytes={offset}-"} if offset else {}
if c.get("token"):
    if urllib.parse.urlparse(c["url"]).netloc != "huggingface.co":
        raise RuntimeError("Refusing to send a Hugging Face token to another domain.")
    headers["Authorization"] = "Bearer " + c["token"]
class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise RuntimeError("Refusing an insecure download redirect.")
        result = super().redirect_request(req, fp, code, msg, hdrs, newurl)
        if urllib.parse.urlparse(req.full_url).netloc != urllib.parse.urlparse(newurl).netloc:
            result.remove_header("Authorization")
        return result
opener = urllib.request.build_opener(Redirect())
with opener.open(urllib.request.Request(c["url"], headers=headers), timeout=60) as response:
    append = offset and response.status == 206
    if append and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
        raise RuntimeError("Invalid resume Content-Range.")
    with part.open("ab" if append else "wb") as f:
        transferred = offset if append else 0
        notified = transferred
        while True:
            block = response.read(4*1024*1024)
            if not block:
                break
            f.write(block)
            transferred += len(block)
            if transferred - notified >= 32*1024*1024:
                print("Downloaded bytes: " + str(transferred), flush=True)
                notified = transferred
if digest(part) != c["sha256"]:
    quarantine = part.with_name(part.name + ".invalid-" + str(time.time_ns()))
    os.replace(part, quarantine)
    raise RuntimeError("Checksum mismatch. Partial file quarantined; retry starts cleanly.")
os.replace(part, target)
print("Download complete; SHA256 verified.")
"""


async def download(executor, asset, comfy: str, token: str | None, timeout: float, on_output=None):
    if asset.gated and not token:
        raise ValueError(f"{asset.name}: add a read-only Hugging Face token in Settings.")
    result = await executor.run_input("python3 -c " + shlex.quote(SCRIPT), json.dumps({
        "comfy": comfy, "target": asset.target(comfy), "url": asset.download_url(),
        "sha256": asset.sha256, "size_bytes": asset.size_bytes,
        "token": token if asset.gated else None,
    }), timeout=timeout, on_output=on_output)
    if result.rc:
        # Do not return downloader tracebacks, redirects or authorization headers.
        raise ValueError(f"{asset.name}: download/verification failed. Check free disk, HF access and checksum.")
    return result.stdout
