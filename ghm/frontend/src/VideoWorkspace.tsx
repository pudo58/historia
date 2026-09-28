import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { PendingClipSettings, BenchmarkControl } from './PerformancePanel';
import PodIdleBanner from './PodIdleBanner';
import { type ProductionRun } from './StudioControls';
import { duration, media, names, navigate, openLog, read, useLocationState, type StudioJob } from './workspace';

type Config={width:number;height:number;frames:number;fps:number;steps:number;shot_seconds:number};
type Shot={index:number;stage:string;job_id?:string;artifact_id?:string;state:string;config:Config;timing:Record<string,number>};
type Scene={scene_id:string;title:string;audio_seconds:number|null;shot_count:number|null;completed_shots:number;keyframe_id?:string;job_id?:string;status:string;shots:Shot[];configs:Config[]};
type Performance={scenes:Scene[];completed_shots:number;remaining_shots:number;unmeasured_scenes:number;eta_seconds:number|null;estimated_remaining_usd:number|null;measured_audio_seconds:number};
const positions=new Map<string,number>();
function Player({id}:{id:string}) {
  const ref=useRef<HTMLVideoElement>(null);
  const initialTime=useRef(positions.get(id)||0);
  const [failed,setFailed]=useState(false);
  if(failed)return <div className="work-empty"><strong>Chưa đọc được video đã lưu</strong><small>Kiểm tra kết nối hoặc file. Thao tác này không tạo lại clip.</small><button onClick={()=>setFailed(false)}>Tải lại video</button></div>;
  return <video ref={ref} controls preload="metadata" src={media(id)} onError={()=>setFailed(true)} onTimeUpdate={e=>{if(e.currentTarget.readyState>0)positions.set(id,e.currentTarget.currentTime);}} onLoadedMetadata={e=>{e.currentTarget.currentTime=initialTime.current;}}/>;
}
export default function VideoWorkspace({projectId,jobs,runs,loadingRuns,editScene}:{projectId:string;jobs:StudioJob[];runs:ProductionRun[];loadingRuns:boolean;editScene:(id:string)=>void}) {
  const params=useLocationState();
  const client=useQueryClient();
  const production=runs.find(r=>r.id===params.get('run'))||runs[0];
  const query=useQuery({queryKey:['video-performance',projectId,production?.id],queryFn:()=>read<Performance>(`/api/studio/projects/${projectId}/performance${production?'?run_id='+production.id:''}`),refetchInterval:4000});
  const [search,setSearch]=useState('');
  const [filter,setFilter]=useState('all');
  const [inspector,setInspector]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const p=query.data;
  const current=jobs.find(j=>j.id===production?.checkpoint.current_job_id);
  const chainJob=(current?.result?.chain_pending_index!=null?current:jobs.find(j=>j.kind==='clip'&&j.status==='paused'&&j.result?.chain_pending_index!=null&&j.scene_id===params.get('scene')));
  const chainIndex=chainJob?.result?.chain_pending_index as number|undefined;
  const chainFrame=chainIndex==null?undefined:(chainJob?.result?.chain_frames as Record<string,string>|undefined)?.[String(chainIndex)];
  const reviewScene=production?.checkpoint.review_scene_id;
  const reviewImages=reviewScene?production?.checkpoint.media?.[reviewScene]?.shot_keyframes:undefined;
  const aiReview=reviewScene?jobs.find(j=>j.kind==='image_review'&&j.scene_id===reviewScene):undefined;
  const filmIds=production?.checkpoint.artifact_ids||(!production?jobs.find(j=>j.kind==='export'&&j.status==='completed')?.result?.artifact_ids:undefined)||[];
  const scenes=p?.scenes||[];
  const recent=[...scenes].reverse().find(s=>s.shots.some(shot=>shot.artifact_id));
  const selected=scenes.find(s=>s.scene_id===params.get('scene'))||recent||scenes[0];
  const shot=(params.has('shot')?selected?.shots.find(s=>s.index===Number(params.get('shot'))):undefined)||[...(selected?.shots||[])].reverse().find(s=>s.artifact_id)||selected?.shots[0];
  const mode=params.get('media')||(filmIds.length?'film':'clip');
  const id=mode==='film'?filmIds[0]:shot?.artifact_id;
  // Freeze the initial selection once media is available; polling must not change playback.
  useEffect(()=>{
    if(!loadingRuns&&query.isSuccess&&!params.has('media')&&(filmIds.length||recent))navigate({run:production?.id||null,media:mode,scene:selected?.scene_id||null,shot:shot?String(shot.index):null},true);
  },[loadingRuns,query.isSuccess,params,production?.id,filmIds.length,recent,mode,selected,shot]);
  const act=async(action:string)=>{
    if(!production||busy)return;
    setBusy(true);setError('');
    try {
      const response=await fetch(`/api/studio/production-runs/${production.id}/${action}`,{method:'POST'});
      if(!response.ok){const data=await response.json();throw new Error(data.detail||'Không thể cập nhật lượt sản xuất.');}
      await client.invalidateQueries();
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  };
  const approve=async(url:string,body:object)=>{
    if(busy)return;
    setBusy(true);setError('');
    try{
      const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!response.ok){const data=await response.json();throw new Error(data.detail||'Không duyệt được ảnh.');}
      await client.invalidateQueries();
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  };
  const selectScene=(s:Scene)=>navigate({scene:s.scene_id,shot:String(s.shots.find(shot=>shot.artifact_id)?.index||0),media:'clip'});
  const visible=scenes.filter(s=>s.title.toLocaleLowerCase().includes(search.toLocaleLowerCase())&&(filter==='all'||(filter==='missing'?s.completed_shots!==(s.shot_count||-1):['failed','reconciling','interrupted'].includes(s.status))));
  const log=()=>{navigate({run:production?.id||null},true);openLog(projectId,{scene:selected?.scene_id,job:shot?.job_id||selected?.job_id,stage:shot?.stage});};
  return <section className="editing-workspace video-workspace" aria-label="Không gian Video">
    <PodIdleBanner projectId={projectId}/>
    <header className="work-header"><div><span className="work-kicker">PHÒNG DỰNG / VIDEO</span><h2>{names[production?.status||'']||'Sẵn sàng xem kết quả'}</h2><div className="stage-track">{(production?.snapshot.frame_interpolation==='rife24'?['speech','keyframe','clip','rife','export']:['speech','keyframe','clip','export']).map((s,i)=><span key={s} className={production?.stage===s?'current':''}>{i+1}. {names[s]}</span>)}</div></div><div className="work-actions"><label className="sr-only" htmlFor="video-run">Lượt sản xuất</label><select id="video-run" value={production?.id||''} onChange={e=>navigate({run:e.target.value,scene:null,shot:null,media:null})}>{!runs.length&&<option value="">Tác vụ thủ công</option>}{runs.map((r,i)=><option key={r.id} value={r.id}>Lượt {runs.length-i} · {names[r.status]||r.status}</option>)}</select><button onClick={()=>{navigate({run:production?.id||null},true);openLog(projectId);}}>Nhật ký ↗</button>{production&&['running','duration_review'].includes(production.status)&&<button disabled={busy} onClick={()=>void act('pause')}>Tạm dừng</button>}{production&&['paused','failed','reconciling'].includes(production.status)&&<button className="work-primary" disabled={busy} onClick={()=>void act('resume')}>Đối chiếu & tiếp tục</button>}</div></header>
    <div className="render-summary"><span><strong>{p?.completed_shots??'—'}/{p?p.completed_shots+p.remaining_shots:'—'}</strong> shot đã lưu {p?.unmeasured_scenes?`· ${p.unmeasured_scenes} cảnh chờ audio`:''}</span><span>Audio <strong>{duration(p?.measured_audio_seconds)}</strong></span><span>ETA <strong>{p?.eta_seconds==null?'Đang thu thập số đo':duration(p.eta_seconds)}</strong></span><span>Xử lý còn lại <strong>{p?.estimated_remaining_usd==null?'Chưa có ước tính':`$${p.estimated_remaining_usd.toFixed(2)}`}</strong></span></div>
    <div className="render-now"><span>{current?`Đang sản xuất: ${current.snapshot?.scene?.title||names[current.kind]} · ${names[current.status]}`:'Chọn cảnh để xem clip hoặc phim đã ghép.'}</span>{current?.scene_id&&<button onClick={()=>{const s=scenes.find(s=>s.scene_id===current.scene_id);if(s)selectScene(s);}}>Đến cảnh đang chạy</button>}</div>
    {(error||query.error||production?.error)&&<div role="alert" className="work-error-box">{error||query.error?.message||production?.error}<button onClick={log}>Xem nhật ký</button></div>}
    {production?.duration_review_required&&<div className="work-error-box">Lời đọc thực tế {duration(production.measured_audio_seconds)}; mục tiêu {duration(production.target_duration_seconds)}. <button disabled={busy} onClick={()=>void act('accept-duration')}>Chấp nhận thời lượng & tiếp tục</button></div>}
    {reviewScene&&reviewImages&&<section className="work-error-box" aria-label="Duyệt ảnh từng shot"><h3>Duyệt toàn bộ {reviewImages.length} ảnh của cảnh {production?.snapshot.scenes.find(s=>s.id===reviewScene)?.title}</h3><p>Wan sẽ chờ cho tới khi bạn xem và duyệt cả bộ ảnh. Ảnh đã lưu không bị tạo lại.</p><div className="review-images">{reviewImages.map((image,index)=><a key={image} href={media(image)} target="_blank" rel="noreferrer"><img src={media(image)} alt={`Ảnh shot ${index+1}`}/><span>Shot {index+1}</span></a>)}</div><button disabled={busy||!!aiReview&&['queued','running','reconciling'].includes(aiReview.status)} onClick={()=>void approve(`/api/studio/projects/${projectId}/jobs`,{kind:'image_review',scene_id:reviewScene})}>Nhờ Qwen3-VL gắn cờ ảnh nghi lỗi · tùy chọn</button>{aiReview&&<p>Kiểm ảnh AI: {names[aiReview.status]||aiReview.status}</p>}{aiReview?.result?.review_output!=null&&<pre className="review-output">{JSON.stringify(aiReview.result.review_output,null,2)}</pre>}<button disabled={busy} onClick={()=>void approve(`/api/studio/production-runs/${production!.id}/approve-keyframes`,{scene_id:reviewScene})}>Tôi đã xem và duyệt toàn bộ ảnh</button></section>}
    {chainJob&&chainFrame&&chainIndex!=null&&<section className="work-error-box" aria-label="Duyệt ảnh nối"><h3>Ảnh nối cho shot {chainIndex+1}</h3><p>Frame cuối của shot trước đã được lưu. Duyệt trước khi gửi shot Wan kế tiếp.</p><a href={media(chainFrame)} target="_blank" rel="noreferrer"><img className="chain-review-image" src={media(chainFrame)} alt="Frame nối từ clip trước"/></a><button disabled={busy} onClick={()=>void approve(`/api/studio/jobs/${chainJob.id}/approve-chain-frame`,{index:chainIndex,artifact_id:chainFrame})}>Duyệt frame nối</button><small>Sau khi duyệt, bấm Đối chiếu & tiếp tục.</small></section>}
    <div className="video-layout"><div className="player-column"><nav className="work-tabs" aria-label="Loại video"><button aria-pressed={mode==='film'} onClick={()=>navigate({media:'film'})}>Phim hoàn chỉnh</button><button aria-pressed={mode==='clip'} onClick={()=>navigate({media:'clip'})}>Clip của cảnh</button></nav><div className="film-screen">{id?<Player key={id} id={id}/>:<div className="work-empty"><span className="play-outline">▷</span><strong>{mode==='film'?'Phim sẽ xuất hiện khi ghép xong':'Shot này chưa có clip đã lưu'}</strong><small>{shot?names[shot.state]:'Giữ nguyên kết quả đã xong; chờ công đoạn hiện tại.'}</small></div>}</div><div className="player-caption"><div><strong>{mode==='film'?'Phim nháp · '+(production?.created_at?new Date(production.created_at).toLocaleString('vi-VN'):'Kết quả gần nhất'):selected?`${selected.title} · Shot ${(shot?.index||0)+1}`:'Chưa có cảnh'}</strong><small>{mode==='film'?'Xem và kiểm chứng trước khi sử dụng':'Clip trung gian · chưa ghép lời đọc và phụ đề'}</small></div>{id&&<a className="work-button" href={media(id)+'?download=true'}>Tải MP4 ↓</a>}{mode==='film'&&filmIds.length>1&&<details className="download-menu"><summary>Tệp khác</summary>{filmIds.slice(1).map((a,i)=><a key={a} href={media(a)+'?download=true'}>{['Phụ đề SRT','Metadata','Cấu hình','Kịch bản'][i]||`Tệp ${i+2}`}</a>)}</details>}</div>
    <div className="shot-strip" aria-label="Các shot của cảnh">{selected?.shots.map(s=><button key={s.index} aria-pressed={mode==='clip'&&shot?.index===s.index} onClick={()=>navigate({media:'clip',scene:selected.scene_id,shot:String(s.index)})}><span>SHOT {String(s.index+1).padStart(2,'0')}</span><strong className={'status-'+s.state}>{names[s.state]||s.state}</strong><small>{s.config.shot_seconds.toFixed(2)}s</small></button>)}</div>
    <footer className="scene-toolbar"><div><strong>{selected?.title||'Chưa có cảnh'}</strong><small>{selected?`${selected.completed_shots}/${selected.shot_count??'?'} shot · ${duration(selected.audio_seconds)}`:''}</small></div><button aria-expanded={inspector} onClick={()=>setInspector(!inspector)}>Thông số</button><button onClick={log}>Xem log</button>{selected&&<button disabled={!!production&&!['paused','completed','abandoned','superseded'].includes(production.status)} onClick={()=>editScene(selected.scene_id)}>Sửa cảnh</button>}</footer>
    </div><aside className="scene-navigator"><header><h3>Cảnh <small>{scenes.length}</small></h3><input aria-label="Tìm cảnh" placeholder="Tìm tên cảnh…" value={search} onChange={e=>setSearch(e.target.value)}/><select aria-label="Lọc cảnh" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">Tất cả cảnh</option><option value="missing">Chưa đủ clip</option><option value="error">Cần xử lý</option></select></header><div className="scene-scroll">{visible.map((s,i)=><button key={s.scene_id} className={selected?.scene_id===s.scene_id?'selected':''} onClick={()=>selectScene(s)}>{s.keyframe_id?<img loading="lazy" src={media(s.keyframe_id)} alt="Ảnh đại diện cảnh"/>:<span className="scene-number">{i+1}</span>}<span><strong>{s.title}</strong><small>{duration(s.audio_seconds)} · {s.completed_shots}/{s.shot_count??'?'} shot</small><em className={'status-'+s.status}>{names[s.status]||s.status}{current?.scene_id===s.scene_id?' · đang sản xuất':''}</em></span></button>)}{!visible.length&&<p className="work-empty">Không có cảnh phù hợp.</p>}</div></aside></div>
    {inspector&&<aside className="render-inspector" aria-label="Thông số cảnh"><header><h3>Thông số · {selected?.title}</h3><button onClick={()=>setInspector(false)}>Đóng</button></header>{selected?.configs.map((c,i)=><p key={i}>{c.width}×{c.height} nguồn · {c.frames} frame · {c.fps} fps · {c.steps} steps</p>)}{shot&&<dl>{Object.entries(shot.timing).map(([k,v])=><div key={k}><dt>{({total_seconds:'Tổng',comfy_execution_seconds:'Comfy thực thi',remote_wait_seconds:'Chờ remote',prepare_upload_seconds:'Chuẩn bị / upload',download_seconds:'Tải output',validation_seconds:'Kiểm tra',storage_seconds:'Lưu artifact'} as Record<string,string>)[k]||k}</dt><dd>{v.toFixed(1)}s</dd></div>)}</dl>}{production?.status==='paused'&&(!current||current.kind==='clip'||current.status==='completed')&&<PendingClipSettings url={`/api/studio/production-runs/${production.id}/pending-clip-config`}/>}</aside>}
    <div className="work-footnote">ETA và chi phí là ước tính xử lý, không phải tổng tiền thuê Pod. Tạm dừng không dừng tiền thuê.</div><details className="work-advanced"><summary>Nâng cao · benchmark GPU</summary><BenchmarkControl projectId={projectId} scenes={scenes}/></details>
  </section>;
}
