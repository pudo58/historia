import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

type Access = { token: string; query_parameter: string };

// Shown only on the computer running HISTORIA: the API refuses to reveal the token through a tunnel.
export default function RemoteAccess() {
  const client = useQueryClient();
  const [tunnel, setTunnel] = useState('');
  const [message, setMessage] = useState('');
  const query = useQuery({ queryKey: ['remote-access'], retry: false, queryFn: async () => {
    const response = await fetch('/api/remote-access');
    if (!response.ok) throw new Error('remote');
    return response.json() as Promise<Access>;
  } });
  if (query.error) return null;  // opened through a tunnel: nothing to show
  const base = tunnel.trim().replace(/\/+$/, '');
  const valid = /^https:\/\/[^\s/]+$/.test(base);
  const link = query.data && valid ? `${base}/?${query.data.query_parameter}=${query.data.token}` : '';
  const rotate = async () => {
    if (!confirm('Đổi mã truy cập? Mọi thiết bị đang mở HISTORIA từ xa sẽ phải mở lại link mới.')) return;
    const response = await fetch('/api/remote-access/rotate', { method: 'POST' });
    setMessage(response.ok ? 'Đã đổi mã. Link cũ không còn dùng được.' : 'Không đổi được mã.');
    await client.invalidateQueries({ queryKey: ['remote-access'] });
  };
  return <section className="panel">
    <h2>Truy cập từ xa</h2>
    <p>Mở HISTORIA qua tunnel (ví dụ cloudflared) cần mã truy cập riêng của máy này. Trang web lạ không gọi được HISTORIA; chỉ thiết bị đã mở link có mã mới dùng được (nhớ 30 ngày).</p>
    <label className="field"><span>Địa chỉ tunnel</span>
      <input value={tunnel} onChange={e => setTunnel(e.target.value)} placeholder="https://ten-ngau-nhien.trycloudflare.com" spellCheck={false}/></label>
    {link && <p><code style={{ wordBreak: 'break-all' }}>{link}</code>{' '}
      <button type="button" onClick={() => void navigator.clipboard.writeText(link).then(() => setMessage('Đã chép link. Không gửi link này cho người lạ.'))}>Chép link</button></p>}
    {tunnel && !valid && <p className="work-footnote">Nhập địa chỉ bắt đầu bằng https://, không kèm đường dẫn.</p>}
    <p className="work-footnote">Ai có link này đều điều khiển được HISTORIA và Pod của bạn. Lộ link thì bấm đổi mã.</p>
    <button type="button" onClick={() => void rotate()}>Đổi mã truy cập</button>
    {message && <p role="status">{message}</p>}
  </section>;
}
