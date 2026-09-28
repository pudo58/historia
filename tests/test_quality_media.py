"""CPU-only checks for local modes and RIFE timing; no GPU quality claim."""
import json

from PIL import Image

from studio.media import probe, render_film, render_rife24, render_still_clip, run_ffmpeg


def test_local_static_and_kenburns_cover_audio(tmp_path):
    image = tmp_path / 'frame.png'
    Image.new('RGB', (128, 72), '#456789').save(image)
    for motion in ('static', 'kenburns'):
        path = tmp_path / f'{motion}.mp4'
        render_still_clip(image, path, 1.35, (128, 72), motion)
        info = probe(path)
        assert info['fps'] == 24
        assert info['duration'] >= 1.35
        assert info['frames'] >= 33


def test_rife_downsample_preserves_source_duration(tmp_path):
    source = tmp_path / 'wan.mp4'
    interpolated = tmp_path / 'rife48.mp4'
    target = tmp_path / 'rife24.mp4'
    run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=128x72:r=16:d=5.0625',
                '-frames:v', '81', '-c:v', 'libx264', str(source)])
    run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=128x72:r=48:d=5.0208333',
                '-frames:v', '241', '-c:v', 'libx264', str(interpolated)])
    render_rife24(interpolated, target, source)
    result = probe(target)
    assert result['fps'] == 24
    assert result['duration'] + .05 >= probe(source)['duration']


def test_opt_in_music_ducking_exports_duration_and_loudness(tmp_path):
    voice, music, clip = (tmp_path / name for name in ('voice.wav', 'music.wav', 'shot.mp4'))
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.2', str(voice)])
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=220:duration=1.2', str(music)])
    run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=128x72:r=24:d=1.4',
                '-c:v', 'libx264', str(clip)])
    result = render_film([{'audio': voice, 'clips': [clip], 'narration': 'Thử nhạc.'}],
        tmp_path / 'export', music, quality='draft',
        project_settings={'audio_mix_profile': 'voice_duck_v1'})
    info = probe(result['video'])
    metadata = json.loads(result['metadata'].read_text(encoding='utf-8'))
    assert info['audio'] and abs(info['duration'] - 1.2) < .1
    assert metadata['audio_mix_profile'] == 'voice_duck_v1'
    assert -19 < metadata['loudness']['integrated_lufs'] < -13
    assert metadata['loudness']['true_peak_dbfs'] <= -1.3
