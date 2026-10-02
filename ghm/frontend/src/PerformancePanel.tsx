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
  const [shortenLast,setShortenLast] = useState(false);
  const [error,setError] = useState('');
  const [busy,setBusy] = useState(false);
  const [saved,setSaved] = useState(false);
  const client = useQueryClient();
  return <form onSubmit={async e=>{e.preventDefault();setBusy(true);setError('');setSaved(false);try{await performanceCall(url,{clip_steps:steps,shorten_last_shot:shortenLast});await client.invalidateQueries();setSaved(true);}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>
    <label className="field"><span>Steps Wan cho shot chưa gửi · Lightning mặc định 4</span><input type="number" min="2" max="50" required value={steps} onChange={e=>setSteps(Number(e.target.value))}/></label>
    <label className="check"><input type="checkbox" checked={shortenLast} onChange={e=>setShortenLast(e.target.checked)}/>Rút shot cuối của cảnh còn 4n+1 frame nếu vẫn phủ audio</label>
    <p>Giữ nguyên độ phân giải, 16 fps và các shot đã gửi hoặc hoàn thành. Shot thường vẫn 81 frame.</p>
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
  const hosts=useQuery({queryKey:['hosts'],queryFn:()=>performanceCall<{id:string;label:string}[]>('/api/hosts')});
  const [host,setHost]=useState('');
  const [rate,setRate]=useState('');
  const [scene,setScene]=useState('');
  const [minutes,setMinutes]=useState(30);
  const [secondScene,setSecondScene]=useState('');
  const [residency,setResidency]=useState('job');
  const [concurrency,setConcurrency]=useState(1);
  const [memoryBaseline,setMemoryBaseline]=useState('');
  const [budget,setBudget]=useState('');
  const [consent,setConsent]=useState(false);
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  const client=useQueryClient();
  return <details><summary>Benchmark Wan trên GPU · chạy riêng</summary><form onSubmit={async e=>{e.preventDefault();setBusy(true);setMessage('');try{const job=await performanceCall<{id:string}>(`/api/studio/projects/${projectId}/benchmarks`,{scene_id:scene||scenes[0]?.scene_id,host_id:host||null,hourly_usd:rate?Number(rate):null,second_scene_id:secondScene||null,warm_shots:secondScene?5:10,max_wall_seconds:minutes*60,max_cost_usd:Number(budget),model_residency:residency,wan_concurrency:concurrency,memory_baseline_id:memoryBaseline||null});setMessage(`Đã tạo benchmark ${job.id}. Xem clip và báo cáo JSON trong danh sách tác vụ.`);await client.invalidateQueries();}catch(e){setMessage((e as Error).message);}finally{setBusy(false);}}}>
    <p>Đo cùng bộ 10 shot nóng, thêm shot khởi động. Chọn hai cảnh để đo dỡ/nạp model giữa cảnh. Đây là lượt GPU có phí; không thay thế clip sản xuất. Tạm dừng phim ở ranh giới đã lưu output, đối chiếu mọi prompt và nhập giá thuê thực của Pod trong cấu hình dự án.</p>
    <label className="field"><span>GPU dùng cho lượt đo</span><select value={host} onChange={e=>setHost(e.target.value)}><option value="">GPU của phim đang chọn</option>{hosts.data?.map(h=><option key={h.id} value={h.id}>{h.label}</option>)}</select></label>
    <label className="field"><span>Giá thuê thực của lượt đo (USD/giờ)</span><input type="number" min="0.01" max="100" step="0.01" required={!!host} value={rate} placeholder="Để trống dùng giá của dự án" onChange={e=>setRate(e.target.value)}/></label>
    <p>Chọn GPU khác chỉ đổi lượt benchmark, giữ GPU và kết quả của phim. GPU đó phải có Wan đã kiểm chứng; lấy giá thực từ Pod/console.</p>
    <label className="field"><span>Ảnh của cảnh</span><select value={scene||scenes[0]?.scene_id||''} onChange={e=>setScene(e.target.value)}>{scenes.map(s=><option key={s.scene_id} value={s.scene_id}>{s.title}</option>)}</select></label>
    <label className="field"><span>Cảnh thứ hai · đo ranh giới cảnh</span><select value={secondScene} onChange={e=>setSecondScene(e.target.value)}><option value="">Chỉ một cảnh</option>{scenes.filter(s=>s.scene_id!==(scene||scenes[0]?.scene_id)).map(s=><option key={s.scene_id} value={s.scene_id}>{s.title}</option>)}</select></label>
    <label className="field"><span>Giữ model trong lượt đo</span><select value={residency} onChange={e=>setResidency(e.target.value)}><option value="job">Theo job · baseline</option><option value="stage">Theo công đoạn · cần hai cảnh</option></select></label>
    <label className="field"><span>Số tiến trình Wan</span><select value={concurrency} onChange={e=>setConcurrency(Number(e.target.value))}><option value={1}>1</option><option value={2}>2 · kiểm tra VRAM đã đo trước khi gửi</option></select></label>
    {concurrency===2&&<label className="field"><span>ID benchmark VRAM trên GPU này</span><input required value={memoryBaseline} onChange={e=>setMemoryBaseline(e.target.value)}/></label>}
    <label className="field"><span>Giới hạn thời gian (phút, kiểm tra giữa các shot)</span><input type="number" required min="5" max="240" value={minutes} onChange={e=>setMinutes(Number(e.target.value))}/></label>
    <label className="field"><span>Ngân sách xử lý tối đa (USD)</span><input type="number" required min="0.01" step="0.01" value={budget} onChange={e=>setBudget(e.target.value)}/></label>
    <p>Giới hạn được kiểm tra trước shot mới; shot đã gửi có thể vượt ngân sách một phần và vẫn được tải về. Tiền Pod chờ và storage tính riêng. Đổi attention/VRAM trong Runtime rồi chạy mỗi cấu hình bằng lượt riêng.</p>
    <label><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/> Tôi muốn chạy benchmark có phí trên GPU đã chọn.</label><button disabled={busy||!consent||!scenes.length||(residency==='stage'&&!secondScene)}>Chạy bộ shot đo thử</button>{message&&<p role="status">{message}</p>}
  </form></details>;
}
