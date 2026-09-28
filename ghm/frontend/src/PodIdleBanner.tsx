import {useQuery} from '@tanstack/react-query';
import {read} from './workspace';

type Advice={state:'unknown'|'busy'|'recent'|'idle';reason:string;idle_seconds?:number;hourly_usd?:number|null;next_ten_minutes_usd?:number|null};
export default function PodIdleBanner({projectId}:{projectId:string}){
  const query=useQuery({queryKey:['pod-idle',projectId],queryFn:()=>read<Advice>(`/api/studio/projects/${projectId}/pod-idle`),refetchInterval:60000});
  if(query.error)return <p className="work-footnote">Chưa xác minh được queue ComfyUI; không thể xác nhận Pod nhàn rỗi.</p>;
  const value=query.data;
  if(!value||value.state==='busy'||value.state==='recent')return null;
  if(value.state==='unknown')return <p className="work-footnote">{value.reason}</p>;
  return <aside className="work-error-box" role="status"><strong>Pod nhàn rỗi hơn 10 phút.</strong> {value.reason} Nếu không còn việc khác trên Pod, mở RunPod để dừng Pod. {value.hourly_usd!=null&&<>Giá bạn nhập: ${value.hourly_usd.toFixed(2)}/giờ; thêm 10 phút khoảng ${value.next_ten_minutes_usd!.toFixed(2)} tiền thuê Pod.</>} <a href="https://www.runpod.io/console/pods" target="_blank" rel="noreferrer">Mở RunPod ↗</a></aside>;
}
