import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

type Settings = { hf_token_configured: boolean; hf_token_hint?: string | null };

export default function HuggingFaceToken() {
  const client = useQueryClient();
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [message, setMessage] = useState('');
  const settings = useQuery({queryKey:['hf-token-status'], queryFn:async()=>{
    const response=await fetch('/api/settings');
    if(!response.ok) throw new Error('Không đọc được trạng thái token.');
    return response.json() as Promise<Settings>;
  }});
  const saved = !!settings.data?.hf_token_configured;
  const save = async (value: string) => {
    setBusy(true); setMessage('');
    try {
      const response=await fetch('/api/settings/hf-token',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:value})});
      if(!response.ok) throw new Error(value ? 'Không lưu được token. Kiểm tra định dạng hf_ và kết nối tới HISTORIA.' : 'Không xóa được token.');
      setToken(''); setEditing(false); setConfirmRemove(false);
      setMessage(value ? 'Đã lưu token vào máy này. Lần sau mở app không cần nhập lại.' : 'Đã xóa token khỏi máy này.');
      await client.invalidateQueries({queryKey:['hf-token-status']});
    } catch(error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  };
  return <section className="panel"><h2>Quyền tải model · Hugging Face</h2>
    {saved && !editing ? <>
      <p><strong>✓ Đã lưu token{settings.data?.hf_token_hint ? ` ${settings.data.hf_token_hint}` : ''}</strong> — mã hóa trong cơ sở dữ liệu của HISTORIA trên máy này, dùng lại mỗi lần cài. Không cần nhập lại.</p>
      <p>Quyền tải từng model vẫn được kiểm tra khi chuẩn bị cài. Lưu token không tự cấp quyền vào model bị giới hạn.</p>
      <div className="actions">
        <button type="button" disabled={busy} onClick={()=>{setEditing(true);setMessage('');}}>Thay token khác</button>
        {confirmRemove
          ? <><button type="button" className="danger" disabled={busy} onClick={()=>void save('')}>Xác nhận xóa token</button><button type="button" disabled={busy} onClick={()=>setConfirmRemove(false)}>Giữ lại</button></>
          : <button type="button" disabled={busy} onClick={()=>setConfirmRemove(true)}>Xóa token</button>}
      </div>
    </> : <>
      <p>{saved ? 'Nhập token mới để thay token đang lưu.' : 'Chưa có token. Nhập token có quyền đọc các model tài khoản của bạn đã được cấp quyền; token được lưu mã hóa trên máy này để dùng lại.'}</p>
      <p>Token không được gửi vào chat. Không cần quyền ghi.</p>
      <form onSubmit={event=>{event.preventDefault(); void save(token.trim());}}>
        <label className="field"><span>Token quyền đọc</span><input type="password" value={token} onChange={event=>setToken(event.target.value)} autoComplete="off" spellCheck={false} placeholder="hf_…" disabled={busy} aria-label="Hugging Face token"/></label>
        <div className="actions">
          <button className="primary" disabled={busy || !token.trim().startsWith('hf_')}>{busy ? 'Đang lưu…' : 'Lưu token'}</button>
          {saved && <button type="button" disabled={busy} onClick={()=>{setEditing(false);setToken('');}}>Hủy, giữ token cũ</button>}
        </div>
      </form>
    </>}
    {(message || settings.error) && <p role="status">{message || settings.error?.message}</p>}
  </section>;
}
