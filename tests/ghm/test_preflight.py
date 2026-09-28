from pathlib import Path

from ghm.executors.base import CommandResult
from ghm.preflight import COMMANDS, evaluate, parse_disks, parse_memory_gb, parse_nvidia_smi, parse_os_release


FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_parsers_read_captured_linux_and_nvidia_samples() -> None:
    assert parse_os_release((FIXTURES / "os-release-ubuntu.txt").read_text()) == "Ubuntu 24.04.2 LTS"
    gpu = parse_nvidia_smi((FIXTURES / "nvidia-smi-rtx5090.csv").read_text(), "Cuda compilation tools, release 12.8")
    assert gpu is not None
    assert gpu.name == "NVIDIA GeForce RTX 5090"
    assert gpu.vram_gb == 31.84
    assert gpu.cuda_version == "12.8"
    assert parse_memory_gb((FIXTURES / "free.txt").read_text()) == 64.0
    assert parse_disks((FIXTURES / "df.txt").read_text())[0].available_gb == 400.0


def test_mig_slice_still_counts_as_nvidia() -> None:
    sample = (
        "NVIDIA RTX PRO 6000 Blackwell Server Edition, [Insufficient Permissions], 580.159.04, 12.0\n"
        "GPU 0: NVIDIA RTX PRO 6000 Blackwell Server Edition (UUID: GPU-test)\n"
        "  MIG 2g.48gb     Device  0: (UUID: MIG-test)\n"
    )
    gpu = parse_nvidia_smi(sample, "Cuda compilation tools, release 13.0")
    assert gpu is not None
    assert gpu.name == "NVIDIA RTX PRO 6000 Blackwell Server Edition"
    assert gpu.vram_gb == 48
    assert gpu.driver_version == "580.159.04"
    assert gpu.compute_capability == "12.0"


def test_evaluation_marks_low_vram_and_dns_as_warnings() -> None:
    values = {name: CommandResult(0, "", "") for name in COMMANDS}
    values.update(
        {
            "os": CommandResult(0, 'PRETTY_NAME="Ubuntu 22.04"', ""),
            "identity": CommandResult(0, "1000\n", ""),
            "container": CommandResult(0, "docker\n", ""),
            "gpu": CommandResult(0, "NVIDIA RTX 4060, 8192, 550.90, 8.9\n", ""),
            "cuda": CommandResult(0, "Cuda compilation tools, release 12.4", ""),
            "memory": CommandResult(0, "Mem: 8589934592 0 0 0 0 0", ""),
            "disk": CommandResult(0, "Filesystem 1B-blocks Used Available Use% Mounted on\noverlay 100 10 40 20% /", ""),
            "python": CommandResult(0, "Python 3.11.9", ""),
            "network": CommandResult(1, "", "dns unavailable"),
        }
    )
    report = evaluate(values)
    assert report.status == "warn"
    assert report.gpu is not None and report.gpu.vram_gb == 8.0
    assert report.container_kind == "docker"
    assert {check.name for check in report.checks if check.status == "warn"} == {
        "Memory",
        "Disk space",
        "Download host DNS",
    }
