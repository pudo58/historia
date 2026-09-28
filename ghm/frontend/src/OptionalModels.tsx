import {useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {read} from './workspace';

const options:[string,string][]=[['qwen-image-lightning-fp8','Qwen-Image Lightning fp8'],['rife-v4.26','RIFE 4.26']];
export default function OptionalModels({hostId}:{hostId?:string}){
  const client=useQueryClient();
  const [busy,setBusy]=useState('');
  const [error,setError]=useState('');
  const query=useQuery({queryKey:['optional-models',hostId],enabled:!!hostId,
    queryFn:()=>read<Record<string,boolean>>(`/api/studio/hosts/${hostId}/optional-models`)});
  if(!hostId)return null;
  async function install(name:string){
    setBusy(name);setError('');
    try{const response=await fetch(`/api/studio/hosts/${hostId}/optional-models/${name}`,{method:'POST'});
      if(!response.ok){const body=await response.json();throw new Error(body.detail||'Không tạo được tác vụ cài model.');}
      await client.invalidateQueries();
    }catch(e){setError((e as Error).message);}finally{setBusy('');}
  }
  return <details className="panel"><summary>Model GPU tùy chọn · cài riêng khi dùng</summary><p>Model này không tải cùng bộ nền. Việc cài sẽ tạo tác vụ có checkpoint và kiểm tra checksum; xem tiến độ ở Tác vụ. ComfyUI có thể cần khởi động lại trước khi nhận model mới.</p>{query.error&&<p role="alert">Không kiểm tra được model tùy chọn.</p>}{options.map(([id,label])=><p key={id}>{label} · {query.data?.[id]?'Đã kiểm tra checksum':'Chưa cài/kiểm tra trên GPU này'} {!query.data?.[id]&&<button disabled={!!busy} onClick={()=>void install(id)}>Cài {label}</button>}</p>)}{error&&<p role="alert">{error}</p>}</details>;
}
