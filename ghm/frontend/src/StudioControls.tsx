import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

export function DeleteControl({name, description, onDelete, disabled = false}: {name:string; description:string; onDelete:()=>Promise<unknown>; disabled?:boolean}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [typed,setTyped] = useState('');
  const [pending,setPending] = useState(false);
  const [error,setError] = useState('');
  return <><button className="danger" disabled={disabled} onClick={()=>{setTyped('');setError('');dialog.current?.showModal();}}>Xóa {name}</button><dialog ref={dialog} className="confirm-dialog" onCancel={e=>{if(pending)e.preventDefault();}} aria-label={`Xác nhận xóa ${name}`}><form onSubmit={async e=>{e.preventDefault();setPending(true);setError('');try {await onDelete();dialog.current?.close();}catch(err){setError((err as Error).message);}finally{setPending(false);}}}><p className="eyebrow">THAO TÁC KHÔNG THỂ HOÀN TÁC</p><h2>Xóa {name}?</h2><p>{description}</p><div className="alert warning">Sao lưu dữ liệu cần giữ trước khi tiếp tục. Máy chủ sẽ từ chối nếu vẫn còn tác vụ cần xử lý.</div><label className="field"><span>Nhập chính xác <strong>{name}</strong> để xác nhận</span><input autoFocus value={typed} onChange={e=>setTyped(e.target.value)} autoComplete="off" disabled={pending}/></label>{error && <p role="alert" className="warning-text">{error}</p>}<div className="actions"><button type="button" disabled={pending} onClick={()=>dialog.current?.close()}>Giữ lại</button><button className="danger" disabled={pending || typed!==name}>{pending?'Đang xóa…':'Tôi xác nhận xóa'}</button></div></form></dialog></>;
}

export type ProductionRun = {id:string;created_at?:string;status:string;stage:string;error?:string;job_ids:string[];measured_audio_seconds:number;target_duration_seconds:number;duration_review_required:boolean;snapshot:{scenes:{id:string;revision:number;title:string;chapter?:string}[]};checkpoint:{current_job_id?:string;media?:Record<string,{speech_id?:string;keyframe_id?:string;clip_ids?:string[]}>;artifact_ids?:string[];jobs?:Record<string,string>}};
type RunJob={id:string;status:string;kind:string;progress:number;queue_position?:number|null};
export function ProductionRunPanel({production:r,jobs,run,busy}:{production?:ProductionRun;jobs:RunJob[];run:<T>(action:()=>Promise<T>)=>Promise<T|undefined>;busy:boolean}) {
  if(!r)return <section className="panel"><h3>Chưa có lượt sản xuất</h3><p>Duyệt kịch bản ở bước 2 để bắt đầu. Không có tác vụ GPU nào được gửi tự động từ màn hình này.</p></section>;
  const current=jobs.find(j=>j.id===r.checkpoint.current_job_id);
  const sceneKey=Object.entries(r.checkpoint.jobs || {}).find(([,id])=>id===r.checkpoint.current_job_id)?.[0];
  const scene=r.snapshot.scenes.find(s=>s.id===sceneKey?.split(':')[1]);
  const ready=Object.values(r.checkpoint.media || {}).filter(m=>m.clip_ids?.length).length;
  const status:Record<string,string>={abandoned:'Đã bỏ lượt · dự án đã mở khóa, remote chưa rõ',running:'Đang sản xuất',paused:'Đã tạm dừng tại checkpoint',pause_requested:'Đang chờ ranh giới an toàn',duration_review:'Cần duyệt thời lượng thực',failed:'Cần xử lý lỗi',reconciling:'Cần đối chiếu GPU',completed:'Đã ghép phim nháp',superseded:'Đã có bản sửa mới',cancelled:'Đã dừng'};
  const stages:Record<string,string>={speech:'Tạo giọng đọc',keyframe:'Tạo hình ảnh',clip:'Tạo clip',export:'Ghép phim',duration_review:'Kiểm tra thời lượng',completed:'Hoàn tất'};
  const act=(action:string)=>void run(async()=>{const response=await fetch(`/api/studio/production-runs/${r.id}/${action}`,{method:'POST'});if(!response.ok){const e=await response.json().catch(()=>({detail:'Mất kết nối. Đối chiếu lại trạng thái, không gửi lại inference.'}));throw new Error(typeof e.detail==='string'?e.detail:'Không thể cập nhật lượt sản xuất.');}return response.json();});
  return <section className="panel production-panel" aria-label="Tiến độ toàn bộ phim"><h2>{status[r.status] || r.status}</h2><div role="status" aria-live="polite"><strong>{stages[r.stage] || r.stage}</strong><p>{scene?`${scene.chapter || ''} · ${scene.title}`:'Theo dõi checkpoint sản xuất'} · {ready}/{r.snapshot.scenes.length} cảnh đã có clip</p>{current?.status==='queued' && <p>Hàng đợi GPU {current.queue_position!=null?`· vị trí #${current.queue_position}`:'· chờ máy sẵn sàng'}</p>}<progress max={Math.max(1,r.snapshot.scenes.length)} value={ready} aria-label="Số cảnh có clip"/>{current?.status==='running' && <p>Tác vụ hiện tại: {current.progress?`${Math.round(current.progress)}%`:'đang xử lý'}</p>}</div>{r.error && <p role="alert" className="warning-text">{r.error}</p>}<p>Giọng đọc đo thực tế: {r.measured_audio_seconds.toFixed(1)} giây / mục tiêu {r.target_duration_seconds} giây.</p>{r.duration_review_required && <div className="alert warning"><div><strong>Thời lượng lời đọc lệch mục tiêu</strong><p>Đã dừng trước bước tạo clip. Chấp nhận {r.measured_audio_seconds.toFixed(1)} giây hoặc tạm dừng rồi sửa lời đọc; không lặp / kéo chậm clip để bù.</p><button disabled={busy} onClick={()=>act('accept-duration')}>Chấp nhận thời lượng đo & tiếp tục</button></div></div>}<div className="actions">{current && ['reconciling','failed','interrupted','paused'].includes(current.status) && r.status!=='abandoned' && <button disabled={busy} onClick={()=>{if(!window.confirm('Bỏ lượt trên GPU cũ & mở khóa dự án? Trạng thái remote chưa rõ. KHÔNG xác nhận GPU đã dừng và KHÔNG dừng tiền thuê GPU. Giữ snapshot, media và bằng chứng; đóng lượt và bước chưa chạy. GPU cũ vẫn bị chặn. Không tự chuyển GPU hoặc chạy lại. Xác nhận?'))return;void run(async()=>{const response=await fetch(`/api/studio/jobs/${current.id}/abandon`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:true,remote_state_unknown:true})});if(!response.ok){const e=await response.json();throw new Error(e.detail || 'Không thể bỏ lượt.');}return response.json();});}}>Bỏ lượt trên GPU cũ & mở khóa dự án</button>}{['running','duration_review'].includes(r.status) && <button disabled={busy} onClick={()=>act('pause')}>Yêu cầu tạm dừng an toàn</button>}{['paused','reconciling','failed'].includes(r.status) && <button disabled={busy} onClick={()=>act('resume')}>{r.status==='failed'?'Thử lại bước lỗi · giữ checkpoint':'Đối chiếu & tiếp tục'}</button>}</div><p className="action-help">Tạm dừng không đồng nghĩa tiến trình remote đã dừng ngay; chờ checkpoint an toàn. Mất kết nối cần đối chiếu trước khi tiếp tục, không tự gửi lại inference. Tiền thuê Pod vẫn tiếp tục.</p><details><summary>Nhật ký kỹ thuật theo lượt sản xuất</summary>{r.job_ids.map(id=><div key={id}><small>{jobs.find(j=>j.id===id)?.kind || 'Tác vụ'} · {id.slice(0,8)}</small><JobEvents id={id} active={['queued','running','reconciling','cancelling'].includes(jobs.find(j=>j.id===id)?.status || '')}/></div>)}</details></section>;
}

type Event = {id:number;message:string;level?:string;created_at?:string};
export function JobEvents({id, active}: {id:string;active:boolean}) {
  const [open,setOpen]=useState(false);
  const [events,setEvents]=useState<Event[]>([]);
  const cursor=useRef(0);
  const query=useQuery({queryKey:['live-job-events',id],gcTime:0,enabled:open,refetchInterval:open && active?2000:false,queryFn:async()=>{
    const result:Event[]=[];
    let after=cursor.current;
    for(let page=0;page<20;page++) {
      const response=await fetch(`/api/studio/jobs/${id}/events?after=${after}`);
      if(!response.ok) throw new Error('Không tải được nhật ký. Thử lại; tác vụ vẫn được giữ trên máy chủ.');
      const rows=await response.json() as Event[];
      result.push(...rows);
      if(rows.length<500)break;
      const next=Math.max(...rows.map(e=>e.id));
      if(next<=after)break;
      after=next;
    }
    return result;
  }});
  useEffect(()=>{if(!query.data?.length)return;cursor.current=Math.max(cursor.current,...query.data.map(e=>e.id));setEvents(previous=>Array.from(new Map([...previous,...query.data!].map(e=>[e.id,e])).values()).slice(-1000));},[query.data]);
  return <details className="job-events" open={open} onToggle={e=>setOpen(e.currentTarget.open)}><summary>Nhật ký tác vụ {active?'· cập nhật mỗi 2 giây':''} {events.length?`· ${events.length} dòng`:''}</summary>{query.error && <div role="alert">{query.error.message} <button onClick={()=>void query.refetch()}>Thử lại</button></div>}<pre className="install-log" tabIndex={0} aria-label="Nhật ký tác vụ">{events.map(e=>`${e.created_at?new Date(e.created_at).toLocaleTimeString('vi-VN')+' ':''}${e.level?'['+e.level+'] ':''}${e.message}`).join('\n') || (query.isFetching?'Đang tải nhật ký…':'Chưa có sự kiện. Tác vụ trong hàng đợi sẽ bắt đầu khi GPU sẵn sàng.')}</pre><small>Hiển thị tối đa 1.000 dòng đã tải; nội dung được giữ khi thu gọn nhật ký.</small></details>;
}
