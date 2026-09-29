import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

export type Pod={id:string;name:string|null;status:string;gpu:string|null;gpu_count:number|null;vcpu:number|null;memory_gb:number|null;cost_per_hr:number|null;uptime_seconds:number|null;session_cost:number|null;host_label:string|null;host_id:string|null};
export type RunPodData={configured:boolean;error?:string;pods:Pod[];running_count?:number;running_cost_per_hr?:number};
export const money=(v:number|null|undefined)=>v==null?'—':`$${v.toFixed(v<1?3:2)}`;
export const uptime=(s:number|null)=>s==null?'—':`${Math.floor(s/3600)}g ${Math.floor(s%3600/60)}p`;

export function useRunPod() {
  return useQuery({queryKey:['runpod-pods'], refetchInterval:30000, queryFn:async()=>{
    const response=await fetch('/api/runpod/pods');
    if(!response.ok) throw new Error('Không đọc được dữ liệu RunPod.');
    return response.json() as Promise<RunPodData>;
  }});
}

export default function RunPodMonitor() {
  const client = useQueryClient();
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const query = useRunPod();
  const data=query.data;
  const save=async(value:string)=>{
    setBusy(true); setMessage('');
    try {
      const response=await fetch('/api/settings/runpod-key',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:value})});
      if(!response.ok) throw new Error('Không lưu được API key. Kiểm tra lại key.');
      setKey(''); await client.invalidateQueries({queryKey:['runpod-pods']});
    } catch(error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  };
  return <section className="panel"><h2>Theo dõi Pod · RunPod</h2>
    {!data?.configured && <>
      <p>Nhập RunPod API key để xem Pod nào đang chạy và đang tốn bao nhiêu tiền. Historia chỉ đọc, không bao giờ dừng hay xóa Pod. Key lưu mã hóa trên máy này.</p>
      <form onSubmit={event=>{event.preventDefault(); save(key.trim());}}>
        <label className="field"><span>RunPod API key (nên tạo key chỉ đọc)</span><input type="password" value={key} onChange={e=>setKey(e.target.value)} autoComplete="off" spellCheck={false} disabled={busy} aria-label="RunPod API key"/></label>
        <button className="primary" disabled={busy||key.trim().length<16}>{busy?'Đang lưu…':'Lưu key'}</button>
      </form></>}
    {data?.configured && <>
      {data.error ? <p className="work-error-box" role="alert">{data.error}</p> : <>
        <p><strong>{data.running_count} Pod đang chạy</strong> · đang tốn {money(data.running_cost_per_hr)}/giờ (≈ {money((data.running_cost_per_hr||0)*24)}/ngày)</p>
        {data.pods.length===0 && <p>Tài khoản chưa có Pod nào.</p>}
        <div style={{overflowX:'auto'}}><table><thead><tr><th>Pod</th><th>Trạng thái</th><th>GPU</th><th>$/giờ</th><th>Đã chạy</th><th>Đã tốn phiên này</th><th>Máy Historia</th></tr></thead><tbody>
          {data.pods.map(p=><tr key={p.id}><td>{p.name||p.id}</td><td>{p.status==='RUNNING'?'🟢 Đang chạy':p.status==='EXITED'?'⏸ Đã dừng':p.status}</td><td>{p.gpu?`${p.gpu_count||1}× ${p.gpu}`:'—'}</td><td>{money(p.cost_per_hr)}</td><td>{uptime(p.uptime_seconds)}</td><td>{money(p.session_cost)}</td><td>{p.host_label||'Chưa nối'}</td></tr>)}
        </tbody></table></div>
        <p className="work-footnote">Cập nhật mỗi 30 giây. Pod đã dừng vẫn tính phí lưu ổ đĩa nhỏ; xóa Pod nếu không dùng nữa. <a href="https://www.runpod.io/console/pods" target="_blank" rel="noreferrer">Mở RunPod ↗</a></p></>}
      <button disabled={busy} onClick={()=>save('')}>Xóa API key</button>
    </>}
    {(message||query.error) && <p role="status">{message||query.error?.message}</p>}
  </section>;
}
