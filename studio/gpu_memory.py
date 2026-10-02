"""Conservative admission budget for independent ComfyUI processes on one GPU.

This is a scheduling allowance, not a guaranteed peak. A Wan clip (and every other
GPU task) is budgeted at 48 GiB. A keyframe image is budgeted at 30 GiB: Qwen-Image at
720x1280 was measured at 28.1 GiB (28784 MiB) on an RTX PRO 6000, so a large card can
render several images at once while a clip keeps the old one-process-per-GPU flow.
Unknown capacity admits only one job; separate physical GPUs remain independent.
"""
import math

PROCESS_VRAM_GIB = 48
IMAGE_VRAM_GIB = 30
GPU_HEADROOM_GIB = 4


def job_budget(kind):
    return IMAGE_VRAM_GIB if kind == 'keyframe' else PROCESS_VRAM_GIB


def _known(vram_gib):
    return isinstance(vram_gib, (int, float)) and math.isfinite(vram_gib)


def image_process_limit(vram_gib):
    """How many image processes one GPU can render at the same time."""
    if not _known(vram_gib):
        return 1
    return max(1, int((vram_gib - GPU_HEADROOM_GIB) // IMAGE_VRAM_GIB))


def admits(vram_gib, running_kinds, kind):
    """May a job of ``kind`` start next to the jobs of ``running_kinds`` on the same GPU?

    The first job on a GPU always starts (as before); others need their budget to fit in what is left.
    """
    if not running_kinds:
        return True
    if not _known(vram_gib):
        return False
    used = sum(job_budget(k) for k in running_kinds)
    return used + job_budget(kind) <= vram_gib - GPU_HEADROOM_GIB


def process_limit(vram_gib):
    if not _known(vram_gib):
        return 1
    return max(1, int((vram_gib - GPU_HEADROOM_GIB) // PROCESS_VRAM_GIB))


def gpu_key(host_id, lane):
    # Legacy lane metadata predates multiple processes per GPU.
    if lane and lane.get('pod_id'):
        return ('pod', lane['pod_id'], lane.get('gpu', lane['index']))
    return ('host', host_id)
