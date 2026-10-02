import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { performanceCall } from './PerformancePanel';
import CostPolicyPanel from './CostPolicyPanel';

type Config={attention_backend:'default'|'sage';memory_policy:'default'|'highvram'};
type State={config:Config;adopt_existing:boolean;instructions:string;actual?:unknown;jobs:{id:string;status:string;error?:string}[]};

export default function RuntimePanel({hostId}:{hostId:string}) {
  const url=`/api/studio/hosts/${hostId}/runtime`;
  const client=useQueryClient();
  const query=useQuery({queryKey:['runtime',hostId],queryFn:()=>performanceCall<State>(url),refetchInterval:5000});
  const [edited,setEdited]=useState<Config|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [actual,setActual]=useState<unknown>(null);
  const config=edited??query.data?.config??{attention_backend:'default',memory_policy:'default'};
  const action=async(name:string)=>{setBusy(true);setError('');try{const result=await performanceCall<State>(`${url}/${name}`,config);if(name==='inspect')setActual(result.actual);await client.invalidateQueries();}catch(e){setError((e as Error).message);}finally{setBusy(false);}};
  const pending=busy||query.data?.jobs.some(j=>['queued','running','reconciling'].includes(j.status));
  return <><section className="panel"><h3>Runtime GPU · tùy chọn thử nghiệm</h3><p>{query.data?.instructions}</p>
    <label className="field"><span>Attention</span><select value={config.attention_backend} onChange={e=>setEdited({...config,attention_backend:e.target.value as Config['attention_backend']})}><option value="default">Giữ mặc định ComfyUI</option><option value="sage">SageAttention · cần benchmark chất lượng</option></select></label>
    <label className="field"><span>Quản lý VRAM</span><select value={config.memory_policy} onChange={e=>setEdited({...config,memory_policy:e.target.value as Config['memory_policy']})}><option value="default">Mặc định</option><option value="highvram">Giữ model trong VRAM · cần đủ bộ nhớ</option></select></label>
    <p>Áp dụng sẽ khởi động lại ComfyUI do Historia quản lý sau khi kiểm tra queue rỗng và checkpoint an toàn. Không thay đổi kích thước, fps hoặc số frame.</p>
    <div className="actions"><button disabled={pending} onClick={()=>void action('inspect')}>Đọc runtime hiện tại</button>{!query.data?.adopt_existing&&<><button disabled={pending} onClick={()=>void action('apply')}>Áp dụng & khởi động lại</button><button disabled={pending} onClick={()=>void action('install-sage')}>Cài SageAttention đã ghim & áp dụng</button></>}</div>
    {!query.data?.adopt_existing&&query.data?.jobs.some(j=>['failed','interrupted','reconciling','cancelled'].includes(j.status))&&<button disabled={busy} onClick={()=>void action('recover')}>Khôi phục runtime đã chọn · không cài lại</button>}
    {(error||query.error)&&<p role="alert">{error||query.error?.message}</p>}{actual!=null&&<pre>{JSON.stringify(actual,null,2)}</pre>}
    {query.data?.jobs.map(j=><p key={j.id}>Runtime {j.id.slice(0,8)} · {j.status}{j.error?` · ${j.error}`:''}</p>)}
  </section><CostPolicyPanel key={hostId} hostId={hostId}/></>;
}
