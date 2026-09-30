"""Safe, read-only preflight collection and parsing for Linux GPU hosts."""

import re
from dataclasses import dataclass

from ghm.executors.base import CommandResult, Executor
from ghm.executors.pty_probe import PreflightTransportError, PTYRequiredError, requires_pty
from ghm.schemas import CheckResult, DiskInfo, GPUInfo, PreflightReport

COMMANDS: dict[str, str] = {
    "os": "cat /etc/os-release",
    "identity": "id -u",
    "container": (
        "if [ -f /.dockerenv ]; then echo docker; "
        "elif grep -qaE 'docker|containerd|kubepods' /proc/1/cgroup 2>/dev/null; "
        "then echo container; else echo vm-or-bare-metal; fi"
    ),
    "gpu": "nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv,noheader,nounits; nvidia-smi -L",
    "cuda": "nvcc --version",
    "memory": "free -b",
    "memory_limit": "cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null",
    "disk": "df -B1 -P / /workspace /data 2>/dev/null || df -B1 -P /",
    "python": "python3 --version || python --version",
    "network": "getent ahostsv4 github.com >/dev/null && getent ahostsv4 huggingface.co >/dev/null",
}


@dataclass(frozen=True)
class CollectedPreflight:
    values: dict[str, CommandResult]


def parse_os_release(output: str) -> str | None:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values.get("PRETTY_NAME") or values.get("NAME")


def parse_nvidia_smi(output: str, cuda_output: str = "") -> GPUInfo | None:
    line = next((line.strip() for line in output.splitlines() if "," in line), "")
    fields = [part.strip() for part in line.split(",")]
    if len(fields) < 3 or not fields[0] or not fields[2]:
        return None
    try:
        vram_gb = round(float(fields[1]) / 1024, 2)
    except ValueError:
        # A MIG slice hides full-GPU memory behind an permission placeholder. The -L profile is already in GB.
        slices = re.findall(r"MIG\s+\d+g\.(\d+(?:\.\d+)?)gb", output, re.IGNORECASE)
        if not slices:
            return None
        vram_gb = round(sum(float(item) for item in slices), 2)
    cuda_match = re.search(r"release\s+([\d.]+)", cuda_output)
    listed = len(re.findall(r"^GPU\s+\d+:", output, re.MULTILINE))
    return GPUInfo(
        count=max(1, listed),
        name=fields[0],
        vram_gb=vram_gb,
        driver_version=fields[2],
        compute_capability=fields[3] if len(fields) > 3 and fields[3] else None,
        cuda_version=cuda_match.group(1) if cuda_match else None,
    )


def parse_memory_gb(output: str) -> float | None:
    line = next((line for line in output.splitlines() if line.startswith("Mem:")), None)
    if line is None:
        return None
    fields = line.split()
    try:
        return round(int(fields[1]) / 1024**3, 2)
    except (IndexError, ValueError):
        return None


def parse_disks(output: str) -> list[DiskInfo]:
    disks: list[DiskInfo] = []
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 6:
            continue
        try:
            disks.append(DiskInfo(mount=fields[-1], available_gb=round(int(fields[3]) / 1024**3, 2)))
        except ValueError:
            continue
    return list({disk.mount: disk for disk in disks}.values())


def parse_python_version(output: str) -> str | None:
    match = re.search(r"Python\s+([\d.]+)", output)
    return match.group(1) if match else None


def _status(checks: list[CheckResult]) -> str:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "pass"


def evaluate(results: dict[str, CommandResult]) -> PreflightReport:
    if any(requires_pty(result) for result in results.values()):
        return PreflightReport(status="fail", checks=[CheckResult(name="SSH command execution", status="fail",
            message="Gateway SSH yêu cầu PTY. Chưa thực hiện được kiểm tra GPU/RAM/Python/DNS; không phải máy thiếu GPU.")])
    os_name = parse_os_release(results["os"].stdout) if results["os"].rc == 0 else None
    gpu = parse_nvidia_smi(results["gpu"].stdout, results["cuda"].stdout) if results["gpu"].rc == 0 else None
    ram_gb = parse_memory_gb(results["memory"].stdout) if results["memory"].rc == 0 else None
    limit = results.get("memory_limit")
    if ram_gb and limit and limit.rc == 0 and limit.stdout.strip().isdigit():
        bounded_gb = round(int(limit.stdout.strip()) / 1024**3, 2)
        if bounded_gb > 0:
            ram_gb = min(ram_gb, bounded_gb)
    disks = parse_disks(results["disk"].stdout) if results["disk"].rc == 0 else []
    python_version = parse_python_version(results["python"].stdout) if results["python"].rc == 0 else None
    checks = [
        CheckResult(
            name="Operating system",
            status="pass" if os_name else "fail",
            message=os_name or "Could not read /etc/os-release.",
        ),
        CheckResult(
            name="NVIDIA GPU",
            status="fail" if gpu is None else "warn" if gpu.vram_gb < 8 else "pass",
            message=("Không đọc được thông tin NVIDIA GPU từ nvidia-smi; kiểm tra lệnh/driver/quyền truy cập, chưa thể kết luận máy thiếu GPU." if gpu is None else f"{gpu.name}, {gpu.vram_gb:g} GB VRAM."),
        ),
        CheckResult(
            name="Memory",
            status="fail" if ram_gb is None else "warn" if ram_gb < 16 else "pass",
            message=("Could not determine host memory." if ram_gb is None else f"{ram_gb:g} GB total RAM."),
        ),
        CheckResult(
            name="Disk space",
            status="fail" if not disks else "warn" if max(d.available_gb for d in disks) < 50 else "pass",
            message=(
                "Could not determine free disk space."
                if not disks
                else f"Largest candidate volume has {max(d.available_gb for d in disks):g} GB free."
            ),
        ),
        CheckResult(
            name="Python",
            status="pass" if python_version else "fail",
            message=(f"Python {python_version}." if python_version else "Python was not found on PATH."),
        ),
        CheckResult(
            name="Download host DNS",
            status="pass" if results["network"].rc == 0 else "warn",
            message=(
                "github.com and huggingface.co resolve."
                if results["network"].rc == 0
                else "Could not resolve one or more model download hosts."
            ),
        ),
    ]
    ssh_mode = results.get("_transport", CommandResult(0, "exec", "")).stdout
    if ssh_mode == "basic_pty":
        checks.append(CheckResult(name="SSH capabilities", status="warn", message="Đã kiểm tra phần cứng qua terminal PTY. Basic SSH không có SCP/SFTP trực tiếp; bộ cài Studio sẽ kiểm tra cầu nối terminal riêng ở bước Chuẩn bị cài."))
    return PreflightReport(
        status=_status(checks),
        ssh_mode=ssh_mode,
        os_name=os_name,
        container_kind=results["container"].stdout.strip() if results["container"].rc == 0 else None,
        is_root=results["identity"].stdout.strip() == "0" if results["identity"].rc == 0 else None,
        gpu=gpu,
        ram_gb=ram_gb,
        disks=disks,
        python_version=python_version,
        checks=checks,
    )


async def collect(executor: Executor) -> CollectedPreflight:
    values: dict[str, CommandResult] = {}
    terminal_probe = getattr(executor, "run_preflight_pty", None)
    if getattr(executor, "host", "").lower().rstrip(".") == "ssh.runpod.io" and terminal_probe:
        values = await terminal_probe(COMMANDS)
        values["_transport"] = CommandResult(0, "basic_pty", "")
        return CollectedPreflight(values)
    for name, command in COMMANDS.items():
        # Transport/auth failures are not equivalent to a missing remote executable.
        try:
            values[name] = await executor.run(command, timeout=20)
        except PTYRequiredError:
            if terminal_probe is None:
                raise
            values = await terminal_probe(COMMANDS)
            values["_transport"] = CommandResult(0, "basic_pty", "")
            break
        if requires_pty(values[name]):
            if terminal_probe is None:
                raise PreflightTransportError("SSH yêu cầu PTY; chưa thực hiện được kiểm tra phần cứng.")
            values = await terminal_probe(COMMANDS)
            values["_transport"] = CommandResult(0, "basic_pty", "")
            break
    return CollectedPreflight(values)
