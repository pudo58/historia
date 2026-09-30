"""A small ownership-checked process supervisor sent over SSH stdin."""
import json
import shlex

SCRIPT = r"""
import json, os, pathlib, signal, subprocess, sys, time
c = json.load(sys.stdin)
root = pathlib.Path(c["root"]).resolve()
comfy = root / "ComfyUI"
python = root / "venv/bin/python"
gpu = c.get("gpu_index")
# Lanes (one ComfyUI per GPU on the same install) each own a marker and log; the classic single
# process keeps its historic file names.
suffix = "" if gpu is None else f"-gpu{int(gpu)}"
marker = root / f".ghm-process{suffix}.json"
action = c["action"]

def alive_owned():
    if not marker.exists():
        return None
    data = json.loads(marker.read_text())
    pid = data["pid"]
    try:
        # PID + kernel start time + full argv prevent terminating a recycled/unrelated PID.
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except FileNotFoundError:
        return None
    if stat[0] in ("Z", "X") or argv == [b""]:
        # Exited but not yet reaped (containers whose PID 1 does not reap orphans), or mid-exit
        # with its command line already released: it is gone, not a foreign process.
        return None
    if str(stat[19]) != data["start"] or argv[:-1] != [x.encode() for x in data["argv"]]:
        raise RuntimeError("Process ownership mismatch; refusing to stop or replace it.")
    return pid

pid = alive_owned()
if action == "inspect-stopped":
    if pid or not marker.exists():
        raise RuntimeError("Expected a stopped, previously owned service.")
    data = json.loads(marker.read_text())
    if data.get("argv", [])[:2] != [str(python), str(comfy / 'main.py')]:
        raise RuntimeError("Stopped service ownership mismatch.")
    print(json.dumps({"owned": True, "stopped": True, "argv": data['argv']}))
elif action == "inspect":
    if not pid:
        raise RuntimeError("No live owned ComfyUI process; refusing runtime maintenance.")
    print(json.dumps({"owned": True, "pid": pid, "argv": json.loads(marker.read_text())['argv']}))
elif action == "stop":
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not alive_owned():
                break
            time.sleep(.1)
        else:
            raise RuntimeError("Managed process did not stop in 10 seconds.")
    print("Managed service stopped. GPU rental is NOT stopped.")
elif action == "start":
    if pid:
        print("Managed process already running.")
    else:
        if not (comfy / "main.py").is_file() or not python.is_file():
            raise RuntimeError("The managed ComfyUI install or virtualenv is missing.")
        extra = list(c["args"])
        env = dict(os.environ)
        if gpu is not None:
            if "--cuda-device" in extra:
                i = extra.index("--cuda-device")
                del extra[i:i + 2]
            extra += ["--cuda-device", str(int(gpu))]
            env["CUDA_VISIBLE_DEVICES"] = str(int(gpu))
        argv = [str(python), str(comfy / "main.py"), "--listen", "127.0.0.1",
                "--port", str(c["port"])] + extra
        with (root / f"comfyui{suffix}.log").open("ab") as log:
            p = subprocess.Popen(argv, cwd=comfy, stdin=subprocess.DEVNULL, stdout=log, env=env,
                                 stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        start = pathlib.Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        marker.write_text(json.dumps(dict(pid=p.pid, start=start, argv=argv)))
        time.sleep(2)
        if p.poll() is not None:
            raise RuntimeError("ComfyUI exited on startup. Inspect the managed comfyui.log.")
        print("Managed ComfyUI started on loopback.")
else:
    raise RuntimeError("Unknown service action.")
"""


async def manage(executor, options, action: str, args: list[str]):
    if options.adopt_existing:
        raise ValueError("Adopt mode never starts/stops an externally managed process.")
    result = await executor.run_input(
        "python3 -c " + shlex.quote(SCRIPT),
        json.dumps({'root': options.root, 'port': options.remote_port, 'args': args, 'action': action,
                    'gpu_index': options.gpu_index}),
        timeout=30,
    )
    if result.rc:
        # Remote traceback contains no submitted credentials.
        raise ValueError("Managed service action failed. Inspect " + options.root + "/comfyui" +
                         ("" if options.gpu_index is None else f"-gpu{options.gpu_index}") + ".log and process ownership.")
    return result.stdout
