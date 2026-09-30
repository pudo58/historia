import { showMessage } from './messages';
import type { ShotDesign } from './ShotStoryboard';

export type Source = { id: string; title: string; kind: string; role: string; text: string; selected: boolean; description: string; status: string; artifact_id?: string };
export type Character = { id: string; name: string; kind: string; description: string; reference_ids: string[] };
export type Scene = { video_profile?: 'fast'|'quality'|null; shot_list?:ShotDesign[]; previous_shot_keyframes?:string[]; id: string; position: number; revision: number; chapter: string; title: string; narration: string; visual_prompt: string; camera: string; motion?: 'wan'|'kenburns'|'static'; image_strategy?: 'shared'|'per_shot'|'chain_last'; shorten_last_shot?: boolean; seed: number; steps?: number; keyframe_steps?: number|null; clip_steps?: number|null; character_ids: string[]; reference_ids: string[]; citations: {source_id: string; quote: string}[]; review_note: string; script_approved?: boolean; keyframe_id?: string; keyframe_approved?: boolean; speech_id?: string; clip_ids?: string[]; shot_keyframes?: string[]; rife_clip_ids?: string[]; clip_approved?: boolean; duration?: number; warnings?: string[] };
export type Project = { id: string; thumbnail_clip_id?: string | null; title: string; topic: string; era: string; location: string; duration_minutes: number; duration_seconds?: number | null; style: string; quality: string; render_profile?: 'draft' | 'standard' | null; output_resolution?: '720p' | '1080p' | '1440p' | null; aspect_ratio?: '16:9' | '9:16' | '1:1' | '4:5' | null; transition?: 'none' | 'natural' | 'dissolve' | null; upscale_method?: 'lanczos' | 'ai' | null; voice: string; tts_device?: 'cuda' | 'cpu' | null; keyframe_profile?: 'standard'|'lightning'; frame_interpolation?: 'none'|'rife24'; audio_mix_profile?: 'legacy'|'voice_duck_v1'; host_id?: string; hourly_usd?: number; pronunciation: string; updated_at: string; sources: Source[]; characters: Character[]; scenes: Scene[]; outline: {title: string; summary: string}[]; outline_approved: boolean };
export type Job = { snapshot?: {production_run_id?:string}; id: string; project_id?: string; host_id?: string; queue_position?: number | null; kind: string; status: string; progress: number; error?: string; created_at: string; result?: {artifact_ids?: string[]; voices?: {voice: string; label: string; artifact_id: string; duration: number}[]; text?: string; silent?: boolean; speech_device?: string; codec_device?: string} };
export type Host = { id: string; label: string; state: string; created_at?: string; address?: string; port?: number; gpu?: {name: string; vram_gb: number} };
export type Artifact = { id: string; name: string; media_type: string; job_id?: string };
export type Production = {target_duration_seconds:number; measured_audio_seconds:number; duration_review_required:boolean; duration_advice:string; checklist:{scene_id:string; script_approved:boolean; keyframe_valid:boolean; keyframe_approved:boolean; speech_valid:boolean; clips_valid:boolean; clip_approved:boolean; duration_seconds:number; shot_count:number}[]; timings:{job_id:string; kind:string; elapsed_seconds?:number; started_at?:string; completed_at?:string}[]};
export type Run = <T>(action: () => Promise<T>) => Promise<T | undefined>;

export async function api<T>(path: string, body?: unknown, method = body === undefined ? 'GET' : 'POST'): Promise<T> {
  const response = await fetch(path, { method, headers: body === undefined ? {} : {'content-type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) {
    const error = await response.json().catch(() => ({detail: 'Không kết nối được máy chủ local.'}));
    if (response.status === 401 && (error.code === 'login_required' || error.code === 'setup_required')) window.dispatchEvent(new Event('historia:locked'));
    throw new Error(typeof error.detail === 'string' ? showMessage(error.detail) : Array.isArray(error.detail) ? error.detail.map((e: {loc: string[]; msg: string}) => `${e.loc.join('.')}: ${e.msg}`).join('; ') : error.detail?.message || 'Yêu cầu chưa thể thực hiện. Kiểm tra trạng thái tác vụ và thử lại.');
  }
  return response.status === 204 ? undefined as T : response.json();
}
export const base = '/api/studio';
export const file = (id: string) => `${base}/artifacts/${id}/file`;
export const labels: Record<string, string> = {voice_audition: 'Nghe thử giọng', abandoned: 'Đã bỏ lượt · remote chưa rõ',install: 'Cài bộ AI', verify: 'Kiểm chứng bộ AI', video_test: 'Clip thử không tiếng', dialogue_test: 'Thử nhân vật nói', queued: 'Đang chờ', running: 'Đang xử lý', completed: 'Hoàn tất', failed: 'Cần xử lý', cancelled: 'Đã dừng', interrupted: 'Bị gián đoạn', reconciling: 'Cần đối chiếu GPU', cancelling: 'Đang dừng', keyframe: 'Tạo ảnh', speech: 'Giọng đọc', clip: 'Tạo clip', export: 'Xuất phim', outline: 'Dàn ý', script: 'Kịch bản'};
