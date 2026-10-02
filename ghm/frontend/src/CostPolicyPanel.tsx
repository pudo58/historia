import {useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {performanceCall} from './PerformancePanel';

type Config={model_residency:'job'|'stage';wan_concurrency:1|2};
type Measurement={telemetry?:{gpu_name?:string;gpu_utilization_mean?:number;process_peak_vram_bytes?:number;host_peak_ram_bytes?:number};timing?:{comfy_execution_seconds?:number}};
type State={config:Config;fault?:{reason:string}|null;evidence?:{comparison:{throughput_gain:number;stage_wall_speedup:number}}|null;measurement?:Measurement|null};
type Bench={id:string;host_id?:string;status:string;created_at:string;kind:string;result?:{benchmark_report?:{policy?:Config;clips_per_gpu_hour?:number;usd_per_finished_minute?:number}}};
export const gib=(v:number|null|undefined)=>v==null?'Chưa đo':`${(v/1024**3).toFixed(1)} GiB`;

export default function CostPolicyPanel({hostId}:{hostId:string}) {
  const url=`/api/studio/hosts/${hostId}/cost-policy`;
  const client=useQueryClient();
  const state=useQuery({queryKey:['cost-policy',hostId],queryFn:()=>performanceCall<State>(url),refetchInterval:5000});
  const jobs=useQuery({queryKey:['benchmark-jobs'],queryFn:()=>performanceCall<Bench[]>('/api/studio/jobs'),refetchInterval:10000});
  const [edited,setEdited]=useState<Config|null>(null);
  const [baseline,setBaseline]=useState('');
  const [candidate,setCandidate]=useState('');
  const [review,setReview]=useState(false);
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  const [comparison,setComparison]=useState<{throughput_gain:number;stage_wall_speedup:number;processing_cost_saving:number|null;candidate_eligible:boolean;hardware_changed:boolean}|null>(null);
  const config=edited??state.data?.config??{model_residency:'job',wan_concurrency:1};
  const samples=jobs.data?.filter(j=>j.kind==='benchmark'&&j.status==='completed'&&j.result?.benchmark_report)||[];
  const submit=async(action:'apply'|'compare'|'reconcile')=>{
    setBusy(true);setMessage('');
    try {
      if(action==='compare')setComparison(await performanceCall(`/api/studio/benchmarks/compare-cost?baseline_id=${encodeURIComponent(baseline)}&candidate_id=${encodeURIComponent(candidate)}`));
      else if(action==='reconcile')await performanceCall(`${url}/reconcile`,{});
      else {await performanceCall(url,{...config,baseline_id:baseline||null,candidate_id:candidate||null,quality_review_passed:review});setMessage('Đã lưu hồ sơ. Checkpoint và clip cũ được giữ nguyên.');setEdited(null);}
      await client.invalidateQueries({queryKey:['cost-policy',hostId]});
    } catch(e) {setMessage((e as Error).message);} finally {setBusy(false);}
  };
  const t=state.data?.measurement?.telemetry;
  return <section className="panel"><h3>Chi phí GPU · hồ sơ đã đo</h3>
    <p>Hồ sơ đã chọn: giữ model theo <strong>{state.data?.config?.model_residency==='stage'?'công đoạn':'job'}</strong> · <strong>{state.data?.config?.wan_concurrency??1} Wan/GPU</strong>. Shot chưa có hồ sơ khớp vẫn chạy một Wan. Giữ nguyên chất lượng, frames, fps và steps của cảnh.</p>
    {t&&<p>Số đo shot đã lưu gần nhất: {t.gpu_name||'GPU chưa xác định'} · tải GPU trung bình {t.gpu_utilization_mean==null?'Chưa đo':`${t.gpu_utilization_mean.toFixed(0)}%`} · VRAM đỉnh theo PID {gib(t.process_peak_vram_bytes)} · RAM hệ thống đỉnh {gib(t.host_peak_ram_bytes)}. Đỉnh được lấy mẫu mỗi 2 giây.</p>}
    {state.data?.evidence&&<p>Hồ sơ đã duyệt: thông lượng ×{state.data.evidence.comparison.throughput_gain.toFixed(2)} · tốc độ toàn clip stage ×{state.data.evidence.comparison.stage_wall_speedup.toFixed(2)}.</p>}
    {state.data?.fault&&<p role="alert">{state.data.fault.reason} <button disabled={busy} onClick={()=>void submit('reconcile')}>Đọc và đối chiếu hai tiến trình</button></p>}
    <label className="field"><span>Giữ model giữa các cảnh</span><select value={config.model_residency} onChange={e=>setEdited({...config,model_residency:e.target.value as Config['model_residency']})}><option value="job">Theo job · mặc định</option><option value="stage">Theo công đoạn · giải phóng khi duyệt/pause hoặc hết 60 giây</option></select></label>
    <label className="field"><span>Clip Wan trên mỗi GPU</span><select value={config.wan_concurrency} onChange={e=>setEdited({...config,wan_concurrency:Number(e.target.value) as 1|2})}><option value={1}>1 · mặc định</option><option value={2}>2 · cần hai Comfy và benchmark đủ VRAM</option></select></label>
    <p>Mở hai Wan cần VRAM đo theo PID + 10% mỗi tiến trình, chừa 10% GPU; cùng 10 shot, thông lượng tăng ≥20%, thời gian toàn lượt giảm và bạn đã xem phim mẫu. Cấu hình chưa đo vẫn chạy một Wan.</p>
    <label className="field"><span>Baseline</span><select value={baseline} onChange={e=>{setBaseline(e.target.value);setComparison(null);setReview(false);}}><option value="">Chọn lượt đo</option>{samples.map(j=><option key={j.id} value={j.id}>{j.created_at.slice(0,16)} · {j.id.slice(0,8)}</option>)}</select></label>
    <label className="field"><span>Candidate · có thể chọn GPU thay thế để so chi phí</span><select value={candidate} onChange={e=>{setCandidate(e.target.value);setComparison(null);setReview(false);}}><option value="">Chọn lượt đo</option>{samples.filter(j=>j.id!==baseline).map(j=><option key={j.id} value={j.id}>{j.created_at.slice(0,16)} · {j.id.slice(0,8)} · {j.result?.benchmark_report?.policy?.wan_concurrency??1} Wan</option>)}</select></label>
    <button disabled={busy||!baseline||!candidate} onClick={()=>void submit('compare')}>So thời gian và chi phí</button>
    {comparison&&<p>Thông lượng ×{comparison.throughput_gain.toFixed(2)} · tốc độ toàn lượt ×{comparison.stage_wall_speedup.toFixed(2)} · tiết kiệm {comparison.processing_cost_saving==null?'Chưa có giá thuê':`${(comparison.processing_cost_saving*100).toFixed(1)}%`} · {comparison.candidate_eligible?'Đạt điều kiện số đo, còn cần duyệt chất lượng':'Chưa đạt điều kiện'}. {comparison.hardware_changed&&'GPU thay thế chỉ được đề xuất nếu không chậm hơn và tiết kiệm ≥20%; Historia không tự đổi GPU.'}</p>}
    <label className="check"><input type="checkbox" checked={review} onChange={e=>setReview(e.target.checked)}/>Tôi đã xem clip mẫu và chấp nhận chất lượng chuyển động.</label>
    <button disabled={busy||((config.model_residency==='stage'||config.wan_concurrency===2)&&(!review||!baseline||!candidate))} onClick={()=>void submit('apply')}>Lưu hồ sơ ở ranh giới an toàn</button>
    {(message||state.error||jobs.error)&&<p role="status">{message||state.error?.message||jobs.error?.message}</p>}
  </section>;
}
