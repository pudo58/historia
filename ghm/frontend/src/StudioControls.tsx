import { useRef, useState } from 'react';
import { navigate } from './workspace';

export function DeleteControl({name, description, onDelete, disabled = false}: {name:string; description:string; onDelete:()=>Promise<unknown>; disabled?:boolean}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [typed,setTyped] = useState('');
  const [pending,setPending] = useState(false);
  const [error,setError] = useState('');
  return <><button className="danger" disabled={disabled} onClick={()=>{setTyped('');setError('');dialog.current?.showModal();}}>Xóa {name}</button><dialog ref={dialog} className="confirm-dialog" onCancel={e=>{if(pending)e.preventDefault();}} aria-label={`Xác nhận xóa ${name}`}><form onSubmit={async e=>{e.preventDefault();setPending(true);setError('');try {await onDelete();dialog.current?.close();}catch(err){setError((err as Error).message);}finally{setPending(false);}}}><p className="eyebrow">THAO TÁC KHÔNG THỂ HOÀN TÁC</p><h2>Xóa {name}?</h2><p>{description}</p><div className="alert warning">Sao lưu dữ liệu cần giữ trước khi tiếp tục. Máy chủ sẽ từ chối nếu vẫn còn tác vụ cần xử lý.</div><label className="field"><span>Nhập chính xác <strong>{name}</strong> để xác nhận</span><input autoFocus value={typed} onChange={e=>setTyped(e.target.value)} autoComplete="off" disabled={pending}/></label>{error && <p role="alert" className="warning-text">{error}</p>}<div className="actions"><button type="button" disabled={pending} onClick={()=>dialog.current?.close()}>Giữ lại</button><button className="danger" disabled={pending || typed!==name}>{pending?'Đang xóa…':'Tôi xác nhận xóa'}</button></div></form></dialog></>;
}

export type ProductionRun = {id:string;created_at?:string;status:string;stage:string;error?:string;job_ids:string[];measured_audio_seconds:number;target_duration_seconds:number;duration_review_required:boolean;snapshot:{frame_interpolation?:string;scenes:{id:string;revision:number;title:string;chapter?:string}[]};checkpoint:{current_job_id?:string;review_scene_id?:string;media?:Record<string,{speech_id?:string;keyframe_id?:string;shot_keyframes?:string[];shot_keyframes_approved?:boolean;clip_ids?:string[];rife_clip_ids?:string[]}>;artifact_ids?:string[];jobs?:Record<string,string>}};
export function JobEvents({id, active}: {id:string;active:boolean}) {
  return <button onClick={()=>navigate({page:'jobs',task:id,task_log:'1',log_job:null,log_scene:null,log_stage:null,run:null})}>Nhật ký {active?'· cập nhật trực tiếp':'↗'}</button>;
}
