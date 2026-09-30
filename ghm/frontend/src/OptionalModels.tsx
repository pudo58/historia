import {useState} from 'react';
import {useQuery, useQueryClient} from '@tanstack/react-query';
import {read} from './workspace';

const options:[string,string,string?][]=[['qwen-image-lightning-fp8','Qwen-Image Lightning fp8'],['rife-v4.26','RIFE 4.26'],
  ['wan-s2v','Wan2.2 S2V · nhân vật nói (3 file, ≈18,3 GB)','Gồm model S2V fp8 (≈16,4 GB), bộ mã hóa âm thanh wav2vec2 (≈0,63 GB) và LoRA Lightning 4 bước (≈1,23 GB). Cần ít nhất 19 GB trống trên ổ model của Pod; chỉ tải khi bạn bấm cài.']];
export default function OptionalModels({hostId,open}:{hostId?:string;open?:boolean}){
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
  return <details className="panel" open={open||undefined}><summary>Model GPU tùy chọn · cài riêng khi dùng</summary><p>Model này không tải cùng bộ nền. Việc cài sẽ tạo tác vụ có checkpoint và kiểm tra checksum; xem tiến độ ở Tác vụ. ComfyUI có thể cần khởi động lại trước khi nhận model mới.</p>{query.error&&<p role="alert">Không kiểm tra được model tùy chọn.</p>}{options.map(([id,label,note])=><p key={id}>{label} · {query.data?.[id]?'Đã kiểm tra checksum':'Chưa cài/kiểm tra trên GPU này'} {!query.data?.[id]&&<button disabled={!!busy} onClick={()=>void install(id)}>Cài {label}</button>}{note&&!query.data?.[id]&&<><br/><small>{note}</small></>}</p>)}{error&&<p role="alert">{error}</p>}</details>;
}
