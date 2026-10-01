import {useEffect, useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {read} from './workspace';
import {Field} from './ui';

type Role={name:string;keywords:string[];text:string};
type Pack={id:string;name:string;period:string;status:string;script:string;visual:string;roles:Role[];avoid:string;
  sources:{claim:string;ref:string}[];builtin?:boolean;version?:string};
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
    <Field label="Kiến thức cho người viết kịch bản (tiếng Việt: tên gọi, xưng hô, chức quan, phong tục)"><textarea rows={12} maxLength={8000} value={pack.script} onChange={e=>set({script:e.target.value})}/></Field>
    <Field label="Hình ảnh chung của thời kỳ (tiếng Anh, vào mọi prompt ảnh)"><textarea rows={4} maxLength={1200} value={pack.visual} onChange={e=>set({visual:e.target.value})}/></Field>
    <h3>Theo vai · chỉ thêm vào prompt khi cảnh nhắc đến vai đó</h3>
    {pack.roles.map((r,i)=><fieldset key={i} className="form-grid"><legend>{r.name||`Vai ${i+1}`}</legend>
      <Field label="Tên vai"><input value={r.name} maxLength={80} onChange={e=>setRole(i,{name:e.target.value})}/></Field>
      <Field label="Từ khóa nhận vai (cách nhau bằng dấu phẩy)"><input value={r.keywords.join(', ')} onChange={e=>setRole(i,{keywords:e.target.value.split(',').map(k=>k.trim()).filter(Boolean)})}/></Field>
      <div className="full"><Field label="Mô tả trang phục/đồ vật (tiếng Anh)"><textarea rows={3} maxLength={700} value={r.text} onChange={e=>setRole(i,{text:e.target.value})}/></Field></div>
      <button type="button" onClick={()=>set({roles:pack.roles.filter((_,n)=>n!==i)})}>Bỏ vai này</button></fieldset>)}
    <button type="button" onClick={()=>set({roles:[...pack.roles,{name:'',keywords:[],text:''}]})}>Thêm vai</button>
    <Field label="Cần tránh (tiếng Anh; dùng khi viết kịch bản và duyệt ảnh, không đưa vào prompt ảnh)"><textarea rows={3} maxLength={800} value={pack.avoid} onChange={e=>set({avoid:e.target.value})}/></Field>
    <details><summary>Nguồn của từng chi tiết ({pack.sources.length})</summary>
      <ul>{pack.sources.map((s,i)=><li key={i}>{s.claim} <em>— {s.ref}</em></li>)}</ul></details>
    <div className="actions"><button className="primary" onClick={()=>void save()}>Lưu gói tri thức</button></div>
    {message&&<p role="status">{message}</p>}{error&&<p role="alert" className="alert error">{error}</p>}
  </details>;
}
