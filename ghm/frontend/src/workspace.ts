import { useSyncExternalStore } from 'react';

const subscribe = (callback:()=>void) => {
  window.addEventListener('popstate',callback);
  return ()=>window.removeEventListener('popstate',callback);
};
export function useLocationState() {
  const search=useSyncExternalStore(subscribe,()=>window.location.search);
  return new URLSearchParams(search);
}
export function navigate(values:Record<string,string|null>, replace=false) {
  const url=new URL(window.location.href);
  for(const [key,value] of Object.entries(values)) {
    if(value===null||value==='')url.searchParams.delete(key);else url.searchParams.set(key,value);
  }
  window.history[replace?'replaceState':'pushState']({},'',url);
  window.dispatchEvent(new PopStateEvent('popstate'));
}
export async function read<T>(url:string):Promise<T> {
  const response=await fetch(url);
  if(!response.ok)throw new Error('Không tải được dữ liệu. Kết quả đã lưu vẫn được giữ.');
  return response.json();
}
export const media=(id:string)=>`/api/studio/artifacts/${id}/file`;
export const activeStatuses=['queued','running','pause_requested','cancelling','reconciling'];
export const names:Record<string,string>={queued:'Đang chờ',running:'Đang xử lý',completed:'Hoàn tất',failed:'Cần xử lý',paused:'Đã tạm dừng',pause_requested:'Đang chờ cuối shot',interrupted:'Bị gián đoạn',reconciling:'Cần đối chiếu GPU',cancelling:'Đang dừng',cancelled:'Đã dừng',abandoned:'Đã bỏ lượt',superseded:'Đã có bản mới',pending:'Chưa gửi',submitting:'Đang gửi',submitted:'Đang xử lý',remote_completed:'Đang tải',downloaded:'Đã lưu',speech:'Giọng đọc',keyframe:'Tạo ảnh',clip:'Tạo clip',rife:'Nội suy RIFE',export:'Ghép phim',optional_model:'Cài model tùy chọn',dialogue_test:'Thử nhân vật nói',voice_audition:'Nghe thử giọng',install:'Cài bộ AI',verify:'Kiểm chứng',video_test:'Clip thử',runtime:'Runtime GPU',benchmark:'Benchmark',script:'Kịch bản',outline:'Dàn ý',duration_review:'Duyệt thời lượng',keyframe_review:'Chờ duyệt ảnh keyframe'};
export const duration=(value:number|null|undefined)=>value==null?'Chưa có số đo':`${Math.floor(value/60).toString().padStart(2,'0')}:${Math.floor(value%60).toString().padStart(2,'0')}`;
export type StudioJob={id:string;project_id?:string;scene_id?:string;host_id?:string;kind:string;status:string;progress:number;error?:string;created_at:string;queue_position?:number|null;snapshot?:{production_run_id?:string;scene?:{id:string;title:string};project?:{title:string}};result?:{artifact_ids?:string[];[key:string]:unknown}};
export type LogContext={job?:string;scene?:string;stage?:string};
export function openLog(project:string,context:LogContext={}) {
  navigate({page:'projects',project,tab:'logs',log_job:context.job||null,log_scene:context.scene||null,log_stage:context.stage||null,log_q:null,log_level:null,log_kind:null,log_view:'activity'});
}
