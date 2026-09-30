import {useState} from 'react';
import {useQueryClient} from '@tanstack/react-query';
import {api, base, type Job, type Project} from './api';
import {media, names} from './workspace';

const active=['queued','running','cancelling','reconciling'];

export default function VoiceAudition({project:p,jobs}:{project:Project;jobs:Job[]}){
  const client=useQueryClient();
  const [text,setText]=useState(p.scenes[0]?.narration?.slice(0,200)||'Các khanh bình thân, trẫm có điều muốn nói.');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const runs=jobs.filter(j=>j.kind==='voice_audition'&&j.project_id===p.id).sort((a,b)=>b.created_at.localeCompare(a.created_at));
  const running=runs.some(j=>active.includes(j.status));
  const last=runs.find(j=>j.status==='completed'&&j.result?.voices?.length);
  async function start(){
    setBusy(true);setError('');
    try{await api(`${base}/projects/${p.id}/voice-audition`,{text:text.trim()});await client.invalidateQueries();}
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  const blocker=!p.host_id?'Chọn và lưu máy GPU cho dự án ở bước “1. Ý tưởng” trước.':'';
  return <section className="panel" aria-label="Nghe thử giọng">
    <h2>Nghe thử giọng</h2>
    <p>Đọc <strong>cùng một câu</strong> bằng mọi giọng mà model giọng đọc trên GPU đang có, để bạn nghe rồi chọn giọng cho người dẫn truyện và từng nhân vật. Mất khoảng một phút GPU (lần đầu còn nạp model).</p>
    {blocker&&<p role="status" className="alert warning">{blocker}</p>}
    <fieldset disabled={busy||running||!!blocker}>
      <label>Câu nghe thử
        <textarea rows={2} maxLength={300} value={text} onChange={e=>setText(e.target.value)}/>
        <small>{text.trim().length}/300 ký tự</small>
      </label>
      <button className="primary" disabled={text.trim().length<2} onClick={()=>void start()}>{busy?'Đang tạo tác vụ…':running?'Đang đọc…':'Nghe thử mọi giọng · dùng GPU'}</button>
    </fieldset>
    {error&&<p role="alert" className="alert error">{error}</p>}
    {runs[0]&&!last&&<p role="status">{new Date(runs[0].created_at).toLocaleString('vi-VN')} · {names[runs[0].status]||runs[0].status}{runs[0].error?` · ${runs[0].error}`:''}</p>}
    {last&&<div className="voice-list">
      <p><strong>{new Date(last.created_at).toLocaleString('vi-VN')}</strong> · “{last.result?.text}”. Dùng tên giọng bên dưới ở ô “Giọng” của dự án hoặc khi thử nhân vật nói.</p>
      {last.result?.voices?.map(v=><div key={v.voice} className="voice-row"><strong>{v.label}</strong> <code>{v.voice}</code> <audio controls preload="none" src={media(v.artifact_id)}/></div>)}
    </div>}
  </section>;
}
