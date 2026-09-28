import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

type Config = {width:number;height:number;frames:number;fps:number;steps:number;shot_seconds:number};
type Shot = {prompt_id?:string;timing?:Record<string,number|null>;attempts?:number};
type Performance = {completed_shots:number;remaining_shots:number;unmeasured_scenes:number;median_shot_seconds:number|null;eta_seconds:number|null;estimated_remaining_usd:number|null;scenes:{scene_id:string;title:string;audio_seconds:number|null;shot_count:number|null;completed_shots:number;configs:Config[]}[];shots:Shot[]};

export async function performanceCall<T>(url:string, body?:unknown):Promise<T> {
  const response = await fetch(url, {method:body===undefined?'GET':'POST', headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'Không thể cập nhật cấu hình.');
  return data;
}
const seconds = (n:number|null|undefined) => n==null?'Chưa đủ số đo':`${n.toFixed(1)} giây`;

export function PendingClipSettings({url}:{url:string}) {
  const [steps,setSteps] = useState(4);
  const [error,setError] = useState('');
  const [busy,setBusy] = useState(false);
  const [saved,setSaved] = useState(false);
  const client = useQueryClient();
  return <form onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');setSaved(false);try{await performanceCall(url,{clip_steps:steps});await client.invalidateQueries();setSaved(true);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>
    <label className="field"><span>Steps Wan cho shot chưa gửi · Lightning mặc định 4</span><input type="number" min="2" max="50" required value={steps} onChange={e=>setSteps(Number(e.target.value))}/></label>
    <p>Giữ nguyên độ phân giải, 81 frame, 16 fps và các shot đã gửi hoặc hoàn thành.</p>
    <button disabled={busy}>Áp dụng cho phần chưa gửi</button>{saved&&<p role="status">Đã lưu cấu hình phần còn thiếu. Tiếp tục khi sẵn sàng.</p>}{error&&<p role="alert">{error}</p>}
  </form>;
}

export default function PerformancePanel({projectId}:{projectId:string}) {
  const query = useQuery({queryKey:['performance',projectId],queryFn:()=>performanceCall<Performance>(`/api/studio/projects/${projectId}/performance`),refetchInterval:5000});
  const p=query.data;
  return <section className="panel"><h3>Cấu hình và thời gian tạo clip</h3>{query.error&&<p role="alert">{query.error.message}</p>}{p&&<>
    <p>{p.completed_shots} shot đã lưu · {p.remaining_shots} shot còn thiếu{p.unmeasured_scenes>0?` · ${p.unmeasured_scenes} cảnh chưa có audio để tính số shot`:''}</p>
    <p>Trung vị shot nóng: {seconds(p.median_shot_seconds)} · ETA xử lý còn lại: {seconds(p.eta_seconds)}</p>
    <p>Chi phí xử lý còn lại: {p.estimated_remaining_usd==null?'Chưa đủ số đo hoặc giá thuê':`$${p.estimated_remaining_usd.toFixed(2)}`}. Đây là ước tính xử lý, không phải tổng tiền thuê Pod; thời gian chờ và duyệt vẫn có thể tính phí.</p>
    <ul>{p.scenes.map(s=><li key={s.scene_id}><strong>{s.title}</strong> · {seconds(s.audio_seconds)} · {s.shot_count??'?'} shot{s.configs.map((c,i)=><p key={i}>{c.width}×{c.height} nguồn · {c.frames} frame / {c.fps} fps · {c.steps} bước · {c.shot_seconds.toFixed(4)} giây/shot</p>)}</li>)}</ul>
    <details><summary>Số đo từng shot</summary>{p.shots.map((s,i)=><p key={s.prompt_id??i}>Shot {i+1} · chuẩn bị/upload {seconds(s.timing?.prepare_upload_seconds)} · chờ remote {seconds(s.timing?.remote_wait_seconds)} · Comfy thực thi {seconds(s.timing?.comfy_execution_seconds)} · tải {seconds(s.timing?.download_seconds)} · tổng {seconds(s.timing?.total_seconds)} · {s.attempts??1} lần tiếp tục/xử lý</p>)}</details>
    <BenchmarkControl projectId={projectId} scenes={p.scenes}/>
  </>}</section>;
}

export function BenchmarkControl({projectId,scenes}:{projectId:string;scenes:Performance['scenes']}) {
  const [scene,setScene]=useState('');
  const [minutes,setMinutes]=useState(30);
  const [consent,setConsent]=useState(false);
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  const client=useQueryClient();
  return <details><summary>Benchmark Wan trên GPU · chạy riêng</summary><form onSubmit={async e=>{e.preventDefault();setBusy(true);setMessage('');try{const job=await performanceCall<{id:string}>(`/api/studio/projects/${projectId}/benchmarks`,{scene_id:scene||scenes[0]?.scene_id,warm_shots:5,max_wall_seconds:minutes*60});setMessage(`Đã tạo benchmark ${job.id}. Xem clip và báo cáo JSON trong danh sách tác vụ.`);await client.invalidateQueries();}catch(e){setMessage((e as Error).message);}finally{setBusy(false);}}}>
    <p>Tạo 1 shot khởi động và 5 shot nóng từ ảnh đã duyệt. Đây là lượt GPU có phí; không thay thế clip sản xuất. Dự án và GPU phải rảnh.</p>
    <label className="field"><span>Ảnh của cảnh</span><select value={scene||scenes[0]?.scene_id||''} onChange={e=>setScene(e.target.value)}>{scenes.map(s=><option key={s.scene_id} value={s.scene_id}>{s.title}</option>)}</select></label>
    <label className="field"><span>Giới hạn thời gian (phút, kiểm tra giữa các shot)</span><input type="number" required min="5" max="240" value={minutes} onChange={e=>setMinutes(Number(e.target.value))}/></label>
    <label><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/> Tôi muốn chạy benchmark có phí trên GPU đã chọn.</label><button disabled={busy||!consent||!scenes.length}>Chạy 6 shot đo thử</button>{message&&<p role="status">{message}</p>}
  </form></details>;
}
