import {useQuery} from '@tanstack/react-query';
import {read} from './workspace';
import {useRunPod, money, uptime} from './RunPodMonitor';

type Advice={state:'unknown'|'busy'|'recent'|'idle';reason:string;idle_seconds?:number;host_id?:string;hourly_usd?:number|null;next_ten_minutes_usd?:number|null};
export default function PodIdleBanner({projectId}:{projectId:string}){
  const query=useQuery({queryKey:['pod-idle',projectId],queryFn:()=>read<Advice>(`/api/studio/projects/${projectId}/pod-idle`),refetchInterval:60000});
  const pods=useRunPod().data;
  if(query.error)return <p className="work-footnote">Chưa xác minh được queue ComfyUI; không thể xác nhận Pod nhàn rỗi.</p>;
  const value=query.data;
  if(!value||value.state==='busy'||value.state==='recent')return null;
  if(value.state==='unknown')return <p className="work-footnote">{value.reason}</p>;
  const pod=pods?.configured&&!pods.error?pods.pods.find(p=>p.status==='RUNNING'&&!!value.host_id&&(p.host_ids||[p.host_id]).includes(value.host_id)):undefined;
  const rate=pod?.cost_per_hr??value.hourly_usd;
  return <aside className="work-error-box" role="status"><strong>Pod nhàn rỗi hơn 10 phút.</strong> {value.reason} Nếu không còn việc khác trên Pod, mở RunPod để dừng Pod. {pod&&<>Pod {pod.name||pod.id} đã chạy {uptime(pod.uptime_seconds)}, đã tốn {money(pod.session_cost)}. </>}{rate!=null&&<>{pod?'Giá RunPod':'Giá bạn nhập'}: {money(rate)}/giờ; thêm 10 phút khoảng {money(rate/6)} tiền thuê Pod.</>} <a href="https://www.runpod.io/console/pods" target="_blank" rel="noreferrer">Mở RunPod ↗</a></aside>;
}
