"""Optional, durable runtime maintenance. Never mutate an adopted Comfy install."""
import asyncio
import json
import shlex
from pathlib import Path

import httpx
from sqlalchemy import select, text

from ghm.remote_service import manage
from studio.installations import identity
from studio.installer import private_python
from studio.models import Job
from studio.schemas import RuntimeConfig
from studio.service import canonical_hash

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / 'manifests/studio-runtime.json').read_text())
PROBE = r'''
import importlib.metadata, json, sys
import torch
c = json.load(sys.stdin)
assert sys.version_info.major == 3 and sys.version_info.minor in c['python_minors'], 'Python 3.11/3.12 required'
assert torch.cuda.is_available() and torch.version.cuda, 'CUDA required'
if c['sage']:
    import sageattention
    assert importlib.metadata.version('sageattention') == c['version'], 'SageAttention version mismatch'
    assert callable(sageattention.sageattn), 'SageAttention API unavailable'
print(json.dumps({'python': sys.version.split()[0], 'torch': torch.__version__, 'cuda': torch.version.cuda,
                  'gpu': torch.cuda.get_device_name(), 'vram_bytes': torch.cuda.get_device_properties(0).total_memory,
                  'sageattention': importlib.metadata.version('sageattention') if c['sage'] else None}))
'''


def runtime_flags(config):
    config = RuntimeConfig.model_validate(config)
    return (['--use-sage-attention'] if config.attention_backend == 'sage' else []) + (
        ['--highvram'] if config.memory_policy == 'highvram' else [])


def merged_flags(argv, config):
    # Only replace the two explicitly selected option groups; preserve other tuning.
    options = {'--use-sage-attention', '--use-pytorch-cross-attention', '--use-flash-attention',
               '--use-split-cross-attention', '--use-quad-cross-attention', '--use-ck-attention',
               '--highvram', '--lowvram', '--novram', '--gpu-only', '--cpu'}
    args = list(argv[2:])
    for flag in ('--listen', '--port'):
        if flag in args:
            index = args.index(flag)
            del args[index:index+2]
    return [a for a in args if a not in options] + runtime_flags(config)


class RuntimeManager:
    def __init__(self, jobs):
        self.jobs, self.hosts, self.service = jobs, jobs.hosts, jobs.service

    def state(self, host_id):
        options = self.hosts.options_for(host_id)
        settings = json.loads(self.hosts.setting('studio_runtime:' + host_id) or '{}')
        with self.service.sessions() as session:
            jobs = [self.service.read(j) for j in session.scalars(select(Job).where(
                Job.host_id == host_id, Job.kind == 'runtime').order_by(Job.created_at.desc()).limit(10))]
        return {'config': settings.get('config', RuntimeConfig().model_dump()),
                'evidence': settings.get('evidence'), 'adopt_existing': options.adopt_existing,
                'manifest': MANIFEST, 'jobs': jobs,
                'instructions': f"SageAttention=={MANIFEST['sageattention']}; --use-sage-attention; --highvram chỉ sau benchmark. "
                                'Comfy adopt: chủ dịch vụ tự cài/restart khi queue rỗng, rồi kiểm tra lại.'}

    def at_boundary(self, host_id, exclude_id=None, recover=False):
        from studio.cost_policy import remote_settled
        from studio.jobs import ACTIVE
        with self.service.sessions() as session:
            for job in session.scalars(select(Job).where(Job.host_id == host_id)):
                if job.id == exclude_id or job.status == 'completed':
                    continue
                if recover and job.kind == 'runtime' and job.status in {'failed', 'interrupted', 'reconciling', 'cancelled'}:
                    continue
                if job.status in ACTIVE and job.status != 'paused':
                    return False
                if (job.result.get('maintenance_pending') or job.result.get('speech_pending') or
                        job.result.get('outline_pending') or job.result.get('chapter_pending') is not None or
                        any(not remote_settled(v) for v in job.result.get('submissions', {}).values())):
                    return False
        return True

    def start(self, host_id, config, action='apply'):
        from studio.cost_policy import remote_settled
        from studio.jobs import ACTIVE
        options = self.hosts.options_for(host_id)
        if options.adopt_existing:
            raise ValueError('ComfyUI được adopt: chỉ cung cấp hướng dẫn và kiểm tra, không tự cài/restart.')
        config = RuntimeConfig.model_validate(config).model_dump()
        snapshot = {'action': action, 'config': config, 'options': options.model_dump(),
                    'identity': identity(self.hosts._require_host(host_id)), 'manifest': MANIFEST}
        with self.service.sessions() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            for job in session.scalars(select(Job).where(Job.host_id == host_id)):
                if action == 'recover' and job.kind == 'runtime' and job.status in {'failed', 'interrupted', 'reconciling', 'cancelled'}:
                    continue
                unresolved = job.status != 'completed' and (job.result.get('speech_pending') or
                    job.result.get('maintenance_pending') or
                    job.result.get('outline_pending') or job.result.get('chapter_pending') is not None or
                    any(not remote_settled(v) for v in job.result.get('submissions', {}).values()))
                if (job.status in ACTIVE and job.status != 'paused') or unresolved:
                    raise ValueError('Tạm dừng ở ranh giới shot và đối chiếu GPU trước khi đổi runtime.')
            self.jobs.recipes.assert_recipes_idle(host_id)
            job = Job(host_id=host_id, kind='runtime', snapshot=snapshot, input_hash=canonical_hash(snapshot))
            session.add(job)
            session.commit()
            return self.service.read(job)

    async def inspect(self, host_id):
        async with self.jobs.backend.connection(host_id) as (_, client):
            actual = await self.jobs.backend.runtime_metadata(client)
        return {**self.state(host_id), 'actual': actual}

    async def execute(self, job):
        config = job.snapshot['config']
        manifest = job.snapshot['manifest']
        options = self.hosts.options_for(job.host_id)
        if (options.adopt_existing or options.model_dump() != job.snapshot['options'] or
                identity(self.hosts._require_host(job.host_id)) != job.snapshot['identity']):
            raise ValueError('Host/cấu hình đã thay đổi; không thực thi runtime cũ.')
        if job.result.get('maintenance_pending'):
            raise ValueError('Lần bảo trì trước bị gián đoạn. Kiểm tra tiến trình/môi trường trước khi tạo lượt mới; không tự cài lại.')
        self.jobs.recipes.assert_recipes_idle(job.host_id)
        if not self.at_boundary(job.host_id, job.id, recover=job.snapshot['action'] == 'recover'):
            raise ValueError('Host đã có công việc mới; không đổi runtime.')
        async with self.jobs.backend.connection(job.host_id) as (executor, client):
            lock = shlex.quote(options.root + '/.studio-runtime.lock')
            held = await executor.run('flock -n -E 73 ' + lock + ' true', timeout=20)
            if held.rc:
                raise ValueError('Bảo trì remote còn giữ khóa hoặc chưa kiểm tra được khóa; không restart/cài lại.')
            stopped = False
            try:
                queue = await client.get('/queue')
                queue.raise_for_status()
                if any(queue.json().get(k) for k in ('queue_running', 'queue_pending')):
                    raise ValueError('Comfy queue chưa rỗng; không đổi runtime.')
            except (httpx.TransportError, ConnectionError):
                if job.snapshot['action'] != 'recover':
                    raise
                owned = json.loads(await manage(executor, options, 'inspect-stopped', []))
                stopped = True
            # Ownership inspection must succeed before even installing a package.
            if not stopped:
                owned = json.loads(await manage(executor, options, 'inspect', []))
            python = options.root + '/venv/bin/python'
            args = {'python_minors': manifest['python_minors'], 'version': manifest['sageattention'],
                    'sage': config['attention_backend'] == 'sage' and job.snapshot['action'] != 'install-sage'}
            result = await executor.run_input(shlex.quote(python) + ' -c ' + shlex.quote(PROBE), json.dumps(args), timeout=90)
            if result.rc:
                raise ValueError('Runtime không tương thích Python 3.11/3.12, Torch/CUDA hoặc SageAttention đã ghim. Chưa dừng ComfyUI.')
            evidence = json.loads(result.stdout)
            if not stopped:
                queue = await client.get('/queue')
                queue.raise_for_status()
                if any(queue.json().get(k) for k in ('queue_running', 'queue_pending')):
                    raise ValueError('Queue có prompt mới sau kiểm tra dependency; không dừng ComfyUI.')
            self.jobs.patch(job.id, result={**job.result, 'maintenance_pending': True, 'probe': evidence})
            await manage(executor, options, 'stop', [])
            if job.snapshot['action'] == 'install-sage':
                command = private_python(python) + ' -m pip install --no-deps --no-build-isolation '
                command += 'sageattention==' + manifest['sageattention'] + ' --index-url https://pypi.org/simple'
                result = await executor.run('flock -n -E 73 ' + lock + ' sh -c ' + shlex.quote(command), timeout=1800)
                if result.rc:
                    raise ValueError('Cài SageAttention thất bại; ComfyUI đang dừng. Không tự đổi Torch/CUDA hoặc fallback.')
                args['sage'] = True
                result = await executor.run_input(shlex.quote(python) + ' -c ' + shlex.quote(PROBE), json.dumps(args), timeout=90)
                if result.rc:
                    raise ValueError('SageAttention chưa import được; giữ ComfyUI dừng để kiểm tra.')
                evidence = json.loads(result.stdout)
            await manage(executor, options, 'start', merged_flags(owned['argv'], config))
            actual = None
            for _ in range(30):
                try:
                    actual = await self.jobs.backend.runtime_metadata(client)
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(1)
            if actual is None or any(actual.get(k) != config[k] for k in ('attention_backend', 'memory_policy')):
                raise ValueError('Chưa xác nhận ComfyUI chạy đúng runtime đã chọn; giữ trạng thái cần kiểm tra, không fallback.')
            evidence['actual'] = actual
            self.hosts.save_setting('studio_runtime:' + job.host_id, json.dumps({'config': config, 'evidence': evidence}))
            self.jobs.patch(job.id, result={**self.service.require(Job, job.id).result,
                                           'maintenance_pending': False, 'config': config, 'evidence': evidence})
            if job.snapshot['action'] == 'recover':
                with self.service.sessions() as session:
                    for old in session.scalars(select(Job).where(Job.host_id == job.host_id,
                            Job.kind == 'runtime', Job.id != job.id,
                            Job.status.in_(['failed', 'interrupted', 'reconciling', 'cancelled']))):
                        old.result = {**old.result, 'maintenance_pending': False, 'recovered_by': job.id}
                        old.status = 'failed'
                    session.commit()
