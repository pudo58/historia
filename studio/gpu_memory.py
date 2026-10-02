"""Conservative admission budget for independent ComfyUI processes on one GPU.

This is a scheduling allowance, not a measured model peak. In particular the old
28 GiB allowance did not leave room for Qwen, its encoders and sampling buffers.
Unknown capacity admits only one job; separate physical GPUs remain independent.
"""
import math

PROCESS_VRAM_GIB = 48
GPU_HEADROOM_GIB = 4


def process_limit(vram_gib):
    if not isinstance(vram_gib, (int, float)) or not math.isfinite(vram_gib):
        return 1
    return max(1, int((vram_gib - GPU_HEADROOM_GIB) // PROCESS_VRAM_GIB))


def gpu_key(host_id, lane):
    # Legacy lane metadata predates multiple processes per GPU.
    if lane and lane.get('pod_id'):
        return ('pod', lane['pod_id'], lane.get('gpu', lane['index']))
    return ('host', host_id)
