import { type ProductionRun } from './StudioControls';
import { names, navigate, type StudioJob } from './workspace';

type Cell = 'done' | 'running' | 'queued' | 'paused' | 'error' | 'review' | 'pending' | 'na';
type PerfScene = { scene_id: string; completed_shots: number; shot_count: number | null };

const cellText: Record<Cell, [string, string]> = {
  done: ['✓', 'Xong'], running: ['●', 'Đang chạy'], queued: ['◌', 'Chờ GPU'], paused: ['❙❙', 'Tạm dừng'],
  error: ['!', 'Cần xử lý'], review: ['◆', 'Chờ bạn duyệt'], pending: ['·', 'Chưa tới'], na: ['—', 'Không cần'],
};

function fromJob(job?: StudioJob): Cell {
  if (!job) return 'pending';
  if (job.status === 'completed') return 'done';
  if (['running', 'cancelling', 'pause_requested'].includes(job.status)) return 'running';
  if (job.status === 'queued') return 'queued';
  if (job.status === 'paused') return 'paused';
  return ['failed', 'reconciling', 'interrupted', 'cancelled'].includes(job.status) ? 'error' : 'pending';
}

export function sceneCells(run: ProductionRun, jobs: StudioJob[], sceneId: string, queue: string[]) {
  const cp = run.checkpoint;
  const out = cp.media?.[sceneId] || {};
  const job = (kind: string) => jobs.find(j => j.id === cp.jobs?.[`${kind}:${sceneId}`]);
  const scene = run.snapshot.scenes.find(s => s.id === sceneId);
  const needsReview = scene?.image_strategy === 'per_shot' || !!scene?.shot_list?.length;
  const stage = (kind: string, produced: boolean): Cell => produced ? 'done' : fromJob(job(kind));
  const clip = stage('clip', !!out.clip_ids?.length);
  const rife = stage('rife', !!out.rife_clip_ids?.length);
  // A clip job only exists once its speech and keyframes were saved (and approved), so later
  // progress implies the earlier stages even when this run reused them from an older run.
  const later = (cell: Cell) => cell === 'pending' ? 'pending' : 'done';
  const clipStarted = later(clip) === 'done' || later(rife) === 'done';
  const keyframe = clipStarted ? 'done' : stage('keyframe', !!(out.keyframe_id || out.shot_keyframes?.length));
  const speech = later(keyframe) === 'done' ? 'done' : stage('speech', !!out.speech_id);
  return {
    speech: speech as Cell,
    keyframe: keyframe as Cell,
    review: (!needsReview ? 'na' : out.shot_keyframes_approved || clipStarted ? 'done' : queue.includes(sceneId) || keyframe === 'done' ? 'review' : 'pending') as Cell,
    clip,
    rife,
  };
}

export function nextAction(run: ProductionRun | undefined, jobs: StudioJob[], queue: string[]): { text: string; tone: 'act' | 'wait' | 'done'; review?: boolean } {
  if (!run) return { text: 'Chưa có lượt sản xuất. Duyệt kịch bản ở bước 2 để bắt đầu tạo video.', tone: 'act' };
  const current = jobs.find(j => j.id === run.checkpoint.current_job_id);
  const where = current ? `${names[current.kind] || current.kind}${current.snapshot?.scene?.title ? ' · ' + current.snapshot.scene.title : ''}` : '';
  if (run.status === 'duration_review') return { text: 'Việc của bạn: lời đọc lệch thời lượng mục tiêu. Chấp nhận thời lượng hoặc sửa lời đọc.', tone: 'act' };
  if (['failed', 'reconciling'].includes(run.status)) return { text: `Việc của bạn: có bước cần xử lý${where ? ' (' + where + ')' : ''}. Xem nhật ký rồi bấm Đối chiếu & tiếp tục.`, tone: 'act' };
  if (run.status === 'keyframe_review') return { text: `Việc của bạn: duyệt ảnh ${queue.length} cảnh để Wan bắt đầu tạo clip.`, tone: 'act', review: true };
  if (run.status === 'paused') return { text: 'Lượt đang tạm dừng. Bấm Đối chiếu & tiếp tục khi sẵn sàng. Pod vẫn tính tiền khi tạm dừng.', tone: 'act' };
  if (run.status === 'completed') return { text: 'Phim đã ghép xong. Xem và kiểm chứng trước khi sử dụng.', tone: 'done' };
  if (queue.length) return { text: `GPU đang chạy${where ? ': ' + where : ''}. Trong lúc chờ, bạn có thể duyệt ảnh ${queue.length} cảnh.`, tone: 'wait', review: true };
  if (run.status === 'pause_requested') return { text: 'Đang chờ shot hiện tại xong để tạm dừng.', tone: 'wait' };
  return { text: where ? `GPU đang chạy: ${where}. Không cần thao tác.` : 'Đang chuẩn bị bước tiếp theo.', tone: 'wait' };
}

export default function SceneProgress({ run, jobs, queue, perf }: { run?: ProductionRun; jobs: StudioJob[]; queue: string[]; perf: PerfScene[] }) {
  if (!run) return null;
  const rife = run.snapshot.frame_interpolation === 'rife24';
  const rows = run.snapshot.scenes.map(scene => ({ scene, cells: sceneCells(run, jobs, scene.id, queue), perf: perf.find(p => p.scene_id === scene.id) }));
  const finished = rows.filter(r => (rife ? r.cells.rife : r.cells.clip) === 'done').length;
  const film = run.status === 'completed' ? 'done' : fromJob(jobs.find(j => j.id === run.checkpoint.jobs?.['export:film']));
  const cell = (value: Cell, extra?: string) => <td><span className={`progress-cell cell-${value}`}><span aria-hidden="true">{cellText[value][0]}</span>{cellText[value][1]}{extra && <small>{extra}</small>}</span></td>;
  return <details className="scene-progress" open>
    <summary>Tiến độ theo cảnh · <strong>{finished}/{rows.length}</strong> cảnh đủ clip · Ghép phim: {cellText[film][1].toLowerCase()}</summary>
    <div className="progress-scroll">
      <table>
        <thead><tr><th scope="col">#</th><th scope="col">Cảnh</th><th scope="col">Giọng đọc</th><th scope="col">Ảnh</th><th scope="col">Duyệt ảnh</th><th scope="col">Clip</th>{rife && <th scope="col">RIFE</th>}<th scope="col"><span className="sr-only">Mở</span></th></tr></thead>
        <tbody>{rows.map(({ scene, cells, perf }, i) => <tr key={scene.id} className={cells.review === 'review' ? 'needs-you' : undefined}>
          <td>{i + 1}</td>
          <th scope="row">{scene.title}{scene.motion && scene.motion !== 'wan' && <small>{scene.motion === 'kenburns' ? 'Ken Burns · local' : 'Ảnh tĩnh · local'}</small>}</th>
          {cell(cells.speech)}{cell(cells.keyframe)}{cell(cells.review)}
          {cell(cells.clip, perf?.shot_count ? `${perf.completed_shots}/${perf.shot_count} shot` : undefined)}
          {rife && cell(cells.rife)}
          <td><button onClick={() => navigate({ scene: scene.id, media: 'clip', shot: null })} aria-label={`Xem clip cảnh số ${i + 1}`}>Xem</button></td>
        </tr>)}</tbody>
      </table>
    </div>
  </details>;
}
