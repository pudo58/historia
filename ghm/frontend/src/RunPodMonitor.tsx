import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

export type Pod={id:string;name:string|null;status:string;gpu:string|null;gpu_count:number|null;vcpu:number|null;memory_gb:number|null;cost_per_hr:number|null;uptime_seconds:number|null;session_cost:number|null;host_label:string|null;host_id:string|null;ssh_ready?:boolean;proxy_username?:string|null;linked_host_id?:string|null;linked_host_label?:string|null;host_ids?:string[];lanes?:{host_id:string;label:string;index:number}[]};
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
  const podAction=async(pod:Pod,action:'stop'|'start'|'terminate')=>{
    const name=pod.name||pod.id;
    let confirmName='';
    if(action==='stop'&&!confirm(`Dừng Pod "${name}"?\n\nGPU ngừng tính tiền, nhưng ổ đĩa của Pod vẫn tính phí lưu trữ nhỏ. Dữ liệu ngoài /workspace có thể mất khi bật lại. Bật lại có thể không còn GPU trống cùng loại.`))return;
    if(action==='terminate'){
      const typed=prompt(`XÓA VĨNH VIỄN Pod "${name}".\n\nToàn bộ dữ liệu trên Pod (model, output chưa tải về) sẽ mất, không khôi phục được. Máy trong Historia vẫn giữ để bạn tự xóa.\n\nGõ đúng tên Pod để xác nhận:`);
      if(typed===null)return;
      confirmName=typed.trim();
    }
    setConnecting(pod.id); setMessage('');
    const send=(force:boolean)=>post(`/api/runpod/pods/${pod.id}/action`,{action,confirm_name:confirmName,force});
    try {
      try { await send(false); }
      catch(error){
        const text=(error as Error).message;
        if(!text.startsWith('Historia còn tác vụ')||!confirm(text+'\n\nVẫn tiếp tục? Tác vụ đang chạy sẽ bị gián đoạn và cần đối chiếu lại.'))throw error;
        await send(true);
      }
      setMessage(action==='stop'?`Đã gửi lệnh dừng Pod ${name}. Trạng thái cập nhật sau vài giây.`:action==='start'?`Đã gửi lệnh bật Pod ${name}. IP/cổng có thể đổi: bấm "Cập nhật" sau khi Pod chạy.`:`Đã xóa Pod ${name} trên RunPod.`);
      await client.invalidateQueries({queryKey:['runpod-pods']});
    } catch(error) { setMessage((error as Error).message); }
    finally { setConnecting(''); }
  };
  const connect=async(pod:Pod,mode:'auto'|'proxy'='auto',allGpus=false)=>{
    setConnecting(pod.id); setMessage('');
    try {
      const result=await post(`/api/runpod/pods/${pod.id}/connect`,{mode,ssh_command:pasted[pod.id]||'',all_gpus:allGpus});
      await client.invalidateQueries({queryKey:['hosts']});
      // Every GPU lane shares one SSH endpoint, so one fingerprint confirmation covers all of them.
      let trusted='';
      for(const host of (result.lanes as {id:string;pinned_fingerprint?:string|null}[]|undefined)||[result.host]){
        if(host.pinned_fingerprint)continue;
        const key=await post(`/api/hosts/${host.id}/inspect-key`);
        if(key.fingerprint===trusted||confirm(`SSH fingerprint của Pod ${pod.name||pod.id}:\n${key.fingerprint}\n\nRunPod không hiển thị fingerprint này để đối chiếu. Chỉ tin cậy nếu bạn vừa tự thuê Pod này. Lưu khóa và tin cậy máy?`)){
          await post(`/api/hosts/${host.id}/confirm-key`,{fingerprint:key.fingerprint});
          trusted=key.fingerprint;
        }
      }
      await client.invalidateQueries({queryKey:['hosts']});
      await client.invalidateQueries({queryKey:['runpod-pods']});
      setMessage(allGpus&&result.lanes?.length>1?`Đã nối ${result.lanes.length} GPU của Pod thành ${result.lanes.length} máy Historia. Cài bộ AI cho GPU 0 trước, sau đó cài thêm cho các GPU còn lại (rất nhanh vì dùng chung model).`:result.action==='address_updated'?'Đã cập nhật địa chỉ mới của Pod. Chạy "Kiểm tra máy" trước khi dùng.':'Đã nối Pod vào Historia. Bấm "Kiểm tra máy" ở danh sách máy bên dưới.');
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
      <p>Nhập RunPod API key để xem Pod nào đang chạy và đang tốn bao nhiêu tiền. Historia chỉ dừng hay xóa Pod khi bạn bấm và xác nhận; dùng key Read/Write nếu muốn điều khiển Pod. Key lưu mã hóa trên máy này.</p>
      <form onSubmit={event=>{event.preventDefault(); save(key.trim());}}>
        <label className="field"><span>RunPod API key (nên tạo key chỉ đọc)</span><input type="password" value={key} onChange={e=>setKey(e.target.value)} autoComplete="off" spellCheck={false} disabled={busy} aria-label="RunPod API key"/></label>
        <button className="primary" disabled={busy||key.trim().length<16}>{busy?'Đang lưu…':'Lưu key'}</button>
      </form></>}
    {data?.configured && <>
      {data.error ? <p className="work-error-box" role="alert">{data.error}</p> : <>
        <p><strong>{data.running_count} Pod đang chạy</strong> · đang tốn {money(data.running_cost_per_hr)}/giờ (≈ {money((data.running_cost_per_hr||0)*24)}/ngày)</p>
        {data.pods.length===0 && <p>Tài khoản chưa có Pod nào.</p>}
        <div style={{overflowX:'auto'}}><table><thead><tr><th>Pod</th><th>Trạng thái</th><th>GPU</th><th>$/giờ</th><th>Đã chạy</th><th>Đã tốn phiên này</th><th>Máy Historia</th><th></th><th>Điều khiển</th></tr></thead><tbody>
          {data.pods.map(p=><tr key={p.id}><td>{p.name||p.id}</td><td>{p.status==='RUNNING'?'🟢 Đang chạy':p.status==='EXITED'?'⏸ Đã dừng':p.status}</td><td>{p.gpu?`${p.gpu_count||1}× ${p.gpu}`:'—'}</td><td>{money(p.cost_per_hr)}</td><td>{uptime(p.uptime_seconds)}</td><td>{money(p.session_cost)}</td><td>{p.lanes&&p.lanes.length>1?p.lanes.map(l=>l.label).join(' + '):p.host_label||'Chưa nối'}</td><td>{p.host_id&&p.status==='RUNNING'&&(p.gpu_count||1)>1&&(p.lanes?.length||0)<(p.gpu_count||1)?<div><small style={{display:'block'}}>Pod có {p.gpu_count} GPU nhưng Historia mới dùng 1. Nối mỗi GPU thành một máy để render song song.</small><button disabled={connecting===p.id||!data.ssh_key_path||(!p.ssh_ready&&!p.proxy_username&&!(pasted[p.id]||'').includes('@ssh.runpod.io'))} onClick={()=>connect(p,p.ssh_ready?'auto':'proxy',true)}>{connecting===p.id?'Đang nối…':`Nối tất cả ${p.gpu_count} GPU`}</button></div>:p.host_id||p.status!=='RUNNING'?null:p.ssh_ready
            ?<button disabled={connecting===p.id||!data.ssh_key_path} title={!data.ssh_key_path?'Lưu đường dẫn khóa SSH ở dưới trước':undefined} onClick={()=>connect(p,'auto',(p.gpu_count||1)>1)}>{connecting===p.id?'Đang nối…':p.linked_host_id?'Cập nhật địa chỉ':(p.gpu_count||1)>1?`Nối ${p.gpu_count} GPU vào Historia (SSH gốc)`:'Nối vào Historia (SSH gốc)'}</button>
            :<div>
              <small style={{display:'block'}}>Pod không mở TCP 22, nối qua proxy RunPod (Basic SSH).{!p.proxy_username&&' Dán lệnh SSH ở tab Connect của Pod:'}</small>
              {!p.proxy_username&&<input value={pasted[p.id]||''} onChange={e=>setPasted({...pasted,[p.id]:e.target.value})} placeholder="ssh abc123-xxxx@ssh.runpod.io -i ~/.ssh/id_ed25519" spellCheck={false} aria-label="Lệnh SSH của Pod"/>}
              <button disabled={connecting===p.id||!data.ssh_key_path||(!p.proxy_username&&!(pasted[p.id]||'').includes('@ssh.runpod.io'))} title={!data.ssh_key_path?'Lưu đường dẫn khóa SSH ở dưới trước':undefined} onClick={()=>connect(p,'proxy',(p.gpu_count||1)>1)}>{connecting===p.id?'Đang nối…':p.linked_host_id?'Cập nhật proxy':(p.gpu_count||1)>1?`Nối ${p.gpu_count} GPU qua proxy`:'Nối qua proxy'}</button>
            </div>}</td><td><div style={{display:'flex',gap:6,flexWrap:'wrap'}}>
              {p.status==='RUNNING'&&<button disabled={connecting===p.id} onClick={()=>podAction(p,'stop')}>Dừng</button>}
              {p.status==='EXITED'&&<button disabled={connecting===p.id} onClick={()=>podAction(p,'start')}>Bật</button>}
              <button className="danger" disabled={connecting===p.id} onClick={()=>podAction(p,'terminate')}>Xóa Pod</button>
            </div></td></tr>)}
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
