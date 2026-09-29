import { useRef, useState } from 'react';
import Dialog from './Dialog';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import LogWorkspace from './LogWorkspace';
import { PendingClipSettings } from './PerformancePanel';
import { media, names, read, useLocationState, navigate, type StudioJob } from './workspace';

export default function TaskCenter({jobs}:{jobs:StudioJob[]}) {
  const params=useLocationState();
  const client=useQueryClient();
  const [pending,setPending]=useState(false);
  const [error,setError]=useState('');
  const query=useQuery({queryKey:['task-hosts'],queryFn:()=>read<{id:string;label:string}[]>('/api/hosts')});
  const projects=useQuery({queryKey:['task-projects'],queryFn:()=>read<{id:string;title:string}[]>('/api/studio/projects')});
  const selected=jobs.find(j=>j.id===params.get('task'));
  const pendingRef=useRef(false);
  const log=params.get('task_log')==='1';
  const closeTask=()=>navigate({task:null,task_log:null,log_job:null,log_scene:null,log_stage:null,log_kind:null,log_level:null,log_q:null});
  const [output,setOutput]=useState('');
  const artifactQuery=useQuery({queryKey:['task-artifacts',selected?.project_id],enabled:!!selected?.project_id,queryFn:()=>read<{id:string;name:string;media_type:string;job_id?:string}[]>(`/api/studio/projects/${selected!.project_id}/artifacts`)});
  const artifacts=(artifactQuery.data||[]).filter(a=>a.job_id===selected?.id);
  const clipPreview=artifacts.find(a=>a.media_type.startsWith('video/')&&a.name.startsWith('clip-'));
  const selectedArtifact=artifacts.find(a=>a.id===output);
  const text=params.get('task_q')||'';
  const state=params.get('task_status')||'';
  const kind=params.get('task_kind')||'';
  const gpu=params.get('task_gpu')||'';
  const rank=(j:StudioJob)=>['failed','reconciling','interrupted'].includes(j.status)?0:['running','cancelling'].includes(j.status)?1:j.status==='queued'?2:j.status==='paused'?3:4;
  const rows=jobs.filter(j=>(!state||j.status===state)&&(!kind||j.kind===kind)&&(!gpu||j.host_id===gpu)&&`${j.snapshot?.project?.title} ${j.snapshot?.scene?.title} ${names[j.kind]} ${j.id} ${j.project_id}`.toLocaleLowerCase().includes(text.toLocaleLowerCase())).sort((a,b)=>rank(a)-rank(b)||Date.parse(b.created_at)-Date.parse(a.created_at));
  const [limit,setLimit]=useState(50);
  const select=(j:StudioJob,showLog=false)=>{navigate({task:j.id,task_log:showLog?'1':null,log_job:null,log_scene:null,log_stage:null,log_kind:null,log_level:null,log_q:null});setOutput('');};
  const action=async(job:StudioJob,suffix:string)=>{
    if(pendingRef.current)return;
    if(suffix==='abandon'&&!window.confirm('Bỏ lượt và mở khóa dự án? Trạng thái GPU có thể chưa rõ. Giữ dữ liệu đã lưu; thao tác này không xác nhận GPU đã dừng và không dừng tiền thuê.'))return;
    pendingRef.current=true;setPending(true);setError('');
    const production=job.snapshot?.production_run_id;
    const url=production&&['resume','pause'].includes(suffix)?`/api/studio/production-runs/${production}/${suffix}`:`/api/studio/jobs/${job.id}/${suffix}`;
    try {
      const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(suffix==='abandon'?{confirmed:true,remote_state_unknown:true}:{})});
      if(!response.ok){const body=await response.json();throw new Error(typeof body.detail==='string'?body.detail:'Không thể thực hiện thao tác.');}
      await client.invalidateQueries();
    }catch(e){setError(`${job.snapshot?.scene?.title||names[job.kind]||job.id} (${job.id}): ${(e as Error).message}`);}finally{pendingRef.current=false;setPending(false);}
  };
  return <section className="editing-workspace task-workspace" aria-label="Trung tâm tác vụ"><header className="work-header"><div><span className="work-kicker">HÀNG ĐỢI & KẾT QUẢ</span><h2>Tác vụ</h2><p>{jobs.filter(j=>['running','queued'].includes(j.status)).length} tác vụ đang chạy hoặc chờ · {jobs.filter(j=>rank(j)===0).length} cần xử lý</p></div></header>{error&&<div role="alert" className="work-error-box">{error}</div>}<div className="log-filters"><label>Tìm dự án / cảnh<input value={text} onChange={e=>navigate({task_q:e.target.value},true)} placeholder="Tên dự án, cảnh hoặc ID…"/></label><label>Trạng thái<select value={state} onChange={e=>navigate({task_status:e.target.value})}><option value="">Tất cả trạng thái</option>{Array.from(new Set(jobs.map(j=>j.status))).map(s=><option key={s} value={s}>{names[s]||s}</option>)}</select></label><label>Công đoạn<select value={kind} onChange={e=>navigate({task_kind:e.target.value})}><option value="">Tất cả loại</option>{Array.from(new Set(jobs.map(j=>j.kind))).map(s=><option key={s} value={s}>{names[s]||s}</option>)}</select></label><label>GPU<select value={gpu} onChange={e=>navigate({task_gpu:e.target.value})}><option value="">Tất cả GPU</option>{query.data?.map(h=><option key={h.id} value={h.id}>{h.label}</option>)}</select></label></div><div className="task-table-scroll"><table className="task-table"><thead><tr><th>Dự án / cảnh</th><th>Công đoạn</th><th>Trạng thái</th><th>Tiến độ</th><th>Thời gian</th><th>GPU</th><th>Thao tác</th></tr></thead><tbody>{rows.slice(0,limit).map(j=><tr key={j.id} className={selected?.id===j.id?'selected':''}><td><button className="task-title" onClick={()=>select(j)}>{selected?.id===j.id&&clipPreview&&<img className="task-preview" loading="lazy" src={`/api/studio/artifacts/${clipPreview.id}/thumbnail`} alt=""/>}<span>{(j.snapshot?.scene?.title||j.snapshot?.project?.title||projects.data?.find(p=>p.id===j.project_id)?.title||names[j.kind]).normalize('NFC')}<small>{j.snapshot?.scene?j.snapshot?.project?.title?.normalize('NFC'):''}</small></span></button></td><td>{names[j.kind]||j.kind}</td><td><span className={'status-chip status-'+j.status}><span aria-hidden="true">●</span>{names[j.status]||j.status}</span></td><td>{j.status==='queued'?`Chờ ${j.queue_position!=null?'#'+j.queue_position:''}`:<div className="task-progress"><span>{j.progress?`${Math.round(j.progress)}%`:'Chưa đo'}</span><div className="progress-track" role="progressbar" aria-label={`Tiến độ ${j.snapshot?.scene?.title||names[j.kind]}`} aria-valuenow={Math.min(100,Math.max(0,j.progress||0))} aria-valuemin={0} aria-valuemax={100}><span style={{width:`${Math.min(100,Math.max(0,j.progress||0))}%`}}/></div></div>}</td><td>{new Date(j.created_at).toLocaleString('vi-VN')}</td><td>{query.data?.find(h=>h.id===j.host_id)?.label||'Local'}</td><td><div className="task-row-actions"><button onClick={()=>select(j,true)} aria-label="Xem log dạng popup ↗">Log ↗</button>{(['interrupted','reconciling','paused'].includes(j.status)||(j.kind==='install'&&j.status==='failed'))&&<button disabled={pending} onClick={()=>void action(j,'resume')} title="Đối chiếu & tiếp tục">Tiếp tục</button>}{j.kind==='clip'&&['queued','running'].includes(j.status)&&<button disabled={pending} onClick={()=>void action(j,'pause')} title="Tạm dừng cuối shot">Tạm dừng</button>}{['queued','running','reconciling'].includes(j.status)&&<button disabled={pending} onClick={()=>void action(j,'cancel')}>{j.status==='queued'?'Rút hàng đợi':'Dừng an toàn'}</button>}{['reconciling','failed','interrupted','paused'].includes(j.status)&&['outline','script','speech','keyframe','clip'].includes(j.kind)&&<button className="task-abandon" disabled={pending} onClick={()=>void action(j,'abandon')} title="Bỏ lượt & mở khóa dự án">Bỏ lượt</button>}</div></td></tr>)}</tbody></table>{!rows.length&&<p className="work-empty">Không có tác vụ phù hợp.</p>}</div>{rows.length>limit&&<button onClick={()=>setLimit(n=>n+50)}>Xem thêm tác vụ</button>}
    {selected&&!log&&<Dialog title={`Chi tiết · ${selected.snapshot?.scene?.title||names[selected.kind]}`} description={`Tác vụ ${selected.id}`} onClose={closeTask} className="studio-task-dialog"><div className="editing-workspace task-detail" role="region" aria-label="Chi tiết tác vụ"><div className="work-actions"><button onClick={()=>void navigator.clipboard.writeText(selected.id).catch(()=>setError('Không truy cập được clipboard.'))}>Copy ID</button></div>{(error||selected.error)&&<div role="alert" className="work-error-box">{error||selected.error}</div>}{selected.kind==='clip'&&selected.status==='paused'&&!selected.snapshot?.production_run_id&&<PendingClipSettings url={`/api/studio/jobs/${selected.id}/pending-clip-config`}/>}
      {clipPreview&&<div className="task-featured-frame"><img loading="lazy" src={`/api/studio/artifacts/${clipPreview.id}/thumbnail`} alt={`Khung hình từ clip đã lưu của ${selected.snapshot?.scene?.title||'tác vụ'}`}/><span>Khung hình trích từ clip đã lưu · không phải ảnh tạo giả</span></div>}
      <div className="artifact-list">{!selected.project_id&&selected.result?.artifact_ids?.map((id,i)=><a key={id} href={media(id)} target="_blank" rel="noreferrer">Kết quả {i+1} ↗</a>)}{artifacts.map(a=><div key={a.id}><button onClick={()=>setOutput(a.id)}>{a.name}</button><a href={media(a.id)+'?download=true'}>Tải ↓</a></div>)}</div>{selectedArtifact?.media_type.startsWith('video/')&&<video key={output} controls preload="metadata" src={media(output)}/>} {selectedArtifact?.media_type.startsWith('image/')&&<img src={media(output)} alt={selectedArtifact.name}/>} {selectedArtifact?.media_type.startsWith('audio/')&&<audio controls src={media(output)}/>}
    </div></Dialog>}
    {selected&&log&&<Dialog title={`Nhật ký · ${selected.snapshot?.scene?.title||names[selected.kind]||selected.kind}`}
      description={`Tác vụ ${selected.id} · ${names[selected.status]||selected.status}`}
      onClose={closeTask} className="studio-log-dialog">
      <LogWorkspace key={selected.id} jobId={selected.id} jobs={[selected]}/>
    </Dialog>}
  </section>;
}
