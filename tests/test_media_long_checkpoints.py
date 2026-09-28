"""Short local stand-in for long timeline checkpoint boundaries, not GPU acceptance."""
import json
import pytest
from studio.media import render_film, run_ffmpeg, probe


def test_fractional_audio_across_multiple_chapter_checkpoints(tmp_path):
    audio, clip = tmp_path / 'speech.wav', tmp_path / 'shot.mp4'
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.371', str(audio)])
    run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=24:d=0.8', '-c:v', 'libx264', str(clip)])
    scene = {'audio': audio, 'clips': [clip], 'narration': 'Hello.'}
    output = render_film([scene] * 13, tmp_path / 'out', quality='draft')
    info = probe(output['video'])
    assert info['audio_duration'] == pytest.approx(.371 * 13, abs=.05)
    assert info['video_duration'] == pytest.approx(.371 * 13, abs=.09)
    assert len(list((tmp_path / 'out').glob('chapter-*.mkv'))) == 2
    assert len(json.loads(output['metadata'].read_text())['subtitle_alignment']) == 13
