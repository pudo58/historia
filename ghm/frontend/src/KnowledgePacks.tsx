import {useEffect, useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {read} from './workspace';
import {Field} from './ui';

type Role={name:string;keywords:string[];text:string};
type Pack={id:string;name:string;period:string;status:string;script:string;visual:string;roles:Role[];avoid:string;
  sources:{claim:string;ref:string}[];builtin?:boolean;version?:string;facts?:Facts;fact_counts?:Record<string,number>};
type Fact={title:string;text:string;ref?:string;year?:number|string|null};
type Facts=Record<string,Fact[]>;
const FACT_LABELS:Record<string,string>={events:'Sự kiện',people:'Nhân vật',titles_offices:'Chức quan, danh hiệu',places:'Địa danh',
  dress_appearance:'Trang phục, diện mạo',architecture_objects:'Kiến trúc, vật dụng',customs_institutions:'Phong tục, chế độ'};

function FactBrowser({facts}:{facts:Facts}){
  const keys=Object.keys(facts);
  const [kind,setKind]=useState(keys.includes('dress_appearance')?'dress_appearance':keys[0]||'');
  const [text,setText]=useState('');
  const needle=text.trim().toLowerCase();
  const all=facts[kind]||[];
  const shown=needle?all.filter(f=>`${f.title} ${f.text} ${f.year??''}`.toLowerCase().includes(needle)):all;
  const total=keys.reduce((n,k)=>n+facts[k].length,0);
  return <details className="fact-browser"><summary>Tư liệu trích từ sách ({total} mục)</summary>
    <p><small>Chỉ đọc. Khi viết kịch bản, hệ thống tự chọn các mục liên quan đến chủ đề/chương để đưa cho mô hình; trang phục luôn được ưu tiên.</small></p>
    <div className="form-grid">
      <Field label="Nhóm"><select value={kind} onChange={e=>setKind(e.target.value)}>{keys.map(k=><option key={k} value={k}>{FACT_LABELS[k]||k} ({facts[k].length})</option>)}</select></Field>
      <Field label="Tìm trong nhóm"><input value={text} onChange={e=>setText(e.target.value)} placeholder="vd: mũ, áo, 1288, Hưng Đạo"/></Field>
    </div>
    <p role="status">{shown.length} / {all.length} mục</p>
    <ul>{shown.slice(0,60).map((f,i)=><li key={i}><strong>{f.title}</strong>{f.year?` [${f.year}]`:''}: {f.text}{f.ref&&<em> — {f.ref}</em>}</li>)}</ul>
    {shown.length>60&&<p><small>Hiện 60 mục đầu; gõ từ khóa để thu hẹp.</small></p>}
  </details>;
}
type Listing={id:string;name:string;period:string;builtin:boolean;status:string;version:string};

export function KnowledgeSelect({value}:{value?:string}){
  const list=useQuery({queryKey:['knowledge'],queryFn:()=>read<Listing[]>('/api/studio/knowledge')});
  const [chosen,setChosen]=useState(value||'');   // controlled: the options arrive after the first render
  return <Field label="Gói tri thức triều đại">
    <select name="knowledge_id" value={chosen} onChange={e=>setChosen(e.target.value)}>
      <option value="">Không dùng (chỉ mô tả “Thời kỳ” ở trên)</option>
      {list.data?.map(p=><option key={p.id} value={p.id}>{p.name} · {p.period}</option>)}
    </select>
    <small>Trang phục, kiến trúc, tên gọi và chức quan của triều đại, chèn vào kịch bản và prompt ảnh. Đổi gói sẽ làm ảnh cũ không còn khớp.</small>
  </Field>;
}

export function KnowledgeEditor({packId}:{packId?:string}){
  const client=useQueryClient();
  const query=useQuery({queryKey:['knowledge',packId],enabled:!!packId,queryFn:()=>read<Pack>(`/api/studio/knowledge/${packId}`)});
  const [pack,setPack]=useState<Pack|null>(null);
  const [message,setMessage]=useState('');
  const [error,setError]=useState('');
  useEffect(()=>{if(query.data)setPack(query.data);},[query.data]);
  if(!packId)return null;
  if(!pack)return <details className="panel"><summary>Gói tri thức triều đại</summary><p>{query.error?'Không tải được gói.':'Đang tải…'}</p></details>;
  const set=(patch:Partial<Pack>)=>{setPack({...pack,...patch});setMessage('');};
  async function save(){
    setError('');setMessage('');
    const {id,name,period,status,script,visual,roles,avoid,sources}=pack as Pack;
    try{
      const response=await fetch(`/api/studio/knowledge/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id,name,period,status,script,visual,roles,avoid,sources})});
      const body=await response.json();
      if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:'Không lưu được gói tri thức.');
      setPack(body);setMessage('Đã lưu. Ảnh tạo sau lần lưu này dùng nội dung mới.');
      await client.invalidateQueries();
    }catch(e){setError((e as Error).message);}
  }
  const setRole=(index:number,patch:Partial<Role>)=>set({roles:pack.roles.map((r,n)=>n===index?{...r,...patch}:r)});
  return <details className="panel knowledge-editor"><summary>Gói tri thức: {pack.name} · {pack.period}</summary>
    <p className="alert notice">{pack.status||'Chưa có ghi chú trạng thái.'} {pack.builtin?'(Bản có sẵn; lưu sẽ tạo bản riêng của bạn.)':'(Bản riêng của bạn.)'}</p>
    <Field label="Ghi chú trạng thái / mức chắc chắn"><input value={pack.status} maxLength={200} onChange={e=>set({status:e.target.value})}/></Field>
    <Field label="Kiến thức cho người viết kịch bản (tiếng Việt: tên gọi, xưng hô, chức quan, phong tục)"><textarea rows={12} maxLength={12000} value={pack.script} onChange={e=>set({script:e.target.value})}/></Field>
    <Field label="Hình ảnh chung của thời kỳ (tiếng Anh, vào mọi prompt ảnh)"><textarea rows={4} maxLength={1200} value={pack.visual} onChange={e=>set({visual:e.target.value})}/></Field>
    <h3>Theo vai · chỉ thêm vào prompt khi cảnh nhắc đến vai đó</h3>
    {pack.roles.map((r,i)=><fieldset key={i} className="form-grid"><legend>{r.name||`Vai ${i+1}`}</legend>
      <Field label="Tên vai"><input value={r.name} maxLength={80} onChange={e=>setRole(i,{name:e.target.value})}/></Field>
      <Field label="Từ khóa nhận vai (cách nhau bằng dấu phẩy)"><input value={r.keywords.join(', ')} onChange={e=>setRole(i,{keywords:e.target.value.split(',').map(k=>k.trim()).filter(Boolean)})}/></Field>
      <div className="full"><Field label="Mô tả trang phục/đồ vật (tiếng Anh)"><textarea rows={3} maxLength={700} value={r.text} onChange={e=>setRole(i,{text:e.target.value})}/></Field></div>
      <button type="button" onClick={()=>set({roles:pack.roles.filter((_,n)=>n!==i)})}>Bỏ vai này</button></fieldset>)}
    <button type="button" onClick={()=>set({roles:[...pack.roles,{name:'',keywords:[],text:''}]})}>Thêm vai</button>
    <Field label="Cần tránh (tiếng Anh; dùng khi viết kịch bản và duyệt ảnh, không đưa vào prompt ảnh)"><textarea rows={3} maxLength={800} value={pack.avoid} onChange={e=>set({avoid:e.target.value})}/></Field>
    {query.data?.facts&&<FactBrowser facts={query.data.facts}/>}
    <details><summary>Nguồn của từng chi tiết ({pack.sources.length})</summary>
      <ul>{pack.sources.map((s,i)=><li key={i}>{s.claim} <em>— {s.ref}</em></li>)}</ul></details>
    <div className="actions"><button className="primary" onClick={()=>void save()}>Lưu gói tri thức</button></div>
    {message&&<p role="status">{message}</p>}{error&&<p role="alert" className="alert error">{error}</p>}
  </details>;
}

type Loaded={name:string;data:Record<string,unknown>;id:string;packName:string};

/** Export a pack to a .json file and import one back (a file from this app, or a bare pack object). */
export function KnowledgeTransfer({current}:{current?:string}){
  const client=useQueryClient();
  const list=useQuery({queryKey:['knowledge'],queryFn:()=>read<Listing[]>('/api/studio/knowledge')});
  const [exportId,setExportId]=useState('');
  const [file,setFile]=useState<Loaded|null>(null);
  const [newId,setNewId]=useState('');
  const [overwrite,setOverwrite]=useState(false);
  const [message,setMessage]=useState('');
  const [error,setError]=useState('');
  const chosen=exportId||current||list.data?.[0]?.id||'';
  async function pick(input:HTMLInputElement){
    const picked=input.files?.[0];
    setFile(null);setError('');setMessage('');setNewId('');setOverwrite(false);
    if(!picked)return;
    try{
      const data=JSON.parse(await picked.text()) as Record<string,unknown>;
      if(!data||typeof data!=='object'||Array.isArray(data))throw new Error('x');
      const pack=((data.pack&&typeof data.pack==='object'?data.pack:data) as Record<string,unknown>);
      setFile({name:picked.name,data,id:String(pack.id||''),packName:String(pack.name||'')});
    }catch{setError('File không đọc được: cần file .json xuất từ Historia.');}
  }
  async function importPack(){
    if(!file)return;
    setError('');setMessage('');
    try{
      const response=await fetch('/api/studio/knowledge/import',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({data:file.data,new_id:newId.trim()||null,overwrite})});
      const body=await response.json();
      if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:'Dữ liệu gói không hợp lệ.');
      setMessage(`Đã nhập gói «${body.name}» (mã ${body.id}).`);setFile(null);
      await client.invalidateQueries();
    }catch(e){setError((e as Error).message);}
  }
  return <details className="panel knowledge-transfer"><summary>Nhập / xuất gói tri thức</summary>
    <h3>Xuất</h3>
    <div className="form-grid">
      <Field label="Gói cần xuất"><select value={chosen} onChange={e=>setExportId(e.target.value)}>
        {list.data?.map(p=><option key={p.id} value={p.id}>{p.name} · {p.period}</option>)}</select></Field>
    </div>
    <p>{chosen?<a className="button" href={`/api/studio/knowledge/${chosen}/export`} download>Tải file gói tri thức (.json)</a>:'Chưa có gói nào.'}</p>
    <small>File gồm cả phần chỉnh được lẫn toàn bộ tư liệu trích từ sách, dùng để sao lưu hoặc chuyển sang máy khác.</small>
    <h3>Nhập</h3>
    <Field label="File gói tri thức (.json)"><input type="file" accept=".json,application/json" onChange={e=>void pick(e.currentTarget)}/></Field>
    {file&&<>
      <p role="status">Gói trong file: <strong>{file.packName||'(chưa đặt tên)'}</strong> · mã <code>{file.id||'?'}</code></p>
      <div className="form-grid">
        <Field label="Mã mới (để trống = giữ mã trong file)"><input value={newId} maxLength={40} placeholder="vd: tran-ban-sao" onChange={e=>setNewId(e.target.value.toLowerCase())}/></Field>
      </div>
      <label className="check"><input type="checkbox" checked={overwrite} onChange={e=>setOverwrite(e.target.checked)}/> Ghi đè nếu đã có gói cùng mã (ảnh đã tạo theo gói cũ sẽ không còn khớp)</label>
      <div className="actions"><button type="button" className="primary" onClick={()=>void importPack()}>Nhập gói</button></div></>}
    {message&&<p role="status">{message}</p>}{error&&<p role="alert" className="alert error">{error}</p>}
  </details>;
}
