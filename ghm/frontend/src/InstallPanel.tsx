import { useState } from 'react';
import HuggingFaceToken from './HuggingFaceToken';
import { JobEvents } from './StudioControls';
import { useQuery, useQueryClient } from '@tanstack/react-query';

type Host = {id:string; label:string; address:string; port:number; username:string; pinned_fingerprint?:string; gpu?:{name:string;vram_gb:number}};
type Options = {root:string; remote_port:number; adopt_existing:boolean; comfy_path?:string|null};
type Discovery = {paths:string[];services:{port:number;version:string}[];suggested_options:Options;transport:string};
type Plan = {plan_id:string; options:Options; missing_bytes:number; required_bytes:number; reserve_bytes:number; valid_files:number; blockers:string[]; inventory:{free_bytes:number;filesystem_path:string;comfy_exists:boolean}; lock:{models:{name:string;repo:string;filename:string;size_bytes:number;revision:string}[]; snapshots:{name:string;repo:string;size_bytes:number;revision:string}[]; environments:Record<string,string>}};
type Job = {id:string; kind:string;status:string;progress:number;error?:string;result:{artifact_ids?:string[];elapsed_seconds?:number}};
type State = {status:string;plan?:Plan;jobs:Job[];verification?:{elapsed_seconds:number;artifact_ids:string[]}};
const base='/api/studio';
const active=['queued','running','cancelling','reconciling'];
const names:Record<string,string>={not_installed:'Chưa cài',queued:'Đang chờ',installing:'Đang cài',installed:'Đã cài · chưa kiểm chứng',verifying:'Đang tạo output thử',verified:'Đã kiểm chứng bản nháp',interrupted:'Gián đoạn · cần kiểm tra',install_failed:'Cài đặt thất bại',verify_failed:'Kiểm chứng thất bại',running:'Đang chạy',completed:'Hoàn tất',failed:'Thất bại',cancelled:'Đã hủy',reconciling:'Cần đối chiếu',cancelling:'Đang ngắt'};
const gb=(n:number)=>`${(n/1024**3).toFixed(2)} GiB`;
async function call<T>(path:string,body?:unknown,method=body===undefined?'GET':'POST'):Promise<T>{
  const r=await fetch(path,{method,headers:body===undefined?{}:{'content-type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  if(!r.ok){const e=await r.json();throw new Error(typeof e.detail==='string'?e.detail:'Thông tin chưa hợp lệ. Kiểm tra đường dẫn/port.');}
  return r.json();
}

export default function InstallPanel(){
  const [id,setId]=useState('');
  const hosts=useQuery({queryKey:['hosts'],queryFn:()=>call<Host[]>('/api/hosts')});
  const selected=id || (hosts.data?.length===1 ? hosts.data[0].id : '');
  const host=hosts.data?.find(h=>h.id===selected);
  return <><div className="page-heading"><div><p className="eyebrow">THIẾT LẬP MỘT LẦN</p><h1>Cài bộ Video lịch sử</h1><p>Chọn GPU → xem trước → xác nhận cài → tạo output kiểm chứng.</p></div></div>
    <div className="alert warning">Dừng cài, render hoặc đóng web KHÔNG dừng tiền thuê GPU. Bộ cài đang ở giai đoạn nghiệm thu; không tự hạ chất lượng hoặc sửa driver.</div>
    <section className="panel"><label className="field"><span>Máy GPU cần cài</span><select value={selected} onChange={e=>setId(e.target.value)}><option value="">Chọn máy…</option>{hosts.data?.map(h=><option value={h.id} key={h.id}>{h.label} · {h.gpu?.name || h.address}</option>)}</select></label>{hosts.error && <p role="alert">{hosts.error.message}</p>}{!hosts.data?.length && <p>Thêm SSH và xác nhận fingerprint trong mục Kết nối GPU trước.</p>}</section>
    <HuggingFaceToken/>
    {host && <HostInstall key={host.id} host={host}/>}
  </>;
}

function HostInstall({host}:{host:Host}){
  const client=useQueryClient();
  const [edited,setOptions]=useState<Options|null>(null);
  const [discovery,setDiscovery]=useState<Discovery|null>(null);
  const [stage,setStage]=useState('');
  const [consent,setConsent]=useState(false);
  const [autoConsent,setAutoConsent]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [fingerprint,setFingerprint]=useState('');
  const url=`${base}/hosts/${host.id}/installation`;
  const state=useQuery({queryKey:['installation',host.id],queryFn:()=>call<State>(url),refetchInterval:3000});
  const plan=state.data?.plan;
  const options=edited || plan?.options || {root:'/workspace/historia',remote_port:8190,adopt_existing:false};
  const working=state.data?.jobs.some(j=>active.includes(j.status));
  const basic=host.address.toLowerCase().replace(/\.$/,'')==='ssh.runpod.io';
  const matching=plan && plan.options.root===options.root && plan.options.remote_port===options.remote_port && plan.options.adopt_existing===options.adopt_existing && (plan.options.comfy_path || null)===(options.comfy_path || null);
  const run=async<T,>(fn:()=>Promise<T>):Promise<T|undefined>=>{setBusy(true);setError('');try{const v=await fn();await client.invalidateQueries();return v;}catch(e){setError((e as Error).message);}finally{setBusy(false);}};
  const change=(next:Options)=>{setOptions(next);setConsent(false);};
  return <>
    <section className="panel"><div className="section-heading"><h2>{host.label}</h2><span className="badge">{names[state.data?.status || 'not_installed']}</span></div>
      <p>{host.username}@{host.address}:{host.port}</p>
      {(error || state.error) && <div role="alert" className="alert error">{error || state.error?.message}</div>}
      {basic && <p className="muted">Runpod Basic SSH được hỗ trợ qua terminal bảo mật. Không cần đổi IP/port, mở cổng ComfyUI công khai hay cài agent thường trú.</p>}
      {!host.pinned_fingerprint && <div className="actions"><button disabled={busy} onClick={()=>void run(async()=>{const result=await call<{fingerprint:string}>(`/api/hosts/${host.id}/inspect-key`,{});setFingerprint(result.fingerprint);})}>Đọc fingerprint</button>{fingerprint && <><code>{fingerprint}</code><button disabled={busy} onClick={()=>void run(async()=>{await call(`/api/hosts/${host.id}/confirm-key`,{fingerprint});setFingerprint('');})}>Tôi đã đối chiếu · tin cậy khóa này</button></>}</div>}
    </section>
    <section className="panel"><h2>Cài tự động · chỉ bổ sung phần thiếu</h2>
      <p>Một lần bấm: kiểm tra GPU và SSH → dò ComfyUI → kiểm tra checksum và dung lượng → tạo tác vụ cài phần thiếu. Không sửa driver, không dừng hoặc thay dependency ComfyUI có sẵn.</p>
      <p>Bộ này gồm Qwen3-VL, Qwen-Image/Edit, Wan2.2 và VieNeu. Có thể cần hơn 100 GiB model, cộng dung lượng môi trường và cache. Chi phí thuê GPU vẫn tiếp tục.</p>
      <p>Đọc điều kiện sử dụng tại <a href="https://huggingface.co/Qwen" target="_blank" rel="noreferrer">Qwen</a>, <a href="https://huggingface.co/Wan-AI" target="_blank" rel="noreferrer">Wan</a> và danh sách model/license trong bản xem trước bên dưới. Nếu chưa xem đủ license, dùng bước kiểm tra riêng trước.</p>
      <label className="install-consent"><input type="checkbox" checked={autoConsent} disabled={busy || working} onChange={e=>setAutoConsent(e.target.checked)}/> Tôi đã xem điều kiện sử dụng bộ model và cho phép tự động tải/cài phần thiếu lên {host.label} sau khi kiểm tra an toàn.</label>
      <button className="primary" disabled={busy || working || !host.pinned_fingerprint || !autoConsent || state.isLoading || !!state.error} onClick={()=>void run(async()=>{
        setStage('Đang kiểm tra GPU, SSH và ComfyUI…');
        const d=await call<Discovery>(url+'/discover',{});
        setDiscovery(d);
        if(d.paths.length>1 || d.services.length>1 || ((d.paths.length>0 || d.services.length>0) && !d.suggested_options.adopt_existing)) throw new Error('Phát hiện môi trường ComfyUI chưa xác định rõ. Chọn cấu hình nâng cao và kiểm tra riêng; không tự tạo hoặc thay môi trường đang có.');
        setOptions(d.suggested_options);
        setStage('Đang kiểm tra file đã cài, checksum và dung lượng…');
        const prepared=await call<Plan>(url+'/prepare',d.suggested_options);
        await client.invalidateQueries({queryKey:['installation',host.id]});
        if(prepared.blockers.length) throw new Error(prepared.blockers.join(' '));
        setStage('Đang tạo tác vụ cài phần còn thiếu…');
        return call(url+'/start',{plan_id:prepared.plan_id,license_accepted:true});
      })}>{busy ? (stage || 'Đang xử lý…') : working ? 'Đang có tác vụ · xem tiến độ bên dưới' : 'Kiểm tra & cài phần còn thiếu'}</button>
      <p role="status">{busy ? 'Giữ trang mở trong khi kiểm tra. Sau khi tạo tác vụ cài, có thể tải lại trang để xem tiến độ.' : 'File đúng checksum được giữ lại. Thiếu dung lượng hoặc file sai checksum sẽ dừng và báo lỗi, không ghi đè. Kiểm chứng output thật vẫn là bước riêng.'}</p>
    </section>
    <section className="panel"><h2>1. Kiểm tra & chuẩn bị</h2><p>Tự tìm ComfyUI có sẵn, kiểm tra GPU và tính chính xác phần model còn thiếu. Chưa tải model hoặc thay đổi máy ở bước này.</p>
      <button className="primary" disabled={busy || working || !host.pinned_fingerprint} onClick={()=>void run(async()=>{setConsent(false);setStage('Đang kiểm tra GPU và tìm ComfyUI…');const d=await call<Discovery>(url+'/discover',{});setDiscovery(d);setOptions(d.suggested_options);setStage('Đang đối chiếu model và dung lượng ổ…');return call<Plan>(url+'/prepare',d.suggested_options);})}>{busy ? (stage || 'Đang xử lý…') : 'Kiểm tra & chuẩn bị cài'}</button>
      {busy && <p role="status">Có thể mất vài phút để kiểm tra checksum model có sẵn. Chưa đóng trang khi đang kiểm tra.</p>}
      {discovery && <p>Đã kiểm tra kết nối {discovery.transport==='terminal'?'Basic SSH':'SSH'}. {discovery.services.map(s=>`ComfyUI ${s.version} ở port ${s.port}`).join('; ') || 'Chưa tìm thấy ComfyUI đang chạy.'}</p>}
      <details><summary>Nâng cao · thư mục, port và chế độ cài</summary><div className="form-grid"><label className="field"><span>Thư mục dịch vụ Studio</span><input value={options.root} disabled={busy || working} onChange={e=>change({...options,root:e.target.value})}/></label><label className="field"><span>Port ComfyUI trên GPU</span><input type="number" min={1024} max={65535} value={options.remote_port} disabled={busy || working} onChange={e=>change({...options,remote_port:Number(e.target.value)})}/></label><label className="field"><span>Chế độ</span><select value={options.adopt_existing?'adopt':'new'} disabled={busy || working} onChange={e=>change({...options,adopt_existing:e.target.value==='adopt',comfy_path:null})}><option value="new">Tạo ComfyUI riêng</option><option value="adopt">Tái sử dụng ComfyUI có sẵn</option></select></label>{options.adopt_existing && <label className="field"><span>Đường dẫn ComfyUI có sẵn</span><input value={options.comfy_path || options.root+'/ComfyUI'} disabled={busy || working} onChange={e=>change({...options,comfy_path:e.target.value})}/></label>}</div><p>/workspace không tự đảm bảo dữ liệu tồn tại khi xóa Pod. Chọn volume phù hợp với gói thuê. Tái sử dụng không sửa dependency Python hoặc dừng ComfyUI có sẵn.</p><button disabled={busy || working || !host.pinned_fingerprint} onClick={()=>void run(async()=>{setConsent(false);setStage('Đang kiểm tra cấu hình đã chọn…');await call(`/api/hosts/${host.id}/preflight`,{});return call<Plan>(url+'/prepare',options);})}>Kiểm tra theo cấu hình này</button></details>
    </section>
    {!plan && <section className="panel"><h2>2. Bộ AI sẽ cài</h2><p>Qwen3-VL 8B (tư liệu/kịch bản), Qwen-Image và Edit-2509 (ảnh), Wan2.2 I2V A14B (clip), VieNeu 0.5B (giọng Việt), ComfyUI và dependency cần thiết.</p><p>Hoàn tất bước 1 để xem dung lượng, revision và license. Chưa có bản xem trước nên chưa thể bắt đầu tải/cài.</p></section>}
    {plan && <section className="panel"><h2>2. Xem trước & xác nhận cài</h2>{!matching && <div className="alert warning">Đang hiển thị bản xem trước đã lưu cho {plan.options.root}:{plan.options.remote_port}. Thông số form đã khác.<button onClick={()=>change(plan.options)}>Dùng thông số đã lưu</button></div>}
      <p><strong>{plan.options.adopt_existing?'Tái sử dụng':'Cài riêng'} ComfyUI: </strong>{plan.options.comfy_path || plan.options.root+'/ComfyUI'} · port {plan.options.remote_port}. Dịch vụ LLM/TTS riêng tại {plan.options.root}.</p>
      <div className="stats"><div><strong>{gb(plan.missing_bytes)}</strong><span>Model còn cần tải</span></div><div><strong>{gb(plan.inventory.free_bytes)}</strong><span>Ổ trống tại {plan.inventory.filesystem_path}</span></div><div><strong>{gb(plan.required_bytes)}</strong><span>Cần trống · gồm dự phòng {gb(plan.reserve_bytes)}</span></div><div><strong>{plan.valid_files}</strong><span>File đúng checksum được giữ</span></div></div>
      <p>Dự phòng dành cho Python, dependency, cache và output; không phải dung lượng tải chính xác của pip. Bộ AI: Qwen3-VL 8B, Qwen-Image/Edit-2509, Wan2.2 I2V A14B và VieNeu 0.5B. Không cài mọi model trên ComfyUI.</p>
      {plan.blockers.map(b=><div role="alert" className="alert error" key={b}>{b}</div>)}
      <details><summary>Danh sách model, revision & điều kiện sử dụng</summary><div className="install-table"><table><thead><tr><th>Thành phần</th><th>Dung lượng</th><th>Nguồn / license</th></tr></thead><tbody>{[...plan.lock.models,...plan.lock.snapshots].map(m=><tr key={m.name}><td>{m.name}<small>{m.revision.slice(0,12)}</small></td><td>{gb(m.size_bytes)}</td><td><a target="_blank" rel="noreferrer" href={`https://huggingface.co/${m.repo}/tree/${m.revision}`}>{m.repo} ↗</a></td></tr>)}</tbody></table></div><p>Mở model card/license của từng nguồn trước khi xác nhận. Giọng preset và tư liệu phải có quyền sử dụng.</p><p>Dependency hệ thống: git, FFmpeg, espeak-ng, Python venv, build tools. PyTorch CUDA và ComfyUI ghim phiên bản; LLM/TTS tách môi trường.</p>{Object.entries(plan.lock.environments).map(([k,v])=><p key={k}><strong>{k}: </strong><code>{v}</code></p>)}</details>
      <label className="install-consent"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/> Tôi đã xem license, dung lượng và cho phép tải/cài bộ này lên {host.label}.</label>
      <button className="primary" disabled={busy || working || !matching || !consent || !!plan.blockers.length} onClick={()=>void run(async()=>{setStage('Đang tạo tác vụ cài…');return call(url+'/start',{plan_id:plan.plan_id,license_accepted:consent});})}>{working?'Đang xử lý · xem tiến độ bên dưới':'Cài phần còn thiếu'}</button><p>Model đúng checksum được giữ nguyên. Có thể đóng/reload trang sau khi tác vụ đã bắt đầu; lịch sử được lưu trên máy Windows.</p>
    </section>}
    <section className="panel"><h2>Thử clip kỹ thuật · không phải phim nghiệm thu</h2><p>Tạo ảnh Qwen rồi clip Wan ở chất lượng bản nháp. Chỉ kiểm tra model ảnh/video; không cần LLM/TTS, không đánh dấu toàn bộ bộ AI đã verified.</p><button title={!plan?'Kiểm tra và chuẩn bị bộ AI trước':!host.pinned_fingerprint?'Đối chiếu fingerprint trước':'GPU bận sẽ xếp hàng; máy chủ kiểm tra điều kiện an toàn'} disabled={busy || !plan || !host.pinned_fingerprint} onClick={()=>void run(()=>call(url+'/video-test',{}))}>Tạo clip thử trên GPU</button></section>
    <section className="panel"><h2>3. Kiểm chứng bằng output thật</h2><p>Tạo ảnh → chỉnh ảnh → clip ngắn → giọng Việt → kiểm tra Qwen3-VL. Chạy tuần tự, tải output về máy bạn trước khi đánh dấu verified. Bước này dùng GPU và thời gian thuê; chỉ kiểm chứng bản nháp, chưa cam kết phim dài hoặc chất lượng 720p.</p><button title={!['installed','verified','verify_failed'].includes(state.data?.status || '')?'Hoàn tất cài bộ AI trước khi kiểm chứng':'Gửi tác vụ kiểm chứng vào hàng đợi GPU'} disabled={busy || !['installed','verified','verify_failed'].includes(state.data?.status || '')} onClick={()=>void run(()=>call(url+'/verify',{}))}>Tạo output thử & kiểm chứng</button>{state.data?.verification && <p>Lần kiểm chứng gần nhất: {state.data.verification.elapsed_seconds}s. Xem và nghe output bên dưới để duyệt chất lượng.</p>}</section>
    {state.data?.jobs.map(job=><InstallJob key={job.id} job={job} disabled={busy} action={suffix=>run(()=>call(`${base}/jobs/${job.id}/${suffix}`,{}))}/>)}
  </>;
}

function InstallJob({job,disabled,action}:{job:Job;disabled:boolean;action:(suffix:string)=>unknown}){
  return <section className="panel"><div className="section-heading"><h3>{job.kind==='install'?'Cài bộ AI':'Kiểm chứng'} · {names[job.status] || job.status}</h3><span>{job.kind==='verify'?`${job.progress}%`:job.id.slice(0,8)}</span></div>{job.error && <p role="alert" className="warning-text">{job.error}</p>}<div className="actions">{active.includes(job.status) && <button disabled={disabled} onClick={()=>action('cancel')}>Ngắt tác vụ</button>}{(['interrupted','reconciling'].includes(job.status) || (job.kind==='install' && job.status==='failed')) && <button disabled={disabled} onClick={()=>action('resume')}>Kiểm tra khóa & tiếp tục</button>}</div><JobEvents id={job.id} active={active.includes(job.status)}/><div className="downloads">{job.result.artifact_ids?.map(id=><div key={id}><a href={`${base}/artifacts/${id}/file`} target="_blank" rel="noreferrer">Xem output {id.slice(0,8)} ↗</a><a href={`${base}/artifacts/${id}/file?download=true`}>Tải output</a></div>)}</div></section>;
}
