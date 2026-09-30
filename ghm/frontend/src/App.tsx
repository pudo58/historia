import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, base, type Host, type Job, type Project, type Run } from './api';
import { AccountControls } from './AuthGate';
import GpuManager from './GpuManager';
import InstallPanel from './InstallPanel';
import { JobList } from './LegacyProjectView';
import ProjectForm from './ProjectForm';
import ProjectLibrary from './ProjectLibrary';
import ProjectView from './ProjectView';
import RemoteAccess from './RemoteAccess';
import RunPodMonitor, { useRunPod, money as usd, uptime as upFmt } from './RunPodMonitor';
import { Badge } from './ui';
import { navigate, useLocationState } from './workspace';

export default function App() {
  const client = useQueryClient();
  const location=useLocationState();
  const page=location.get('page')||'projects';
  const projectId=location.get('project');
  const setPage=(value:string)=>navigate({page:value});
  const setProjectId=(value:string|null)=>navigate({project:value,tab:null,run:null,scene:null,shot:null,media:null,log_job:null,log_scene:null,log_stage:null});
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const projects = useQuery({queryKey: ['projects'], queryFn: () => api<Project[]>(`${base}/projects`)});
  const hosts = useQuery({queryKey: ['hosts'], queryFn: () => api<Host[]>('/api/hosts')});
  const jobs = useQuery({queryKey: ['studio-jobs'], queryFn: () => api<Job[]>(`${base}/jobs`), refetchInterval: 3000});
  const project = useQuery({queryKey: ['project', projectId], queryFn: () => api<Project>(`${base}/projects/${projectId}`), enabled: !!projectId, refetchInterval: 4000});
  const run: Run = async action => {
    setBusy(true); setError('');
    try { const value = await action(); await client.invalidateQueries(); return value; }
    catch (e) { setError((e as Error).message); return undefined; }
    finally { setBusy(false); }
  };
  const active = jobs.data?.filter(j => ['queued', 'running', 'reconciling', 'cancelling'].includes(j.status)).length || 0;
  const hostUsage=(hosts.data||[]).map(h=>({host:h, jobs:(jobs.data||[]).filter(j=>j.host_id===h.id&&['running','queued','cancelling','reconciling'].includes(j.status))}));
  const lead=hostUsage.find(entry=>entry.jobs.some(j=>j.status==='running'))||hostUsage.find(entry=>entry.jobs.length)||hostUsage[0];
  const leadProject=projects.data?.find(p=>p.host_id===lead?.host.id);
  const hourly=leadProject?.hourly_usd;
  const runpod=useRunPod().data;
  const runningPods=runpod?.configured&&!runpod.error?runpod.pods.filter(p=>p.status==='RUNNING'):null;
  const leadPod=runningPods?(runningPods.find(p=>lead&&(p.host_ids||[p.host_id]).includes(lead.host.id))||runningPods[0]):null;
  const leadState=lead?.jobs.some(j=>j.status==='running')?'Đang xử lý':lead?.jobs.some(j=>j.status==='reconciling')?'Cần đối chiếu':lead?.jobs.some(j=>j.status==='queued')?'Có tác vụ chờ':lead?.host.state==='ready'?'Không có tác vụ Studio':'Chưa sẵn sàng';
  return <div className={`studio ${page==='jobs'||(page==='projects'&&projectId&&['video','logs'].includes(location.get('tab')||''))?'editing-mode':''}`}><aside className="sidebar"><a className="brand" href="#" onClick={e => {e.preventDefault(); setPage('projects'); setProjectId(null);}}><span className="monogram">H</span><span>HISTORIA<small>VIDEO STUDIO</small></span></a><div className="sidebar-label">KHÔNG GIAN LÀM VIỆC</div><nav aria-label="Điều hướng chính">{[['projects', '01', 'Dự án phim'], ['jobs', '02', 'Tác vụ'], ['gpu', '03', 'Kết nối GPU'], ['packs', '04', 'Bộ AI & kiểm chứng']].map(([id, number, title]) => <button key={id} className={page === id ? 'selected' : ''} aria-current={page === id ? 'page' : undefined} onClick={() => setPage(id)}><span>{number}</span>{title}{id === 'jobs' && active > 0 && <b>{active}</b>}</button>)}</nav><div className="sidebar-bottom"><div className="gpu-live-widget" role="status" aria-live="polite"><strong>GPU · {lead?.host.label||'Chưa kết nối'}</strong><span className={`status-chip status-${lead?.jobs.some(j=>j.status==='running')?'running':lead?.jobs.length?'reconciling':lead?.host.state==='ready'?'ready':'pending'}`}><span aria-hidden="true">●</span>{leadState}</span><small>{lead?.host.gpu?.name||'Chưa kiểm chứng phần cứng'}</small>{runningPods?<><small className={runningPods.length?'pod-cost-live':undefined}>{runningPods.length?`RunPod: ${runningPods.length} Pod chạy · ${usd(runpod?.running_cost_per_hr)}/giờ`:'RunPod: không có Pod nào đang chạy ✓'}</small>{leadPod&&<small>Pod {leadPod.name||leadPod.id}: chạy {upFmt(leadPod.uptime_seconds)} · đã tốn {usd(leadPod.session_cost)}</small>}</>:<><small>{hourly!=null?`Giá do bạn nhập: $${hourly.toFixed(2)}/giờ`:'Giá thuê: chưa nhập · nhập RunPod API key để thấy giá thực'}</small><small>Uptime pod: chưa kết nối RunPod</small></>}<button type="button" onClick={()=>setPage('gpu')}>Quản lý GPU ↗</button><p>Dừng job không dừng tính tiền. Dừng Pod ở trang Kết nối GPU hoặc trong RunPod.</p></div></div></aside><div className="workspace"><header className="topbar"><span>Historical Video Studio <span className="muted">/ {page === 'projects' ? 'Dự án' : page === 'gpu' ? 'Hạ tầng GPU' : page === 'jobs' ? 'Tác vụ' : 'Bộ AI & kiểm chứng'}</span></span><span className="topbar-right"><Badge>CHỈ CHẠY TRÊN MÁY NÀY</Badge><AccountControls/></span></header><main className="studio-content" aria-busy={busy}>
    {(error || projects.error || hosts.error || jobs.error || project.error) && <div role="alert" className="alert error"><span>{error || (projects.error || hosts.error || jobs.error || project.error)?.message}</span><button onClick={() => {setError(''); void client.invalidateQueries();}}>Thử lại</button></div>}
    {page==='gpu'||page==='packs'?<div className="alert notice"><span>◈</span><div><strong>Thiết lập có kiểm chứng</strong><p>Chỉ chạy tác vụ AI khi model và workflow tương ứng đã sẵn sàng; kết quả không được giả lập.</p></div></div>:null}
    {page === 'projects' && !projectId && <><section className="project-hero"><div><p className="eyebrow">TỪ TƯ LIỆU ĐẾN THƯỚC PHIM</p><h1>Lịch sử, qua<br/><em>góc nhìn của bạn.</em></h1><p>Biến tư liệu thành câu chuyện điện ảnh. Chuẩn bị nguồn, định hình thế giới và duyệt từng cảnh — theo nhịp làm việc của bạn.</p><button className="primary" onClick={() => setCreating(true)}>＋ Tạo dự án phim</button></div><div className="hero-note"><span className="hero-folio" aria-hidden="true">H</span><p>PHÒNG LÀM PHIM LỊCH SỬ</p><strong>Mỗi thước phim bắt đầu<br/>bằng một nguồn tin cậy.</strong><small>Tư liệu local · Con người kiểm chứng</small></div></section>{!projectId&&!creating&&((hosts.data||[]).length===0||(hosts.data||[]).every(h=>h.state!=='ready')||(projects.data||[]).length===0)&&<section className="panel getting-started"><h2>Bắt đầu trong 3 bước</h2><ol>{[[ (hosts.data||[]).length>0,'Thêm máy GPU (Pod RunPod) qua SSH','gpu','Mở Kết nối GPU'],[(hosts.data||[]).some(h=>h.state==='ready'),'Kiểm tra máy rồi cài Bộ AI','packs','Mở Bộ AI'],[(projects.data||[]).length>0,'Tạo dự án phim đầu tiên',null,'Tạo dự án']].map(([done,label,target,action],i)=><li key={i} className={done?'done':''}><span aria-hidden="true">{done?'✓':i+1}</span><span>{label as string}</span>{!done&&<button type="button" onClick={()=>target?setPage(target as string):setCreating(true)}>{action as string}</button>}</li>)}</ol></section>}<div className="journey">{[['Ý tưởng','Tư liệu & bối cảnh'],['Kịch bản','Lời dẫn & từng cảnh'],['Video','Duyệt & hoàn thiện']].map(([s,detail],i) => <span key={s}><b>{String(i+1).padStart(2,'0')}</b><span><strong>{s}</strong><small>{detail}</small></span></span>)}</div>
      {creating && <section className="panel"><div className="section-heading"><h2>Tạo dự án phim</h2><button onClick={() => setCreating(false)}>Đóng</button></div><ProjectForm hosts={hosts.data || []} busy={busy} onSave={data => run(async () => {const p = await api<Project>(`${base}/projects`, data); setProjectId(p.id); setCreating(false);})}/></section>}
      {projects.isLoading ? <div className="project-loading" role="status"><span>Đang mở thư viện dự án…</span><div className="project-grid" aria-hidden="true">{[1,2,3].map(n=><div className="project-skeleton" key={n}/>)}</div></div> : <ProjectLibrary projects={projects.data || []} open={setProjectId}/>}</>}
    {page === 'projects' && projectId && (project.data ? <ProjectView key={projectId} project={project.data} hosts={hosts.data || []} jobs={(jobs.data || []).filter(j => j.project_id === projectId)} run={run} busy={busy} error={error} back={() => setProjectId(null)}/> : <p>Đang tải dự án…</p>)}
    {page === 'jobs' && <><div className="page-heading"><div><p className="eyebrow">SẢN XUẤT</p><h1>Tác vụ</h1><p>Hàng đợi, kết quả và nhật ký của từng công đoạn.</p></div></div><JobList jobs={jobs.data || []} run={run}/></>}
    {page === 'gpu' && <><div className="page-heading"><div><p className="eyebrow">THIẾT LẬP MỘT LẦN</p><h1>Máy GPU của bạn</h1><p>Nhập SSH → đối chiếu fingerprint → kiểm tra máy → cài bộ AI.</p></div><button className="primary" onClick={()=>setPage('packs')}>Cài bộ Video lịch sử →</button></div><RunPodMonitor/><div className="alert warning">Dừng tác vụ hay đóng trình duyệt không dừng tiền thuê GPU — dừng Pod bằng nút "Dừng" ở bảng RunPod phía trên hoặc tại RunPod. Bộ AI và giọng đọc cài ở trang Bộ AI & kiểm chứng; phần bên dưới dành cho thao tác kỹ thuật chuyên sâu.</div><div className="gpu-legacy"><GpuManager/></div><RemoteAccess/></>}
    {page === 'packs' && <InstallPanel/>}
    <footer className="app-footer">HISTORIA / Lịch sử cần được kiểm chứng bởi con người. AI không tự đảm bảo sự chính xác hay tính nhất quán.</footer>
  </main></div></div>;
}
