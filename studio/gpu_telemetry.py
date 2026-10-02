"""Read-only, shared physical-GPU sampling. Missing measurements stay missing."""
import asyncio
import json
import math
import shlex

from studio.gpu_memory import gpu_key

# No installs, files, signals, or CUDA context. PID memory is driver-reported, not
# torch reserved memory. Unsupported driver fields remain null, never zero.
PROBE = r'''
import csv, io, json, subprocess, time
def rows(args):
    p = subprocess.run(['nvidia-smi', *args, '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)
    if p.returncode: raise RuntimeError('nvidia-smi unavailable')
    return list(csv.reader(io.StringIO(p.stdout)))
def number(s):
    try: return float(s.strip())
    except ValueError: return None
gpus = []
for r in rows(['--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu']):
    if len(r) == 6:
        gpus.append(dict(index=int(r[0]), uuid=r[1].strip(), name=r[2].strip(),
                         total_mib=number(r[3]), used_mib=number(r[4]), utilization=number(r[5])))
processes = []
for r in rows(['--query-compute-apps=gpu_uuid,pid,used_gpu_memory']):
    if len(r) == 3:
        processes.append(dict(uuid=r[0].strip(), pid=int(r[1]), used_mib=number(r[2])))
mem = {}
with open('/proc/meminfo') as f:
    for line in f:
        k, v = line.split(':', 1)
        if k in ('MemTotal', 'MemAvailable'): mem[k] = int(v.split()[0]) * 1024
print(json.dumps(dict(timestamp=time.time(), gpus=gpus, processes=processes,
    host_ram_used_bytes=mem.get('MemTotal', 0)-mem.get('MemAvailable', 0), host_ram_total_bytes=mem.get('MemTotal'))))
'''


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


class Window:
    def __init__(self, pid, prompt_id):
        self.pid, self.prompt_id = pid, prompt_id
        self.samples = 0
        self.summary = {'sample_interval_seconds': 2, 'process_peak_vram_bytes': None,
                        'gpu_peak_used_bytes': None, 'host_peak_ram_bytes': None,
                        'gpu_utilization_mean': None, 'gpu_utilization_peak': None,
                        'model_load_seconds': None,
                        'model_load_note': 'Comfy history does not expose model loading separately',
                        'peak_kind': 'sampled_driver_memory', 'active_prompt_ids': []}
        self.utilization_sum = self.utilization_samples = 0

    def add(self, sample, gpu_index, active_ids):
        gpu = next((g for g in sample.get('gpus', []) if g.get('index') == gpu_index), None)
        if not gpu or not gpu.get('uuid') or not finite(gpu.get('total_mib')):
            return
        if self.summary.get('gpu_uuid') not in (None, gpu['uuid']):
            self.summary['identity_changed'] = True
            return
        self.samples += 1
        self.summary.update(gpu_uuid=gpu['uuid'], gpu_name=gpu.get('name'),
                            gpu_total_bytes=int(gpu['total_mib'] * 1024**2),
                            last_sample_at=sample.get('timestamp'), samples=self.samples)
        def peak(key, value):
            if finite(value):
                self.summary[key] = max(self.summary.get(key) or 0, value)
        peak('gpu_peak_used_bytes', gpu['used_mib'] * 1024**2 if finite(gpu.get('used_mib')) else None)
        peak('host_peak_ram_bytes', sample.get('host_ram_used_bytes'))
        processes = [p['used_mib'] for p in sample.get('processes', [])
                     if p.get('uuid') == gpu['uuid'] and p.get('pid') == self.pid and finite(p.get('used_mib'))]
        if processes:
            peak('process_peak_vram_bytes', sum(processes) * 1024**2)
            self.summary['process_memory_samples'] = self.summary.get('process_memory_samples', 0) + 1
        if finite(gpu.get('utilization')):
            self.utilization_sum += gpu['utilization']
            self.utilization_samples += 1
            self.summary['gpu_utilization_mean'] = self.utilization_sum / self.utilization_samples
            peak('gpu_utilization_peak', gpu['utilization'])
        self.summary['active_prompt_ids'] = sorted(set(self.summary['active_prompt_ids']) | set(active_ids))


class TelemetryPool:
    """One sampler connection per physical GPU, shared by both Comfy lanes."""
    def __init__(self, backend):
        self.backend = backend
        self.monitors = {}

    async def snapshot(self, executor):
        result = await executor.run_input('python3 -c ' + shlex.quote(PROBE), '', timeout=15)
        if result.rc:
            raise ValueError('GPU telemetry unavailable')
        return json.loads(result.stdout)

    async def watch(self, job, generation, prompt_id):
        if not self.backend.hosts or not generation or ':' not in generation:
            return None
        lane = json.loads(self.backend.hosts.setting('host_lane:' + job.host_id) or '{}')
        key = gpu_key(job.host_id, lane)
        window = Window(int(generation.split(':')[0]), prompt_id)
        index = lane.get('gpu', lane.get('index', 0)) if lane else (self.backend.hosts.options_for(job.host_id).gpu_index or 0)
        monitor = self.monitors.get(key)
        if monitor is None:
            monitor = {'windows': {}, 'index': index, 'last': None}
            self.monitors[key] = monitor
            monitor['task'] = asyncio.create_task(self._sample(job.host_id, monitor))
        monitor['windows'][prompt_id] = window
        if monitor['last']:
            window.add(monitor['last'], index, monitor['windows'])
        return key, window

    async def _sample(self, host_id, monitor):
        try:
            async with self.backend.connection(host_id) as (executor, _):
                while True:
                    sample = await self.snapshot(executor)
                    monitor['last'] = sample
                    for window in list(monitor['windows'].values()):
                        window.add(sample, monitor['index'], monitor['windows'])
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- telemetry never changes a paid render's outcome
            for window in monitor['windows'].values():
                window.summary['unavailable'] = True

    async def unwatch(self, token):
        if token is None:
            return {}
        key, window = token
        monitor = self.monitors[key]
        monitor['windows'].pop(window.prompt_id, None)
        if not monitor['windows']:
            monitor['task'].cancel()
            await asyncio.gather(monitor['task'], return_exceptions=True)
            self.monitors.pop(key, None)
        return dict(window.summary)

    async def close(self):
        tasks = [m['task'] for m in self.monitors.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.monitors.clear()
