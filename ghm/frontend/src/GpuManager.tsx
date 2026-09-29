import React, { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { DeleteControl } from './StudioControls';

type Host = {
  id: string; label: string; address: string; port: number; username: string;
  auth_kind: string; state: string; pinned_fingerprint?: string; local_url?: string;
  tunnel_status?: string; gpu?: { name: string; vram_gb: number };
};
type Check = { name: string; status: 'pass' | 'warn' | 'fail'; message: string };
type Report = { status: 'pass' | 'warn' | 'fail'; os_name?: string; container_kind?: string; is_root?: boolean; gpu?: { name: string; vram_gb: number }; ram_gb?: number; disks: { mount: string; available_gb: number }[]; python_version?: string; checks: Check[]; created_at: string };
type Recipe = { name: string; description: string };
type Step = { step_id: string; status: string; attempts: number; message?: string };
type Run = { id: string; host_id: string; recipe_name: string; status: string; cancel_requested: boolean; created_at: string; steps: Step[] };
type Log = { id: number; step_id?: string; level: string; message: string };

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, init);
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Request failed' }));
    throw new Error(error.detail || 'Request failed');
  }
  return response.status === 204 ? undefined as T : response.json();
};
const button = 'rounded-lg border px-3 py-2 text-sm font-semibold disabled:opacity-40';
const checkNames: Record<string, string> = {
  'Operating system': 'Hệ điều hành', 'NVIDIA GPU': 'GPU NVIDIA', Memory: 'Bộ nhớ RAM',
  'Disk space': 'Dung lượng ổ đĩa', Python: 'Python', 'Download host DNS': 'DNS máy tải model',
  'SSH capabilities': 'Khả năng SSH', 'SSH command execution': 'Chạy lệnh qua SSH',
};
const checkLabels = { pass: 'Đạt', warn: 'Cần lưu ý', fail: 'Không đạt' };
const knownMessages: Record<string, string> = {
  'Could not read /etc/os-release.': 'Không đọc được thông tin hệ điều hành từ /etc/os-release.',
  'Could not determine host memory.': 'Không xác định được dung lượng RAM của máy.',
  'Could not determine free disk space.': 'Không xác định được dung lượng ổ đĩa còn trống.',
  'Python was not found on PATH.': 'Không tìm thấy Python trong PATH.',
  'github.com and huggingface.co resolve.': 'Có thể phân giải DNS cho github.com và huggingface.co.',
  'Could not resolve one or more model download hosts.': 'Không phân giải được DNS của một hoặc nhiều máy tải model.',
};
function checkMessage(message: string) {
  if (knownMessages[message]) return knownMessages[message];
  if (/^\d+(?:\.\d+)? GB total RAM\.$/.test(message)) return message.replace(' GB total RAM.', ' GB RAM tổng cộng.');
  if (/^Largest candidate volume has \d+(?:\.\d+)? GB free\.$/.test(message)) return message.replace('Largest candidate volume has ', 'Ổ phù hợp nhất còn ').replace(' GB free.', ' GB trống.');
  if (/^Python [\d.]+\.$/.test(message)) return message;
  if (/^.+, \d+(?:\.\d+)? GB VRAM\.$/.test(message)) return message;
  if (!/[A-Za-z]/.test(message) || /[À-ỹ]/.test(message)) return message;
  return null;
}
function StatusChip({ status, label }: { status: string; label: string }) {
  const tone = ['pass', 'ready', 'running', 'completed', 'verified', 'succeeded'].includes(status) ? 'status-ready'
    : ['fail', 'failed', 'degraded', 'unreachable', 'cancelled', 'interrupted'].includes(status) ? 'status-failed' : 'status-queued';
  const symbol = tone === 'status-ready' ? '✓' : tone === 'status-failed' ? '×' : '·';
  return <span className={`status-chip ${tone}`} title={status}><span aria-hidden="true">{symbol}</span>{label}</span>;
}
function ErrorDetail({ message }: { message: string }) {
  const known: Record<string, string> = { 'Request failed': 'Yêu cầu không thành công.', 'Could not reach this host to inspect its SSH key.': 'Không kết nối được máy để đọc khóa SSH.' };
  const friendly = known[message] || (/[À-ỹ]/.test(message) ? message : null);
  return <div role="alert" className="mt-3 text-sm text-rose-300">{friendly || 'Không thể hoàn tất thao tác. Xem thông tin kỹ thuật để biết nguyên nhân.'}{friendly !== message && <details><summary>Thông tin kỹ thuật gốc</summary><span className="break-all">{message}</span></details>}</div>;
}
const hostLabels: Record<string, string> = { needs_setup: 'Cần thiết lập', ready: 'Sẵn sàng', degraded: 'Kiểm tra không đạt', unreachable: 'Không kết nối được' };
const runLabels: Record<string, string> = { queued: 'Đang chờ', running: 'Đang chạy', completed: 'Hoàn tất', failed: 'Thất bại', cancelled: 'Đã hủy', cancelling: 'Đang yêu cầu dừng', pending: 'Chưa bắt đầu', skipped: 'Đã bỏ qua', succeeded: 'Hoàn tất' };

function Preflight({ report }: { report: Report }) {
  if (report.container_kind?.toLowerCase().includes('support pty')) {
    return <div role="status" className="mt-4 w-full rounded-lg border border-amber-500 p-4 text-sm text-amber-200">Kết quả cũ không hợp lệ do SSH yêu cầu PTY. Chưa kiểm tra được GPU/DNS; hãy bấm Kiểm tra máy để kiểm tra bằng bản sửa mới.</div>;
  }
  return <div className="mt-4 w-full rounded-lg border border-slate-700 bg-slate-900 p-4 text-sm">
    <div className="flex items-center justify-between gap-3"><h4 className="font-semibold">Kết quả kiểm tra máy</h4><StatusChip status={report.status} label={checkLabels[report.status]} /></div>
    <div className="mt-3 grid gap-2 text-slate-300 sm:grid-cols-2"><p>Hệ điều hành: {report.os_name || 'Chưa xác định'}</p><p>Môi trường: {report.container_kind === 'vm-or-bare-metal' ? 'Máy ảo hoặc máy vật lý' : report.container_kind || 'Chưa xác định'} {report.is_root ? '(quyền root)' : ''}</p><p>GPU: {report.gpu ? `${report.gpu.name} · ${report.gpu.vram_gb} GB` : 'Chưa kiểm tra được'}</p><p>RAM: {report.ram_gb != null ? `${report.ram_gb} GB` : 'Chưa xác định'}</p><p>Python: {report.python_version || 'Chưa xác định'}</p><p>Ổ đĩa: {report.disks[0] ? `Còn ${report.disks[0].available_gb} GB tại ${report.disks[0].mount}` : 'Chưa xác định'}</p></div>
    <div className="mt-3 space-y-2 border-t border-slate-700 pt-3">{report.checks.map(check => { const translated = checkMessage(check.message); return <div key={check.name} className="flex flex-wrap items-start gap-2"><StatusChip status={check.status} label={checkLabels[check.status]} /><span><strong>{checkNames[check.name] || check.name}:</strong> {translated || 'Xem thông tin kỹ thuật bên dưới.'}{!translated && <details><summary>Thông tin kỹ thuật gốc</summary><span className="break-all">{check.message}</span></details>}</span></div>; })}</div>
  </div>;
}

function SavedPreflight({hostId, latest}: {hostId: string; latest?: Report}) {
  const query = useQuery({queryKey: ['preflight', hostId], queryFn: () => api<Report | null>(`/api/hosts/${hostId}/preflight`)});
  const report = latest || query.data;
  if (query.error) return <p role="alert">Không tải được kết quả preflight đã lưu.</p>;
  return report ? <Preflight report={report}/> : null;
}

function RunView({ run, onUpdate, onCancel, onResume, onSkip }: { run: Run; onUpdate: (run: Run) => void; onCancel: () => void; onResume: () => void; onSkip: (step: string) => void }) {
  const [logs, setLogs] = useState<Log[]>([]);
  const latest = useRef({run, onUpdate});
  useEffect(() => { latest.current = {run, onUpdate}; }, [run, onUpdate]);
  useEffect(() => {
    const source = new EventSource(`/api/runs/${run.id}/events`);
    source.addEventListener('log', event => { const log = JSON.parse((event as MessageEvent<string>).data) as Log; setLogs(current => current.some(item => item.id === log.id) ? current : [...current, log]); });
    source.addEventListener('state', event => { const state = JSON.parse((event as MessageEvent<string>).data) as Pick<Run, 'status' | 'steps'>; latest.current.onUpdate({ ...latest.current.run, ...state }); if (['completed', 'failed', 'cancelled'].includes(state.status)) source.close(); });
    // Reconnect with Last-Event-ID after transient failures.
    return () => source.close();
  }, [run.id, run.status]);
  const done = ['completed', 'failed', 'cancelled'].includes(run.status);
  return <div className="mt-4 w-full rounded-lg border border-indigo-400/50 bg-slate-900 p-4 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-2"><div><h4 className="font-semibold">Quy trình kỹ thuật: {run.recipe_name}</h4><StatusChip status={run.status} label={runLabels[run.status] || run.status} /></div><div className="flex gap-2">{!done && <button onClick={onCancel} className={`${button} border-rose-400 text-rose-300`}>Yêu cầu dừng</button>}{done && run.status !== 'completed' && <button onClick={onResume} className={`${button} border-sky-400 text-sky-300`}>Tiếp tục</button>}</div></div>
    <ol className="mt-3 space-y-1">{run.steps.map(step => <li key={step.step_id} className="flex flex-wrap gap-x-2"><StatusChip status={step.status} label={runLabels[step.status] || step.status} /><span>{step.step_id} {step.attempts ? `· lần thử ${step.attempts}` : ''}</span>{run.status === 'failed' && step.status === 'failed' && <button onClick={() => onSkip(step.step_id)} className="text-xs text-amber-300 underline">Bỏ qua & tiếp tục</button>}</li>)}</ol>
    <pre className="mt-3 max-h-40 overflow-auto rounded bg-slate-950 p-3 text-xs text-slate-300">{logs.length ? logs.map(log => `[${log.level}] ${log.message}`).join('\n') : 'Đang chờ nhật ký quy trình…'}</pre>
  </div>;
}

export default function GpuManager() {
  const client = useQueryClient();
  const [error, setError] = useState('');
  const [reports, setReports] = useState<Record<string, Report>>({});
  const [runs, setRuns] = useState<Record<string, Run>>({});
  const [recipeName, setRecipeName] = useState('ssh-check');
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: () => api<Host[]>('/api/hosts') });
  const recipes = useQuery({ queryKey: ['recipes'], queryFn: () => api<Recipe[]>('/api/recipes') });
  const add = useMutation({ mutationFn: (body: object) => api<Host>('/api/hosts', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }), onSuccess: () => client.invalidateQueries({ queryKey: ['hosts'] }), onError: (e: Error) => setError(e.message) });
  const inspect = useMutation({ mutationFn: (id: string) => api<{ fingerprint: string }>(`/api/hosts/${id}/inspect-key`, { method: 'POST' }), onSuccess: (result, id) => { if (confirm(`SSH fingerprint:\n${result.fingerprint}\n\nĐã đối chiếu fingerprint với nhà cung cấp? Chỉ xác nhận nếu khóa khớp. Bạn có tin cậy và lưu khóa này?`)) api<Host>(`/api/hosts/${id}/confirm-key`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ fingerprint: result.fingerprint }) }).then(() => client.invalidateQueries({ queryKey: ['hosts'] })).catch((e: Error) => setError(e.message)); }, onError: (e: Error) => setError(e.message) });
  const preflight = useMutation({ mutationFn: (id: string) => api<Report>(`/api/hosts/${id}/preflight`, { method: 'POST' }), onSuccess: (report, id) => { setReports(current => ({ ...current, [id]: report })); client.invalidateQueries({ queryKey: ['hosts'] }); }, onError: (e: Error) => setError(e.message) });
  const startRun = useMutation({ mutationFn: (hostId: string) => api<Run>(`/api/hosts/${hostId}/runs`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ recipe_name: recipeName }) }), onSuccess: (run, hostId) => setRuns(current => ({ ...current, [hostId]: run })), onError: (e: Error) => setError(e.message) });
  const tunnel = useMutation({ mutationFn: (hostId: string) => api<Host>(`/api/hosts/${hostId}/tunnel`, { method: 'POST' }), onSuccess: () => client.invalidateQueries({ queryKey: ['hosts'] }), onError: (e: Error) => setError(e.message) });
  const stopTunnel = useMutation({ mutationFn: (hostId: string) => api<void>(`/api/hosts/${hostId}/tunnel`, { method: 'DELETE' }), onSuccess: () => client.invalidateQueries({ queryKey: ['hosts'] }), onError: (e: Error) => setError(e.message) });
  const updateRun = (hostId: string, run: Run) => setRuns(current => ({ ...current, [hostId]: run }));
  const runAction = async (hostId: string, path: string) => { try { updateRun(hostId, await api<Run>(path, { method: 'POST' })); } catch (e) { setError((e as Error).message); } };
  const submit = (event: React.FormEvent<HTMLFormElement>) => { event.preventDefault(); setError(''); const element=event.currentTarget; const form = new FormData(element); add.mutate({ label: form.get('label'), address: form.get('address'), port: Number(form.get('port')), username: form.get('username'), auth_kind: form.get('auth_kind'), secret: form.get('secret') }, {onSuccess:()=>element.reset()}); };

  return <div className="gpu-manager min-h-screen bg-slate-950 p-6 text-slate-100"><div className="mx-auto max-w-6xl"><div className="grid gap-6 lg:grid-cols-[400px_1fr]"><form onSubmit={submit} className="rounded-2xl border border-slate-700 bg-slate-900 p-5 shadow-xl"><h2 className="text-lg font-semibold">Thêm kết nối SSH</h2>{[['label', 'Tên máy dễ nhớ', 'GPU dựng phim'], ['address', 'Địa chỉ máy / IP', 'gpu.example.com'], ['port', 'Cổng SSH', '22'], ['username', 'Tên đăng nhập', 'ubuntu'], ['secret', 'Mật khẩu hoặc đường dẫn khóa riêng', '']].map(([name, label, placeholder]) => <label key={name} className="mt-4 block text-sm text-slate-300">{label}<input name={name} required type={name === 'secret' ? 'password' : name === 'port' ? 'number' : 'text'} placeholder={placeholder} defaultValue={name === 'port' ? '22' : ''} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 p-2.5" /></label>)}<label className="mt-4 block text-sm">Phương thức xác thực<select name="auth_kind" className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 p-2.5"><option value="password">Mật khẩu</option><option value="private_key">Đường dẫn khóa riêng</option></select></label><button disabled={add.isPending} className="mt-5 w-full rounded-lg bg-emerald-400 px-4 py-2.5 font-bold text-slate-950 disabled:opacity-50">Lưu kết nối GPU</button>{error && <ErrorDetail message={error}/>}</form><section className="rounded-2xl border border-slate-700 bg-slate-900 p-5"><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-semibold">Máy GPU đã lưu</h2><label className="text-sm text-slate-300">Quy trình kỹ thuật <select value={recipeName} onChange={e => setRecipeName(e.target.value)} className="ml-2 rounded border border-slate-700 bg-slate-950 p-2">{recipes.data?.map(recipe => <option key={recipe.name} value={recipe.name}>{recipe.name}</option>)}</select></label></div><div className="mt-4 space-y-3">{hosts.isLoading && <p className="text-slate-400">Đang tải…</p>}{hosts.data?.length === 0 && <p className="text-slate-400">Chưa có GPU. Thêm kết nối SSH bằng biểu mẫu bên cạnh.</p>}{hosts.data?.map(host => <article key={host.id} className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-700 bg-slate-950 p-4"><div><h3 className="font-semibold">{host.label}</h3><p className="text-sm text-slate-400">{host.username}@{host.address}:{host.port} · {host.auth_kind}</p><div className="mt-2 flex flex-wrap gap-2"><StatusChip status={host.state} label={hostLabels[host.state] || host.state} />{host.tunnel_status === 'running' && <StatusChip status="running" label="Đường hầm SSH đang mở" />}</div></div><div className="flex flex-wrap gap-2"><button onClick={() => inspect.mutate(host.id)} disabled={inspect.isPending || Boolean(host.pinned_fingerprint)} className={`${button} border-emerald-400 text-emerald-300`}>{host.pinned_fingerprint ? 'Đã tin cậy khóa' : 'Đọc fingerprint SSH'}</button><button onClick={() => preflight.mutate(host.id)} title={!host.pinned_fingerprint?'Đọc và xác nhận fingerprint trước':'Kiểm tra GPU, RAM, ổ đĩa và SSH'} disabled={preflight.isPending || !host.pinned_fingerprint} className={`${button} border-sky-400 text-sky-300`}>Kiểm tra máy</button><button onClick={() => startRun.mutate(host.id)} title={!host.pinned_fingerprint?'Đọc và xác nhận fingerprint trước':'Chạy quy trình kỹ thuật đã chọn, không thay thế bộ cài AI Studio'} disabled={startRun.isPending || !host.pinned_fingerprint} className={`${button} border-indigo-400 text-indigo-300`}>Chạy quy trình kỹ thuật</button><button onClick={() => tunnel.mutate(host.id)} title={host.local_url?'Tunnel đã mở':host.state!=='ready'?'Chạy kiểm tra máy thành công trước':'Mở kết nối localhost qua SSH'} disabled={tunnel.isPending || host.state !== 'ready' || Boolean(host.local_url)} className={`${button} border-fuchsia-400 text-fuchsia-300`}>Mở tunnel bảo mật</button>{host.local_url && <><button onClick={() => window.open(host.local_url, '_blank')} className={`${button} border-emerald-400 text-emerald-300`}>Mở ComfyUI</button><button onClick={() => stopTunnel.mutate(host.id)} className={`${button} border-rose-400 text-rose-300`}>Đóng tunnel</button></>}</div><div className="gpu-safety-help"><p>{!host.pinned_fingerprint?'Bước tiếp theo: đọc và đối chiếu fingerprint với nhà cung cấp trước khi tin cậy máy.':host.state!=='ready'?'Chạy kiểm tra máy để xác nhận GPU và SSH trước khi mở tunnel.':'Máy đã sẵn sàng. Có thể cài bộ AI hoặc mở tunnel khi cần.'}</p><DeleteControl name={host.label} description="Chỉ gỡ kết nối GPU và thông tin đã lưu trên máy local. KHÔNG xóa Pod, KHÔNG dừng GPU, KHÔNG dừng tính phí và KHÔNG xóa model trên GPU. Bạn phải dừng máy tại nhà cung cấp để ngừng tiền thuê. Các dự án đang dùng máy này sẽ cần chọn GPU khác." onDelete={async()=>{await api(`/api/hosts/${host.id}`,{method:'DELETE'});await client.invalidateQueries();}}/></div><SavedPreflight hostId={host.id} latest={reports[host.id]}/>{runs[host.id] && <RunView run={runs[host.id]} onUpdate={run => updateRun(host.id, run)} onCancel={() => runAction(host.id, `/api/runs/${runs[host.id].id}/cancel`)} onResume={() => runAction(host.id, `/api/runs/${runs[host.id].id}/resume`)} onSkip={step => runAction(host.id, `/api/runs/${runs[host.id].id}/steps/${step}/skip`)} />}</article>)}</div></section></div></div></div>;
}
