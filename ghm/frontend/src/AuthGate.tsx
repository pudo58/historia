import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';

type Status = { enabled: boolean; configured: boolean; authenticated: boolean; min_length: number };
export const LOCKED_EVENT = 'historia:locked';

async function call(path: string, body?: unknown): Promise<Status> {
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map((e: { msg: string }) => e.msg).join('; ') : 'Không thực hiện được. Thử lại.');
  return data;
}

/** Nothing of the studio is rendered (or requested) until the password is entered. */
export default function AuthGate({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [status, setStatus] = useState<Status | null>(null);
  const [failure, setFailure] = useState('');
  const refresh = useCallback(() => call('/api/auth/status').then(setStatus).catch(() => setFailure('Không kết nối được máy chủ local. Hãy chạy start-historia.bat.')), []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const locked = () => { client.clear(); void refresh(); };
    window.addEventListener(LOCKED_EVENT, locked);
    return () => window.removeEventListener(LOCKED_EVENT, locked);
  }, [client, refresh]);

  if (status && (!status.enabled || status.authenticated)) return <>{children}</>;
  if (!status) return <main className="auth-screen"><p role="status">{failure || 'Đang kiểm tra…'}</p></main>;
  return <Form status={status} onDone={(next) => { client.clear(); setStatus(next); }}/>;
}

function Form({ status, onDone }: { status: Status; onDone: (s: Status) => void }) {
  const setup = !status.configured;
  const [password, setPassword] = useState('');
  const [again, setAgain] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    if (setup && password.length < status.min_length) return setError(`Mật khẩu cần từ ${status.min_length} ký tự trở lên.`);
    if (setup && password !== again) return setError('Hai lần nhập mật khẩu chưa giống nhau.');
    setBusy(true);
    try { onDone(await call(setup ? '/api/auth/setup' : '/api/auth/login', { password })); }
    catch (e) { setError(e instanceof Error ? e.message : 'Lỗi không xác định.'); setBusy(false); }
  };
  return <main className="auth-screen">
    <form className="auth-card" onSubmit={submit}>
      <span className="monogram">H</span>
      <h1>{setup ? 'Tạo mật khẩu cho HISTORIA' : 'Đăng nhập HISTORIA'}</h1>
      <p>{setup ? 'Mật khẩu này chặn người khác dùng studio, kể cả khi mở qua tunnel. Lưu ở máy này, đã băm, không gửi đi đâu.' : 'Nhập mật khẩu để dùng các chức năng.'}</p>
      <label><span>Mật khẩu</span><input type="password" autoFocus autoComplete={setup ? 'new-password' : 'current-password'} value={password} onChange={e => setPassword(e.target.value)} required/></label>
      {setup && <label><span>Nhập lại mật khẩu</span><input type="password" autoComplete="new-password" value={again} onChange={e => setAgain(e.target.value)} required/></label>}
      {error && <p className="auth-error" role="alert">{error}</p>}
      <button className="primary" disabled={busy || !password}>{busy ? 'Đang xử lý…' : setup ? 'Tạo mật khẩu' : 'Đăng nhập'}</button>
      {!setup && <small>Quên mật khẩu? Trên máy này chạy <code>ghm reset-password</code>.</small>}
    </form>
  </main>;
}

/** Topbar buttons: change password, log out. */
export function AccountControls() {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [message, setMessage] = useState('');
  const logout = async () => { await fetch('/api/auth/logout', { method: 'POST' }); window.dispatchEvent(new Event(LOCKED_EVENT)); };
  const change = async (event: FormEvent) => {
    event.preventDefault();
    try { await call('/api/auth/change-password', { current, new: next }); setMessage('Đã đổi mật khẩu. Các thiết bị khác phải đăng nhập lại.'); setCurrent(''); setNext(''); }
    catch (e) { setMessage(e instanceof Error ? e.message : 'Không đổi được.'); }
  };
  return <span className="account-controls">
    <button type="button" onClick={() => { setOpen(!open); setMessage(''); }}>Đổi mật khẩu</button>
    <button type="button" onClick={logout}>Đăng xuất</button>
    {open && <form className="account-popover" onSubmit={change}>
      <input type="password" placeholder="Mật khẩu hiện tại" autoComplete="current-password" value={current} onChange={e => setCurrent(e.target.value)} required/>
      <input type="password" placeholder="Mật khẩu mới (tối thiểu 8 ký tự)" autoComplete="new-password" value={next} onChange={e => setNext(e.target.value)} required minLength={8}/>
      <button className="primary" disabled={!current || next.length < 8}>Lưu mật khẩu mới</button>
      {message && <small role="status">{message}</small>}
    </form>}
  </span>;
}
