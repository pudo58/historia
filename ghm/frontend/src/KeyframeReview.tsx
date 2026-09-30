import { useEffect, useMemo, useRef, useState } from 'react';
import { type ProductionRun } from './StudioControls';
import { media, names, type StudioJob } from './workspace';

type Approve = (url: string, body: object) => Promise<void>;

/** Scenes whose shot keyframes are generated but not yet approved, in queue order. */
export function reviewQueue(run?: ProductionRun): string[] {
  if (!run) return [];
  const cp = run.checkpoint;
  const queued = [...(cp.review_pending || [])];
  if (cp.review_scene_id && !queued.includes(cp.review_scene_id)) queued.unshift(cp.review_scene_id);
  return queued.filter(id => !cp.media?.[id]?.shot_keyframes_approved && (cp.media?.[id]?.shot_keyframes?.length || 0) > 0);
}

const typing = (target: EventTarget | null) =>
  target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));

export default function KeyframeReview({ run, jobs, projectId, busy, approve }: { run: ProductionRun; jobs: StudioJob[]; projectId: string; busy: boolean; approve: Approve }) {
  const queue = reviewQueue(run);
  const [sceneId, setSceneId] = useState(queue[0]);
  const [shot, setShot] = useState(0);
  const [viewed, setViewed] = useState<Set<string>>(() => new Set());
  const panel = useRef<HTMLElement>(null);
  const current = queue.includes(sceneId) ? sceneId : queue[0];
  const index = queue.indexOf(current);
  const scene = run.snapshot.scenes.find(s => s.id === current);
  const images = run.checkpoint.media?.[current]?.shot_keyframes || [];
  const shotIndex = Math.min(shot, Math.max(images.length - 1, 0));
  const design = scene?.shot_list?.[shotIndex];
  const waitingAll = run.status === 'keyframe_review';
  const aiReview = jobs.find(j => j.kind === 'image_review' && j.scene_id === current);
  const viewedPending = useMemo(() => queue.filter(id => viewed.has(id)), [queue, viewed]);

  useEffect(() => {
    if (current) setViewed(previous => previous.has(current) ? previous : new Set(previous).add(current));
  }, [current]);

  const go = (offset: number) => {
    if (!queue.length) return;
    setSceneId(queue[(index + offset + queue.length) % queue.length]);
    setShot(0);
  };
  const approveScenes = async (ids: string[]) => {
    if (!ids.length || busy) return;
    const next = queue.find(id => !ids.includes(id) && queue.indexOf(id) > index) || queue.find(id => !ids.includes(id));
    await approve(`/api/studio/production-runs/${run.id}/approve-keyframes`, { scene_ids: ids });
    if (next) { setSceneId(next); setShot(0); }
  };

  const keys = useRef({ go, approveScenes, current, images: images.length });
  keys.current = { go, approveScenes, current, images: images.length };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey || typing(event.target) || document.querySelector('dialog[open]')) return;
      const k = keys.current;
      const key = event.key.toLowerCase();
      const inside = !!panel.current?.contains(document.activeElement);
      if (key === 'j') { event.preventDefault(); k.go(1); }
      else if (key === 'k') { event.preventDefault(); k.go(-1); }
      else if (key === 'a' && !event.shiftKey) { event.preventDefault(); void k.approveScenes([k.current]); }
      else if (inside && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) {
        event.preventDefault();
        setShot(value => Math.max(0, Math.min(k.images - 1, value + (event.key === 'ArrowRight' ? 1 : -1))));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  if (!queue.length || !scene) return null;
  return <section ref={panel} className="kf-review" id="kf-review" aria-label="Duyệt ảnh keyframe">
    <header className="kf-head">
      <div>
        <span className="work-kicker">DUYỆT ẢNH TRƯỚC KHI TẠO CLIP</span>
        <h3>{queue.length} cảnh chờ duyệt ảnh</h3>
        <p>{waitingAll
          ? 'Đã tạo xong ảnh cho cả phim. Wan chờ bạn duyệt các cảnh dưới đây rồi mới tạo clip.'
          : 'GPU vẫn đang tạo ảnh cho các cảnh còn lại. Bạn có thể duyệt dần; Wan chỉ bắt đầu khi mọi cảnh đã duyệt.'}</p>
      </div>
      <div className="kf-actions">
        <button className="work-primary" disabled={busy} onClick={() => void approveScenes([current])}>Duyệt cảnh này · A</button>
        <button disabled={busy || !viewedPending.length} onClick={() => void approveScenes(viewedPending)}
          title="Chỉ gồm các cảnh bạn đã mở xem trong phiên này">Duyệt {viewedPending.length} cảnh đã xem</button>
      </div>
    </header>
    <div className="kf-body">
      <nav className="kf-scenes" aria-label="Cảnh chờ duyệt">
        {queue.map((id, i) => {
          const item = run.snapshot.scenes.find(s => s.id === id);
          const count = run.checkpoint.media?.[id]?.shot_keyframes?.length || 0;
          return <button key={id} aria-current={id === current ? 'true' : undefined} onClick={() => { setSceneId(id); setShot(0); }}>
            <span className="kf-scene-index">{i + 1}</span>
            <span><strong>{item?.title || 'Cảnh'}</strong><small>{count} ảnh · {viewed.has(id) ? 'đã xem' : 'chưa xem'}</small></span>
          </button>;
        })}
      </nav>
      <div className="kf-stage">
        <figure className="kf-preview">
          <a href={media(images[shotIndex])} target="_blank" rel="noreferrer"><img src={media(images[shotIndex])} alt={`Shot ${shotIndex + 1} của ${scene.title}`}/></a>
          <figcaption><strong>Shot {shotIndex + 1}/{images.length}</strong>{design && <span>{[design.purpose, design.narration_excerpt].filter(Boolean).join(' · ') || 'Chưa ghi mục đích shot'}</span>}</figcaption>
        </figure>
        <div className="kf-thumbs" aria-label="Ảnh các shot">
          {images.map((image, i) => <button key={image} aria-pressed={i === shotIndex} onClick={() => setShot(i)}><img loading="lazy" src={media(image)} alt=""/><span>Shot {i + 1}</span></button>)}
        </div>
      </div>
      <aside className="kf-context">
        <h4>{scene.title}</h4>
        <p className="kf-narration">{scene.narration || 'Không có lời dẫn trong bản chụp lượt này.'}</p>
        {waitingAll
          ? <button disabled={busy || !!aiReview && ['queued', 'running', 'reconciling'].includes(aiReview.status)} onClick={() => void approve(`/api/studio/projects/${projectId}/jobs`, { kind: 'image_review', scene_id: current })}>Nhờ Qwen3-VL gắn cờ ảnh nghi lỗi · tùy chọn</button>
          : <small>Kiểm ảnh bằng Qwen3-VL mở khi đã tạo xong ảnh cả phim.</small>}
        {aiReview && <p>Kiểm ảnh AI: {names[aiReview.status] || aiReview.status}</p>}
        {aiReview?.result?.review_output != null && <pre className="review-output">{JSON.stringify(aiReview.result.review_output, null, 2)}</pre>}
      </aside>
    </div>
    <footer className="kf-keys"><kbd>J</kbd>/<kbd>K</kbd> đổi cảnh · <kbd>←</kbd>/<kbd>→</kbd> đổi ảnh (khi đang ở khung duyệt) · <kbd>A</kbd> duyệt cảnh đang xem. Ảnh đã lưu không bị tạo lại; duyệt không tự chạy lại GPU.</footer>
  </section>;
}
