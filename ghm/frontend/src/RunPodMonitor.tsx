import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

export type Pod={id:string;name:string|null;status:string;gpu:string|null;gpu_count:number|null;vcpu:number|null;memory_gb:number|null;cost_per_hr:number|null;uptime_seconds:number|null;session_cost:number|null;host_label:string|null;host_id:string|null;ssh_ready?:boolean;proxy_username?:string|null;linked_host_id?:string|null;linked_host_label?:string|null};
export type RunPodData={configured:boolean;ssh_key_path?:string;error?:string;pods:Pod[];running_count?:number;running_cost_per_hr?:number};
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
  const [keyPath, setKeyPath] = useState('');
  const [connecting, setConnecting] = useState('');
  const [pasted, setPasted] = useState<Record<string,string>>({});
  const post=async(url:string,body?:object)=>{
    const response=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},body:body?JSON.stringify(body):undefined});
    const data=await response.json().catch(()=>({}));
    if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'Thao tác không thành công.');
    return data;
  };
  const saveKeyPath=async()=>{
    setBusy(true); setMessage('');
    try { await post('/api/settings/runpod-ssh-key',{token:keyPath.trim()}); setKeyPath(''); await client.invalidateQueries({queryKey:['runpod-pods']}); }
    catch(error) { setMessage((error as Error).message); }
    finally { setBusy(false); }
  };
  const connect=async(pod:Pod,mode:'auto'|'proxy'='auto')=>{
    setConnecting(pod.id); setMessage('');
    try {
      const result=await post(`/api/runpod/pods/${pod.id}/connect`,{mode,ssh_command:pasted[pod.id]||''});
      await client.invalidateQueries({queryKey:['hosts']});
      const host=result.host;
      if(!host.pinned_fingerprint){
        const key=await post(`/api/hosts/${host.id}/inspect-key`);
        if(confirm(`SSH fingerprint của Pod ${pod.name||pod.id}:\n${key.fingerprint}\n\nRunPod không hiển thị fingerprint này để đối chiếu. Chỉ tin cậy nếu bạn vừa tự thuê Pod này. Lưu khóa và tin cậy máy?`))
          await post(`/api/hosts/${host.id}/confirm-key`,{fingerprint:key.fingerprint});
      }
      await client.invalidateQueries({queryKey:['hosts']});
      await client.invalidateQueries({queryKey:['runpod-pods']});
      setMessage(result.action==='address_updated'?'Đã cập nhật địa chỉ mới của Pod. Chạy "Kiểm tra máy" trước khi dùng.':'Đã nối Pod vào Historia. Bấm "Kiểm tra máy" ở danh sách máy bên dưới.');
    } catch(error) { setMessage((error as Error).message); }
    finally { setConnecting(''); }
  };
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
        <div style={{overflowX:'auto'}}><table><thead><tr><th>Pod</th><th>Trạng thái</th><th>GPU</th><th>$/giờ</th><th>Đã chạy</th><th>Đã tốn phiên này</th><th>Máy Historia</th><th></th></tr></thead><tbody>
          {data.pods.map(p=><tr key={p.id}><td>{p.name||p.id}</td><td>{p.status==='RUNNING'?'🟢 Đang chạy':p.status==='EXITED'?'⏸ Đã dừng':p.status}</td><td>{p.gpu?`${p.gpu_count||1}× ${p.gpu}`:'—'}</td><td>{money(p.cost_per_hr)}</td><td>{uptime(p.uptime_seconds)}</td><td>{money(p.session_cost)}</td><td>{p.host_label||'Chưa nối'}</td><td>{p.host_id||p.status!=='RUNNING'?null:p.ssh_ready
            ?<button disabled={connecting===p.id||!data.ssh_key_path} title={!data.ssh_key_path?'Lưu đường dẫn khóa SSH ở dưới trước':undefined} onClick={()=>connect(p)}>{connecting===p.id?'Đang nối…':p.linked_host_id?'Cập nhật địa chỉ':'Nối vào Historia (SSH gốc)'}</button>
            :<div>
              <small style={{display:'block'}}>Pod không mở TCP 22, nối qua proxy RunPod (Basic SSH).{!p.proxy_username&&' Dán lệnh SSH ở tab Connect của Pod:'}</small>
              {!p.proxy_username&&<input value={pasted[p.id]||''} onChange={e=>setPasted({...pasted,[p.id]:e.target.value})} placeholder="ssh abc123-xxxx@ssh.runpod.io -i ~/.ssh/id_ed25519" spellCheck={false} aria-label="Lệnh SSH của Pod"/>}
              <button disabled={connecting===p.id||!data.ssh_key_path||(!p.proxy_username&&!(pasted[p.id]||'').includes('@ssh.runpod.io'))} title={!data.ssh_key_path?'Lưu đường dẫn khóa SSH ở dưới trước':undefined} onClick={()=>connect(p,'proxy')}>{connecting===p.id?'Đang nối…':p.linked_host_id?'Cập nhật proxy':'Nối qua proxy'}</button>
            </div>}</td></tr>)}
        </tbody></table></div>
        {!data.ssh_key_path?<form onSubmit={e=>{e.preventDefault(); saveKeyPath();}}>
          <label className="field"><span>File khóa SSH riêng (để nối Pod tự động, khóa không đặt passphrase)</span><input value={keyPath} onChange={e=>setKeyPath(e.target.value)} placeholder="C:\Users\bạn\.ssh\id_ed25519" spellCheck={false} disabled={busy}/></label>
          <button disabled={busy||!keyPath.trim()}>Lưu đường dẫn khóa</button></form>
          :<p className="work-footnote">Khóa SSH dùng để nối Pod: {data.ssh_key_path} · <a href="#" onClick={e=>{e.preventDefault(); fetch('/api/settings/runpod-ssh-key',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:''})}).then(()=>client.invalidateQueries({queryKey:['runpod-pods']}));}}>đổi</a></p>}
        <p className="work-footnote">Cập nhật mỗi 30 giây. Pod đã dừng vẫn tính phí lưu ổ đĩa nhỏ; xóa Pod nếu không dùng nữa. <a href="https://www.runpod.io/console/pods" target="_blank" rel="noreferrer">Mở RunPod ↗</a></p></>}
      <button disabled={busy} onClick={()=>save('')}>Xóa API key</button>
    </>}
    {(message||query.error) && <p role="status">{message||query.error?.message}</p>}
  </section>;
}
