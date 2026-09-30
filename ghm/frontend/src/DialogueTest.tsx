import {useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {api, base, type Job, type Project} from './api';
import {media, names, read} from './workspace';
import OptionalModels from './OptionalModels';

const active=['queued','running','cancelling','reconciling'];
const MAX_CHARS=400;
// Preset names shipped with the installed VieNeu model; the audition lists whatever the GPU actually has.
const VOICES:[string,string][]=[['','Giọng của dự án'],['Binh','Bình · nam Bắc'],['Tuyen','Tuyên · nam Bắc'],['Vinh','Vĩnh · nam Nam'],['Doan','Đoan · nữ Nam'],['Ly','Ly · nữ Bắc'],['Ngoc','Ngọc · nữ Bắc']];

export default function DialogueTest({project:p,jobs}:{project:Project;jobs:Job[]}){
  const client=useQueryClient();
  const scenes=p.scenes.filter(s=>s.keyframe_id);
  const [sceneId,setSceneId]=useState('');
  const [text,setText]=useState('');
  const [voice,setVoice]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const models=useQuery({queryKey:['optional-models',p.host_id],enabled:!!p.host_id,
    queryFn:()=>read<Record<string,boolean>>(`/api/studio/hosts/${p.host_id}/optional-models`)});
  const ready=!!models.data?.['wan-s2v'];
  const trials=jobs.filter(j=>j.kind==='dialogue_test'&&j.project_id===p.id)
    .sort((a,b)=>b.created_at.localeCompare(a.created_at));
  const running=trials.some(j=>active.includes(j.status));
  const chosen=scenes.find(s=>s.id===(sceneId||scenes[0]?.id));
  async function start(){
    if(!chosen)return;
    setBusy(true);setError('');
    try{
      await api(`${base}/projects/${p.id}/dialogue-test`,{scene_id:chosen.id,text:text.trim(),...(voice?{voice}:{})});
      await client.invalidateQueries();
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  let blocker='';
  if(!p.host_id)blocker='Chọn và lưu máy GPU cho dự án ở bước “1. Ý tưởng” trước.';
  else if(!scenes.length)blocker='Cần ít nhất một cảnh đã có ảnh (tạo ảnh ở bước sản xuất trước).';
  else if(models.isSuccess&&!ready)blocker='Cài nhóm model Wan2.2 S2V trước: bấm nút cài ngay bên dưới (cũng có ở bước “1. Ý tưởng” → “Model GPU tùy chọn”).';
  return <section className="panel" aria-label="Thử nhân vật nói">
    <h2>Thử nhân vật nói</h2>
    <p>Chọn một cảnh đã có ảnh (không cần duyệt), gõ <strong>một câu thoại tiếng Việt</strong>. Historia tạo giọng đọc, rồi Wan2.2 S2V làm nhân vật trong ảnh nói câu đó (miệng chuyển động theo tiếng). Đây là bản thử để bạn đánh giá; nó không thay clip hay lời dẫn của cảnh.</p>
    <p className="alert notice">S2V dùng bộ mã hóa âm thanh tiếng Anh, nên độ khớp môi với tiếng Việt chưa được đảm bảo — đó chính là điều bản thử này kiểm tra. Bản thử dài tối đa khoảng 14 giây, chất lượng nháp, và dùng GPU đang thuê (chưa có số đo thời gian trên Pod của bạn; lần đầu còn phải nạp model 14B).</p>
    {blocker&&<p role="status" className="alert warning">{blocker}</p>}
    {models.isSuccess&&!ready&&<OptionalModels hostId={p.host_id} open/>}
    <fieldset disabled={busy||running||!!blocker}>
      <div className="form-grid">
        <label>Cảnh (dùng ảnh đã tạo của cảnh)
          <select value={chosen?.id||''} onChange={e=>setSceneId(e.target.value)}>
            {scenes.map(s=><option key={s.id} value={s.id}>{s.position}. {s.title}</option>)}
          </select>
        </label>
        <label>Giọng của câu này
          <select value={voice} onChange={e=>setVoice(e.target.value)}>{VOICES.map(([id,label])=><option key={id} value={id}>{label}</option>)}</select>
        </label>
        <label>Câu thoại của nhân vật
          <textarea rows={3} maxLength={MAX_CHARS} value={text} onChange={e=>setText(e.target.value)} placeholder="Ví dụ: Quân ta đã thắng lớn trên sông Bạch Đằng!"/>
          <small>{text.trim().length}/{MAX_CHARS} ký tự · giọng: {voice||p.voice||'mặc định của dự án'}</small>
        </label>
      </div>
      <button className="primary" disabled={text.trim().length<2} onClick={()=>void start()}>{busy?'Đang tạo tác vụ…':running?'Đang thử…':'Thử nhân vật nói · dùng GPU'}</button>
    </fieldset>
    {error&&<p role="alert" className="alert error">{error}</p>}
    {trials.slice(0,3).map(j=><article key={j.id} className="dialogue-trial">
      <p><strong>{new Date(j.created_at).toLocaleString('vi-VN')}</strong> · {names[j.status]||j.status}{j.error?` · ${j.error}`:''}</p>
      {j.status==='completed'&&j.result?.artifact_ids?.map(id=><video key={id} controls preload="metadata" src={media(id)}/>)}
    </article>)}
  </section>;
}
