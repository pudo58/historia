import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

export default function HuggingFaceToken() {
  const client = useQueryClient();
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const settings = useQuery({queryKey:['hf-token-status'], queryFn:async()=>{
    const response=await fetch('/api/settings');
    if(!response.ok) throw new Error('Không đọc được trạng thái token.');
    return response.json() as Promise<{hf_token_configured:boolean}>;
  }});
  return <section className="panel"><h2>Quyền tải model · Hugging Face</h2>
    <p>{settings.data?.hf_token_configured ? 'Đã lưu token. Quyền tải từng model sẽ được kiểm tra khi chuẩn bị cài.' : 'Chưa cấu hình token. Nhập token có quyền đọc model đã được tài khoản của bạn cấp quyền truy cập.'}</p>
    <p>Token không được gửi vào chat. Không cần quyền ghi. Lưu token không tự cấp quyền vào model bị giới hạn.</p>
    <form onSubmit={async event=>{
      event.preventDefault(); setBusy(true); setMessage('');
      try {
        const response=await fetch('/api/settings/hf-token',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:token.trim()})});
        if(!response.ok) throw new Error('Không lưu được token. Kiểm tra định dạng hf_ và kết nối tới HISTORIA.');
        setToken(''); setMessage('Đã lưu token. Bây giờ bấm Kiểm tra & chuẩn bị cài để xác minh quyền tải.');
        await client.invalidateQueries({queryKey:['hf-token-status']});
      } catch(error) { setMessage((error as Error).message); }
      finally { setBusy(false); }
    }}>
      <label className="field"><span>Token quyền đọc</span><input type="password" value={token} onChange={event=>setToken(event.target.value)} autoComplete="off" spellCheck={false} placeholder="hf_…" disabled={busy} aria-label="Hugging Face token"/></label>
      <button className="primary" disabled={busy || !token.trim().startsWith('hf_')}>{busy ? 'Đang lưu…' : 'Lưu token'}</button>
    </form>
    {(message || settings.error) && <p role="status">{message || settings.error?.message}</p>}
  </section>;
}
