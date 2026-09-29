import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import Dialog from './Dialog';
import { activeStatuses, names, navigate, read, useLocationState, type StudioJob } from './workspace';

type Entry={id:number;job_id:string;scene_id?:string;scene_title?:string;kind:string;created_at:string;message:string;level?:string;stage?:string;legacy:boolean;source?:string};
type Page={items:Entry[];has_more:boolean};
type Props={projectId?:string;jobId?:string;jobs:StudioJob[];onBack?:()=>void};
export default function LogWorkspace(props:Props) {
  const params=useLocationState();
  const filters=new URLSearchParams();
  for(const key of ['scene','job','stage','q','kind','level']) {
    const value=params.get('log_'+key);
    if(value)filters.set(key==='scene'||key==='job'?key+'_id':key,value);
  }
  if(params.get('run')&&props.projectId)filters.set('run_id',params.get('run')!);
  if(params.get('log_view')==='errors'&&!filters.has('level'))filters.set('level','error,warning');
  const endpoint=props.projectId?`/api/studio/projects/${props.projectId}/events`:`/api/studio/jobs/${props.jobId}/journal`;
  const scope=endpoint+'?'+filters.toString();
  return <Journal key={scope} {...props} scope={scope} endpoint={endpoint} filters={filters}/>;
}
function Journal({projectId,jobId,jobs,onBack,scope,endpoint,filters}:Props&{scope:string;endpoint:string;filters:URLSearchParams}) {
  const params=useLocationState();
  const [events,setEvents]=useState<Entry[]>([]);
  const cursor=useRef(0);
  const [older,setOlder]=useState(false);
  const [loadingOlder,setLoadingOlder]=useState(false);
  const [error,setError]=useState('');
  const [follow,setFollow]=useState(true);
  const [wrap,setWrap]=useState(true);
  const [scrollTop,setScrollTop]=useState(0);
  const [unseen,setUnseen]=useState(0);
  const [search,setSearch]=useState(params.get('log_q')||'');
  const [inspected,setInspected]=useState<Entry|null>(null);
  const viewport=useRef<HTMLDivElement>(null);
  const lastBatch=useRef<Page|null>(null);
  const runId=params.get('run');
  const scopedJobs=jobs.filter(j=>(!jobId||j.id===jobId)&&(!runId||!projectId||j.snapshot?.production_run_id===runId));
  const selectedJob=filters.get('job_id');
  const relevant=scopedJobs.filter(j=>(!selectedJob||j.id===selectedJob)&&(!filters.get('scene_id')||j.scene_id===filters.get('scene_id')));
  const active=relevant.some(j=>activeStatuses.includes(j.status));
  const query=useQuery({queryKey:['journal',scope],gcTime:0,retry:1,refetchInterval:active?2000:false,queryFn:async()=>{
    const result=await read<Page>(scope+(cursor.current?`&after=${cursor.current}&tail=false`:'&tail=true'));
    return result;
  }});
  useEffect(()=>{
    if(!query.data||lastBatch.current===query.data)return;
    lastBatch.current=query.data;
    const first=cursor.current===0;
    if(first)setOlder(query.data.has_more);
    if(query.data.items.length) {
      cursor.current=Math.max(cursor.current,...query.data.items.map(e=>e.id));
      setEvents(previous=>Array.from(new Map([...previous,...query.data!.items].map(e=>[e.id,e])).values()).sort((a,b)=>a.id-b.id));
      if(!follow)setUnseen(n=>n+query.data!.items.length);
    }
    // Drain paginated new events with one reader, even after a job finishes.
    if(!first&&query.data.has_more)void query.refetch();
  },[query.data,follow,query]);
  useEffect(()=>{if(follow&&viewport.current)viewport.current.scrollTop=viewport.current.scrollHeight;},[events,follow]);
  const change=(key:string,value:string)=>navigate({['log_'+key]:value||null});
  const rowHeight=wrap?64:40;
  const start=Math.max(0,Math.floor(scrollTop/rowHeight)-5);
  const visible=events.slice(start,start+24);
  const loadOlder=async()=>{
    setLoadingOlder(true);setError('');setFollow(false);
    try {
      const result=await read<Page>(scope+`&before=${events[0]?.id||0}`);
      setEvents(previous=>Array.from(new Map([...result.items,...previous].map(e=>[e.id,e])).values()).sort((a,b)=>a.id-b.id));
      setOlder(result.has_more);
    }catch(e){setError((e as Error).message);}finally{setLoadingOlder(false);}
  };
  const scenes=Array.from(new Map(scopedJobs.filter(j=>j.scene_id).map(j=>[j.scene_id!,j.snapshot?.scene?.title||j.scene_id!])).entries());
  const view=params.get('log_view')||'activity';
  return <section className="editing-workspace log-workspace" aria-label="Nhật ký sản xuất">
    <header className="work-header"><div><span className="work-kicker">NHẬT KÝ STUDIO</span><h2>Theo dõi sản xuất</h2><p className={query.error?'work-error':''}>{query.error?'Mất kết nối nhật ký · không xác định lại trạng thái render':query.isFetching?'Đang cập nhật…':`Cập nhật cuối ${query.dataUpdatedAt?new Date(query.dataUpdatedAt).toLocaleTimeString('vi-VN'):'—'}`}</p></div><div className="work-actions">{onBack&&<button onClick={onBack}>← Video</button>}<a className="work-button" href={endpoint+'/export?'+filters.toString()}>Tải nhật ký</a><button onClick={()=>void query.refetch()}>Cập nhật</button></div></header>
    <form className="log-filters" onSubmit={e=>{e.preventDefault();change('q',search);}}><label>Tìm nội dung<div className="search-control"><input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Tìm trong toàn bộ lịch sử…"/><button>Tìm</button></div></label><label>Cảnh<select value={params.get('log_scene')||''} onChange={e=>navigate({log_scene:e.target.value,log_job:null,log_stage:null})}><option value="">Tất cả cảnh</option>{scenes.map(([id,title])=><option key={id} value={id}>{title}</option>)}</select></label><label>Công đoạn<select value={params.get('log_kind')||''} onChange={e=>change('kind',e.target.value)}><option value="">Tất cả</option>{Array.from(new Set(scopedJobs.map(j=>j.kind))).map(k=><option key={k} value={k}>{names[k]||k}</option>)}</select></label><label>Mức độ<select value={params.get('log_level')||''} onChange={e=>change('level',e.target.value)}><option value="">Tất cả</option><option value="info">Thông tin</option><option value="warning">Cảnh báo</option><option value="error">Lỗi</option></select></label></form>
    {filters.get('stage')&&<p className="work-context">Shot: {filters.get('stage')} · Sự kiện cũ hoặc chung không có liên kết shot vẫn được hiển thị. <button onClick={()=>change('stage','')}>Bỏ lọc shot</button></p>}
    <div className="journal-layout"><aside className="journal-jobs"><button className={!selectedJob?'selected':''} onClick={()=>navigate({log_job:null,log_stage:null})}>Toàn bộ tác vụ <small>{scopedJobs.length}</small></button>{scopedJobs.map(j=><button key={j.id} className={selectedJob===j.id?'selected':''} onClick={()=>navigate({log_job:j.id,log_scene:j.scene_id||null,log_stage:null})}><strong>{j.snapshot?.scene?.title||names[j.kind]||j.kind}</strong><small>{names[j.kind]} · {names[j.status]||j.status}</small></button>)}</aside>
    <div className="journal-main"><nav className="work-tabs" aria-label="Chế độ nhật ký">{[['activity','Hoạt động'],['errors','Lỗi & cảnh báo'],['technical','Log kỹ thuật']].map(([id,label])=><button key={id} aria-pressed={view===id} onClick={()=>navigate({log_view:id,log_level:null})}>{label}</button>)}</nav>
      {view==='errors'&&relevant.filter(j=>j.error).map(j=><div className="work-error-box" key={j.id}><strong>{j.snapshot?.scene?.title||names[j.kind]}</strong><p>{j.error}</p><button onClick={()=>navigate({log_job:j.id,log_scene:j.scene_id||null,log_stage:null})}>Lọc tác vụ này</button><small>Trạng thái: {names[j.status]}. Xử lý qua điều khiển Video hoặc Tác vụ.</small></div>)}
      {(error||query.error)&&<p role="alert" className="work-error">{error||query.error?.message}</p>}
      {older&&<button disabled={loadingOlder} onClick={()=>void loadOlder()}>{loadingOlder?'Đang tải…':'Tải sự kiện trước đó'}</button>}
      <div className="journal-viewport" ref={viewport} tabIndex={0} aria-label="Nội dung nhật ký" onScroll={e=>{setScrollTop(e.currentTarget.scrollTop);if(e.currentTarget.scrollHeight-e.currentTarget.clientHeight-e.currentTarget.scrollTop>80)setFollow(false);}}>
        {!events.length&&<div className="work-empty">{query.isFetching?'Đang tải nhật ký…':'Chưa có sự kiện phù hợp.'}<small>Nhật ký Studio; không phải terminal hoặc log Comfy thô.</small></div>}
        <div style={{height:events.length*rowHeight,position:'relative'}}>{visible.map((e,i)=><article key={e.id} className={`journal-row ${wrap?'wrapped':''} level-${e.level||'unknown'}`} style={{position:'absolute',top:(start+i)*rowHeight,height:rowHeight}}><time>{new Date(e.created_at).toLocaleTimeString('vi-VN')}</time><span className="journal-source" title={e.job_id}>{e.scene_title||names[e.kind]}<small>{e.stage||'Toàn tác vụ'} · {e.level||'cũ'}</small></span><button className="journal-message" onClick={()=>setInspected(e)} title="Mở nội dung đầy đủ">{e.message}</button></article>)}</div>
      </div><footer className="journal-footer"><label><input type="checkbox" checked={follow} onChange={e=>{setFollow(e.target.checked);setUnseen(0);}}/> Theo dõi dòng mới</label><label><input type="checkbox" checked={wrap} onChange={e=>{setWrap(e.target.checked);setScrollTop(0);if(viewport.current)viewport.current.scrollTop=0;}}/> Xuống dòng</label><button onClick={()=>void navigator.clipboard.writeText(events.map(e=>`${e.created_at} ${e.message}`).join('\n')).catch(()=>setError('Không truy cập được clipboard. Chọn nội dung để sao chép.'))}>Sao chép phần đã tải</button>{unseen>0&&<button onClick={()=>{setFollow(true);setUnseen(0);}}>{unseen} dòng mới ↓</button>}<small>{events.length} dòng đã tải · dùng Tải nhật ký để lấy đầy đủ</small></footer>
    </div></div>
    {inspected&&<Dialog title={`Sự kiện #${inspected.id}`} description={`${inspected.scene_title||names[inspected.kind]||'Studio'} · ${new Date(inspected.created_at).toLocaleString('vi-VN')}`} onClose={()=>setInspected(null)} className="studio-event-dialog">
      <div className="event-detail"><p>{inspected.legacy?'Sự kiện cũ · chưa có metadata công đoạn/mức độ':`${inspected.stage||'Toàn tác vụ'} · ${inspected.level||'Thông tin'}`}</p><pre>{view==='technical'?JSON.stringify(inspected,null,2):inspected.message}</pre></div>
    </Dialog>}
  </section>;
}
