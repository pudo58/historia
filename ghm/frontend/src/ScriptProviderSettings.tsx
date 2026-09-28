import {useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {read} from './workspace';

type Status={configured:boolean;url:string;model:string;has_key:boolean};

export default function ScriptProviderSettings(){
  const client=useQueryClient();
  const query=useQuery({queryKey:['script-provider'],queryFn:()=>read<Status>('/api/studio/script-provider')});
  const [key,setKey]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  async function save(form:HTMLFormElement){
    setBusy(true);setError('');
    try{
      const data=new FormData(form);
      const response=await fetch('/api/studio/script-provider',{method:'PUT',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:data.get('url'),model:data.get('model'),api_key:key})});
      if(!response.ok){const body=await response.json();throw new Error(body.detail||'Không lưu được API.');}
      setKey('');await client.invalidateQueries({queryKey:['script-provider']});
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  async function clear(){
    setBusy(true);setError('');
    try{const response=await fetch('/api/studio/script-provider',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:'',model:'',api_key:''})});if(!response.ok)throw new Error('Không tắt được API.');await client.invalidateQueries({queryKey:['script-provider']});}
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  return <details className="panel"><summary>API kịch bản · tùy chọn</summary><p>Có cấu hình thì dàn ý và kịch bản mới dùng API; không có thì dùng Qwen3-VL trên GPU. Khóa được mã hóa trong kho bí mật local. Job đã gửi giữ provider cũ; khi trạng thái không rõ sẽ chờ đối chiếu.</p><form key={query.data?.url||'empty'} className="form-grid" onSubmit={e=>{e.preventDefault();void save(e.currentTarget);}}><label className="field"><span>URL HTTPS chat completions</span><input name="url" type="url" required defaultValue={query.data?.url||''} placeholder="https://example.com/v1/chat/completions"/></label><label className="field"><span>Model</span><input name="model" required defaultValue={query.data?.model||''}/></label><label className="field"><span>API key {query.data?.has_key?'· đã lưu':''}</span><input type="password" autoComplete="new-password" required value={key} onChange={e=>setKey(e.target.value)}/></label>{error&&<p role="alert">{error}</p>}<div className="full actions"><button disabled={busy}>Lưu API</button>{query.data?.configured&&<button type="button" disabled={busy} onClick={()=>void clear()}>Tắt API, dùng Qwen3-VL</button>}</div></form></details>;
}
