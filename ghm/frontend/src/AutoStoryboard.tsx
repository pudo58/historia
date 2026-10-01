import {useState} from 'react';
import {useQueryClient} from '@tanstack/react-query';
import {api, base, type Project} from './api';

export default function AutoStoryboard({project:p}:{project:Project}){
  const client=useQueryClient();
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  const [error,setError]=useState('');
  const ready=p.scenes.filter(s=>s.speech_id&&!s.shot_list?.length&&(s.motion||'wan')==='wan').length;
  async function run(){
    setBusy(true);setError('');setMessage('');
    try{
      const result=await api<{updated:string[];skipped:string[]}>(`${base}/projects/${p.id}/auto-storyboard`,{});
      setMessage(`Đã lập shot list nhiều góc cho ${result.updated.length} cảnh; ${result.skipped.length} cảnh được bỏ qua (chưa có lời đọc, quá ngắn hoặc đã có shot list).`);
      await client.invalidateQueries();
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  return <section className="panel" aria-label="Chia góc máy tự động">
    <h2>Chia góc máy tự động</h2>
    <p>Cảnh dài hơn một clip hiện chạy lại từ <strong>cùng một khung ảnh</strong> nên bị giật và lặp. Nút này chia mỗi cảnh thành nhiều shot theo nhịp toàn cảnh → trung cảnh → cận chi tiết, mỗi shot có ảnh riêng. Chạy sau khi đã tạo lời đọc; không tốn GPU, nhưng ảnh từng shot sẽ cần duyệt và tạo thêm.</p>
    <button className="primary" disabled={busy||!ready} onClick={()=>void run()}>{busy?'Đang chia…':`Chia góc cho ${ready} cảnh đã có lời đọc`}</button>
    {!ready&&<p role="status"><small>Chưa có cảnh nào đủ điều kiện: cần lời đọc đã tạo và cảnh chưa có shot list.</small></p>}
    {message&&<p role="status">{message}</p>}{error&&<p role="alert" className="alert error">{error}</p>}
  </section>;
}
