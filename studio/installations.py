"""Explicit preview -> consent -> durable install -> real-output verification."""
import asyncio
import json
import shlex
import time

import httpx
from sqlalchemy import select

from ghm.executors.http import comfy_client
from ghm.schemas import HostOptions
from studio.install_checks import inventory, summarize
from studio.models import Installation, Job
from studio.packs import PACK_ID, check_model_access, check_remote_model_access, load_graph, resolve_pack
from studio.service import canonical_hash


def identity(host):
    return [host.address, host.port, host.username, host.auth_kind, host.pinned_fingerprint]


class Installations:
    def __init__(self, jobs):
        self.jobs, self.hosts, self.service = jobs, jobs.hosts, jobs.service

    def state(self, host_id):
        self.hosts._require_host(host_id)
        with self.service.sessions() as session:
            row = session.get(Installation, (host_id, PACK_ID))
            result = self.service.read(row) if row else {'host_id': host_id, 'status': 'not_installed'}
            result['jobs'] = [self.service.read(j) for j in session.scalars(
                select(Job).where(Job.host_id == host_id, Job.kind.in_(['install', 'verify']))
                .order_by(Job.created_at.desc()).limit(20))]
        snapshot = result.get('installed_snapshot')
        components = result.get('components', {})
        current_identity = identity(self.hosts._require_host(host_id))
        for name in ('qwen_image', 'qwen_edit', 'wan_i2v', 'tts', 'llm'):
            evidence = components.get(name, {'status': 'not_verified'})
            if evidence.get('status') == 'verified' and (not snapshot or
                    evidence.get('snapshot_hash') != canonical_hash(snapshot) or
                    snapshot.get('identity') != current_identity or
                    snapshot.get('options') != self.hosts.options_for(host_id).model_dump() or
                    snapshot['lock'].get('workflow_hashes') != {n: canonical_hash(load_graph(n)) for n in ('qwen_image', 'qwen_edit', 'wan_i2v')}):
                evidence = {**evidence, 'status': 'stale'}
            components[name] = evidence
        result['components'] = components
        result['all_components_verified'] = all(e['status'] == 'verified' for e in components.values())
        if result.get('status') == 'verified' and not result['all_components_verified']:
            result['status'] = 'verify_failed'
        return result

    def record_component(self, job, name, **evidence):
        state = self.state(job.host_id)
        components = state['components']
        components[name] = {'status': 'verified', 'verified_at': time.time(),
                            'snapshot_hash': canonical_hash(job.snapshot),
                            'identity': job.snapshot['identity'],
                            'lock_hash': canonical_hash(job.snapshot['lock']),
                            'environments': job.snapshot['lock'].get('environments', {}),
                            'job_id': job.id, **evidence}
        self.patch(job.host_id, 'verifying', components=components)

    def component_proven(self, host_id, names):
        """A silent video test proves only the workflows it actually downloaded."""
        if not names:
            return True
        state = self.state(host_id)
        if all(state['components'].get(name, {}).get('status') == 'verified' for name in names):
            return True
        host = self.hosts._require_host(host_id)
        options = self.hosts.options_for(host_id).model_dump()
        with self.service.sessions() as session:
            tests = session.scalars(select(Job).where(Job.host_id == host_id, Job.kind == 'video_test', Job.status == 'completed'))
            for job in tests:
                snapshot = job.snapshot or {}
                if snapshot.get('identity') != identity(host) or snapshot.get('options') != options:
                    continue
                hashes = snapshot.get('lock', {}).get('workflow_hashes', {})
                submissions = (job.result or {}).get('submissions', {})
                if all(submissions.get(name, {}).get('state') == 'downloaded' and hashes.get(name) == canonical_hash(load_graph(name)) for name in names):
                    return True
        snapshot = state.get('installed_snapshot')
        # A completed install has checksum-verified files, including TTS, before output verification.
        if state.get('status') in {'installed', 'verifying', 'verified', 'verify_failed'} and snapshot and snapshot.get('identity') == identity(host) and snapshot.get('options') == options:
            return True
        return False

    def require_transport(self, host_id):
        host = self.hosts._require_host(host_id)
        if not host.pinned_fingerprint:
            raise ValueError('Xác nhận fingerprint ở Kết nối GPU trước khi chuẩn bị cài.')
        report = self.hosts.latest_preflight(host_id)
        if not report or report.status == 'fail' or not report.gpu:
            raise ValueError('Chạy kiểm tra máy thành công trước khi chuẩn bị cài.')
        from studio.installer import MIN_VRAM_GIB
        driver = int(report.gpu.driver_version.split('.')[0])
        if report.gpu.vram_gb < MIN_VRAM_GIB or driver < 570:
            raise ValueError(f'Bộ nền cần GPU 24 GB (nvidia-smi ≥{MIN_VRAM_GIB} GiB) và driver NVIDIA ≥570; máy này có '
                             f'{report.gpu.name} {report.gpu.vram_gb:g} GiB, driver {report.gpu.driver_version}. '
                             'Không tự sửa driver/hạ chất lượng.')
        return host

    async def discover(self, host_id):
        self.jobs.recipes.assert_idle(host_id)
        await self.hosts.preflight(host_id)
        host = self.require_transport(host_id)
        executor = self.hosts.executor_for(host)
        try:
            script = "import json,pathlib; print(json.dumps([p for p in ['/ComfyUI','/workspace/ComfyUI','/workspace/comfyui','/opt/ComfyUI'] if (pathlib.Path(p)/'main.py').is_file()]))"
            result = await executor.run('python3 -c ' + shlex.quote(script), timeout=30)
            if result.rc:
                raise ValueError('Không dò được môi trường ComfyUI.')
            paths = json.loads(result.stdout)
            running = []
            for port in (8188, 8190):
                try:
                    async with comfy_client(executor, port, timeout=5) as client:
                        response = await client.get('/system_stats')
                        response.raise_for_status()
                        version = response.json().get('system', {}).get('comfyui_version')
                        if version:
                            running.append({'port': port, 'version': version})
                except (ConnectionError, ValueError, httpx.HTTPError):
                    pass
            adopt = len(paths) == 1 and len(running) == 1
            options = HostOptions(root='/workspace/historia', adopt_existing=adopt,
                                  comfy_path=paths[0] if adopt else None,
                                  remote_port=running[0]['port'] if adopt else 8190)
            return {'paths': paths, 'services': running, 'suggested_options': options.model_dump(),
                    'transport': 'terminal' if getattr(executor, 'terminal_only', False) else 'ssh',
                    'gpu': self.hosts.latest_preflight(host_id).gpu.model_dump()}
        finally:
            await executor.close()

    async def prepare(self, host_id, options):
        self.jobs.recipes.assert_idle(host_id)
        host = self.require_transport(host_id)
        executor = self.hosts.executor_for(host)
        try:
            # Test stdin+exec before resolving network metadata or touching any model.
            result = await executor.run_input("python3 -c 'import sys; print(sys.stdin.read())'", 'STUDIO_EXEC_OK', timeout=30)
            if result.rc or result.stdout.strip() != 'STUDIO_EXEC_OK':
                raise ValueError('Không thực hiện được lệnh/stdin qua SSH. Kiểm tra kết nối trước khi cài.')
            if hasattr(executor, 'check_install_transport'):
                await executor.check_install_transport()
            try:
                lock = await asyncio.to_thread(resolve_pack, self.hosts.setting('hf_token'))
            except Exception:  # noqa: BLE001 -- do not expose upstream URLs or authentication diagnostics
                raise ValueError('Không khóa được metadata bộ model từ Hugging Face. Kiểm tra kết nối/quyền truy cập nguồn model rồi thử lại; chưa tải hay cài gì.') from None
            await asyncio.to_thread(check_model_access, lock, self.hosts.setting('hf_token'))
            access = await check_remote_model_access(executor, lock, self.hosts.setting('hf_token'))
            summary = summarize(await inventory(executor, options, lock), options)
            summary['remote_access'] = access
        finally:
            await executor.close()
        plan = {'lock': lock, 'options': options.model_dump(), 'identity': identity(host),
                'prepared_at': time.time(), **summary}
        plan['plan_id'] = canonical_hash(plan)
        with self.service.sessions() as session:
            row = session.get(Installation, (host_id, PACK_ID))
            if row is None:
                row = Installation(host_id=host_id, pack_id=PACK_ID)
                session.add(row)
            row.data = {**(row.data or {}), 'plan': plan}
            session.commit()
        return plan

    def start(self, host_id, plan_id, consent):
        if not consent:
            raise ValueError('Cần xác nhận license, dung lượng và cho phép cài trên máy đã chọn.')
        host = self.require_transport(host_id)
        state = self.state(host_id)
        plan = state.get('plan')
        if not plan or plan['plan_id'] != plan_id:
            raise ValueError('Bản xem trước đã thay đổi. Chuẩn bị cài lại để xác nhận đúng bộ model.')
        if time.time() - plan['prepared_at'] > 3600:
            raise ValueError('Bản xem trước quá 1 giờ. Kiểm tra lại dung lượng và cấu hình trước khi cài.')
        if plan['identity'] != identity(host):
            raise ValueError('SSH/fingerprint đã thay đổi; cần chuẩn bị lại.')
        if plan['blockers']:
            raise ValueError(' '.join(plan['blockers']))
        # A repeated click returns the same queued/running/completed installation.
        with self.service.sessions() as session:
            existing = session.scalar(select(Job).where(Job.host_id == host_id, Job.kind == 'install',
                                      Job.input_hash == plan_id, Job.status.in_(['queued', 'running', 'completed'])))
            if existing:
                return self.service.read(existing)
        self.jobs.recipes.assert_idle(host_id)
        options = HostOptions.model_validate(plan['options'])
        self.hosts.save_options(host_id, options)
        with self.service.sessions() as session:
            job = Job(host_id=host_id, kind='install', input_hash=plan_id, snapshot=plan)
            session.add(job)
            row = session.get(Installation, (host_id, PACK_ID))
            row.status = 'queued'
            row.data = {'plan': plan}
            session.commit()
        return self.service.read(job)

    def video_test(self, host_id):
        host = self.require_transport(host_id)
        self.jobs.recipes.assert_idle(host_id)
        state = self.state(host_id)
        plan = state.get('installed_snapshot') or state.get('plan')
        if not plan or plan['identity'] != identity(host):
            raise ValueError('Cần bản cấu hình đã kiểm tra cho GPU này.')
        if plan['options'] != self.hosts.options_for(host_id).model_dump():
            raise ValueError('Cấu hình đã thay đổi; kiểm tra lại trước khi thử clip.')
        with self.service.sessions() as session:
            row = Job(host_id=host_id, kind='video_test', snapshot=plan,
                      input_hash=canonical_hash(plan))
            session.add(row)
            session.commit()
        return self.service.read(row)

    async def execute_video_test(self, job):
        self.validate_snapshot(job)
        log = lambda text: self.jobs.event(job.id, text)
        executor = self.hosts.executor_for(self.hosts._require_host(job.host_id))
        try:
            options = HostOptions.model_validate(job.snapshot['options'])
            lock = {**job.snapshot['lock'], 'snapshots': []}
            lock['models'] = [m for m in lock['models'] if m['name'] not in
                              {'qwen-edit', 'qwen-edit-lightning'}]
            if not lock['models']:
                raise ValueError('Không có danh sách model ảnh/video để kiểm tra.')
            log('Thử clip không tiếng: kiểm tra checksum model ảnh/video, không dùng LLM/TTS.')
            report = await inventory(executor, options, lock)
            if not report['comfy_exists'] or any(f['state'] != 'valid' for f in report['files']):
                raise ValueError('Model ảnh/video thiếu hoặc sai checksum; không chạy thử.')
            # Acquire/check the same remote installer lock without changing the machine.
            from studio.installer import InstallExecutor
            check = await InstallExecutor(executor, options.root).run('true', timeout=15)
            if check.rc:
                raise ValueError('Bộ cài trên GPU còn giữ khóa; chưa chạy thử clip.')
        finally:
            await executor.close()
        image = None
        ids = []
        for name in ('qwen_image', 'wan_i2v'):
            current = self.service.require(Job, job.id)
            path = await self.jobs.backend.generate(current, name,
                'Cinematic ancient stone courtyard, soft morning light, trees gently moving in the wind, slow camera dolly, no text.',
                [image] if image else [], 42, 'draft', log,
                lambda stage, value: self.jobs.checkpoint(job.id, stage, value), name)
            ids.append(self.service.artifact(path, None, name + path.suffix, job.id)['id'])
            self.jobs.patch(job.id, result={**self.service.require(Job, job.id).result,
                                          'artifact_ids': ids, 'silent': True})
            image = path
        log('Đã tạo clip thử không tiếng. Không đánh dấu toàn bộ bộ AI là verified.')

    def verify(self, host_id):
        host = self.require_transport(host_id)
        self.jobs.recipes.assert_idle(host_id)
        state = self.state(host_id)
        if state['status'] not in {'installed', 'verified', 'verify_failed'}:
            raise ValueError('Cài xong bộ model trước khi tạo output kiểm chứng.')
        snapshot = state.get('installed_snapshot')
        if not snapshot or snapshot['identity'] != identity(host) or snapshot['options'] != self.hosts.options_for(host_id).model_dump():
            raise ValueError('Cấu hình máy đã thay đổi; kiểm tra/cài lại trước khi benchmark.')
        if snapshot['lock']['workflow_hashes'] != {n: canonical_hash(load_graph(n)) for n in ('qwen_image', 'qwen_edit', 'wan_i2v')}:
            raise ValueError('Workflow đã đổi; chuẩn bị lại bộ cài trước khi kiểm chứng.')
        with self.service.sessions() as session:
            job = Job(host_id=host_id, kind='verify', input_hash=canonical_hash(snapshot), snapshot=snapshot)
            session.add(job)
            row = session.get(Installation, (host_id, PACK_ID))
            row.status = 'verifying'
            session.commit()
        return self.service.read(job)

    def patch(self, host_id, status, **data):
        with self.service.sessions() as session:
            row = session.get(Installation, (host_id, PACK_ID))
            row.status = status
            row.data = {**row.data, **data}
            session.commit()

    def validate_snapshot(self, job):
        host = self.require_transport(job.host_id)
        if identity(host) != job.snapshot['identity'] or self.hosts.options_for(host.id).model_dump() != job.snapshot['options']:
            raise ValueError('Host hoặc cấu hình đã thay đổi. Không chạy bản cài cũ.')

    async def execute(self, job):
        from studio.installer import install
        self.validate_snapshot(job)
        log = lambda value: self.jobs.event(job.id, value)
        self.patch(job.host_id, 'installing' if job.kind == 'install' else 'verifying')
        if job.kind == 'install':
            log('Bắt đầu cài bộ đã xác nhận. Không sửa driver, không tự dừng ComfyUI có sẵn.')
            result = await install(self.hosts, job, job.snapshot['lock'], log)
            self.jobs.patch(job.id, result=result)
            self.patch(job.host_id, 'installed', installed_snapshot=job.snapshot, install_result=result, components={})
            log('Đã cài. Chưa verified: bấm Kiểm chứng để tạo ảnh, ảnh chỉnh sửa, clip, giọng đọc và kiểm tra LLM.')
            return
        backend = self.jobs.backend
        checkpoint = lambda stage, value: self.jobs.checkpoint(job.id, stage, value)
        started = time.monotonic()
        self.patch(job.host_id, 'verifying', components={name: {'status': 'not_verified'}
                   for name in ('qwen_image', 'qwen_edit', 'wan_i2v', 'tts', 'llm')})
        executor = self.hosts.executor_for(self.hosts._require_host(job.host_id))
        try:
            await check_remote_model_access(executor, job.snapshot['lock'], self.hosts.setting('hf_token'))
            report = await inventory(executor, HostOptions.model_validate(job.snapshot['options']), job.snapshot['lock'])
            if not report['comfy_exists'] or any(f['state'] != 'valid' for f in report['files']):
                raise ValueError('Checksum model chưa hợp lệ; không chạy benchmark.')
        except Exception:
            self.patch(job.host_id, 'verify_failed', components={name: {'status': 'blocked', 'reason': 'access_or_inventory_not_verified'}
                       for name in ('qwen_image', 'qwen_edit', 'wan_i2v', 'tts', 'llm')})
            raise
        finally:
            await executor.close()
        artifacts = []
        image = None
        for index, name in enumerate(('qwen_image', 'qwen_edit', 'wan_i2v')):
            current = self.service.require(Job, job.id)
            path = await backend.generate(current, name,
                'Cinematic landscape, ancient stone courtyard, soft morning light, slow camera movement, no text.',
                [image] if image else [], 42, 'draft', log, checkpoint, name)
            if name == 'wan_i2v':
                from studio.media import probe
                if probe(path)['duration'] <= 0:
                    raise ValueError('Clip kiểm chứng không đọc được.')
            else:
                from PIL import Image
                with Image.open(path) as preview:
                    preview.verify()
            artifact = self.service.artifact(path, None, name + path.suffix, job.id)
            artifacts.append(artifact['id'])
            self.record_component(job, name, artifact_ids=[artifact['id']],
                                  workflow_hash=job.snapshot['lock']['workflow_hashes'][name], quality='draft')
            if name != 'wan_i2v':
                image = path
            self.jobs.patch(job.id, progress=(index+1)*20)
        audio = await backend.speech(job, 'Buổi sáng, ánh nắng nhẹ trải trên dòng sông Bạch Đằng. Trần Hưng Đạo cùng quân dân Đại Việt chuẩn bị bảo vệ quê hương. Đây là đoạn đọc thử tiếng Việt, cần người dùng nghe và duyệt phát âm trước khi nghiệm thu.', 'default', log)
        from studio.remote_worker import inspect_wav
        measured = inspect_wav(audio)
        speech_evidence = json.loads(audio.with_suffix('.segments.json').read_text(encoding='utf-8'))
        if not 10 <= measured['duration'] <= 20:
            raise ValueError('Mẫu TTS phải dài thực 10–20 giây; điều chỉnh lời đọc, không kéo chậm audio.')
        artifacts.append(self.service.artifact(audio, None, 'speech.wav', job.id)['id'])
        artifacts.append(self.service.artifact(audio.with_suffix('.segments.json'), None, 'speech.segments.json', job.id)['id'])
        self.record_component(job, 'tts', artifact_ids=artifacts[-2:], audio=measured,
                              runtime_version=speech_evidence.get('vieneu_version'),
                              device=speech_evidence.get('backbone_device') or speech_evidence.get('device'),
                              elapsed_seconds=speech_evidence.get('elapsed_seconds'), listening_approved=False)
        output = await backend.language(job, 'Trả lời đúng JSON {"ok":true}.', [], log)
        if output.get('ok') is not True:
            raise ValueError('LLM không trả JSON kiểm chứng hợp lệ.')
        self.record_component(job, 'llm', json_probe=output)
        result = {**self.service.require(Job, job.id).result, 'artifact_ids': artifacts,
                  'elapsed_seconds': round(time.monotonic()-started, 2), 'quality': 'draft',
                  'gpu': self.hosts.latest_preflight(job.host_id).gpu.model_dump()}
        self.jobs.patch(job.id, result=result)
        self.patch(job.host_id, 'verified', verification=result)
        log('Đã tải và kiểm tra output thật của toàn bộ bộ AI. Chỉ benchmark bản nháp; chưa nghiệm thu phim dài/720p.')
