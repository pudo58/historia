import { useState } from 'react';
import { base, file, type Project } from './api';
import { Badge, Empty } from './ui';

export default function ProjectLibrary({projects, open}: {projects: Project[]; open: (id: string) => void}) {
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState('recent');
  const visible = projects.filter(p => `${p.title} ${p.era} ${p.location}`.normalize('NFC').toLocaleLowerCase('vi').includes(search.normalize('NFC').toLocaleLowerCase('vi'))).sort((a,b) => sort === 'title' ? a.title.localeCompare(b.title, 'vi') : (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0));
  return <section className="project-library" aria-label="Thư viện dự án"><div className="library-toolbar"><div><p className="eyebrow">THƯ VIỆN CỦA BẠN</p><h2>Dự án phim <span className="library-count">{projects.length}</span></h2></div><div className="library-controls"><label className="field"><span>Tìm dự án</span><input type="search" value={search} onChange={e => setSearch(e.target.value)} placeholder="Tên phim, thời kỳ, địa điểm…"/></label><label className="field"><span>Sắp xếp</span><select value={sort} onChange={e => setSort(e.target.value)}><option value="recent">Cập nhật gần nhất</option><option value="title">Tên dự án A–Z</option></select></label></div></div>{!visible.length ? <Empty title={projects.length ? 'Không tìm thấy dự án phù hợp' : 'Thước phim đầu tiên bắt đầu từ một câu chuyện'}>{projects.length ? 'Thử một tên phim, thời kỳ hoặc địa điểm khác.' : 'Tạo dự án, thêm tư liệu và soạn storyboard. Bạn chưa cần kết nối GPU ở bước này.'}</Empty> : <div className="project-grid">{visible.map((p,i) => {
    const cover = p.thumbnail_clip_id ? `${base}/artifacts/${p.thumbnail_clip_id}/thumbnail` : undefined;
    const image = p.scenes?.find(s => s.keyframe_id)?.keyframe_id || p.sources?.find(s => ['image','frame'].includes(s.kind) && s.artifact_id)?.artifact_id;
    const imageUrl = cover || (image ? file(image) : undefined);
    return <button className="project-card" key={p.id} onClick={() => open(p.id)}><div className={`project-art ${imageUrl ? 'has-image' : ''}`}>{imageUrl && <img loading="lazy" src={imageUrl} alt="" onError={e => {e.currentTarget.style.display='none';}}/>}<span>{(p.era || 'PHIM LỊCH SỬ').normalize('NFC')}</span><b>{String(i+1).padStart(2,'0')}</b><div className="film-lines"/><small>{cover ? 'Khung hình từ clip đã lưu' : image ? 'Ảnh từ cảnh đã lưu' : 'Tác phẩm đang dựng'}</small></div><div className="project-description"><Badge>{p.quality === 'draft' ? 'Bản nháp' : 'Bản chính'}</Badge><h2>{p.title.normalize('NFC')}</h2><p>{(p.era || 'Chưa chọn thời kỳ').normalize('NFC')} · {(p.location || 'Chưa chọn địa điểm').normalize('NFC')}</p><footer><span>{p.duration_seconds ?? p.duration_minutes * 60} giây · {p.aspect_ratio || '16:9'}</span><span>Mở dự án ↗</span></footer></div></button>;
  })}</div>}</section>;
}
