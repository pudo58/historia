"""Offline contract tests only; not GPU/voice quality acceptance."""
import asyncio
import json
import math
import struct
import wave
from types import SimpleNamespace

import pytest

from studio.remote_worker import inspect_wav, local_codec_path, split_speech, synthesize_segments
from studio.backend import worker_failure
from studio.packs import check_remote_model_access, REMOTE_ACCESS_SCRIPT


def wav(path, silent=False, frames=8000):
    with wave.open(str(path), 'wb') as out:
        out.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        out.writeframes(b''.join(struct.pack('<h', 0 if silent else int(4000 * math.sin(i / 10))) for i in range(frames)))


def test_pcm_requires_real_signal(tmp_path):
    path = tmp_path / 'audio.wav'
    wav(path, silent=True)
    with pytest.raises(ValueError, match='silent'):
        inspect_wav(path)
    wav(path)
    result = inspect_wav(path)
    assert result['duration'] == 1
    assert result['sample_rate'] == 8000
    assert result['non_silent']
    assert len(result['sha256']) == 64


def test_chunks_preserve_vietnamese_and_bound_size():
    text = 'Trần Hưng Đạo. ' + 'Bạch Đằng ' * 60
    chunks = split_speech(text)
    assert all(len(c) <= 240 for c in chunks)
    assert ' '.join(chunks) == ' '.join(text.split())
    with pytest.raises(ValueError):
        split_speech('  ')


def test_timestamps_are_measured_frame_boundaries(tmp_path):
    class Fake:
        def infer(self, text, voice):
            return text
        def save(self, waveform, path):
            wav(path, frames=4000 if waveform == 'Một.' else 8000)
    output = tmp_path / 'speech.wav'
    result = synthesize_segments(Fake(), 'Một. Hai.', None, output)
    assert [(s['start'], s['end']) for s in result['segments']] == [(0, .5), (.5, 1.5)]
    assert result['audio']['duration'] == 1.5
    assert result['word_aligned'] is False
    assert not list(tmp_path.glob('speech-segment-*'))


def test_remote_access_stops_safely_without_mutation():
    class Executor:
        async def run_input(self, command, data, timeout):
            assert 'SECRET' not in command
            assert json.loads(data)['token'] == 'SECRET'
            assert "method='HEAD'" in REMOTE_ACCESS_SCRIPT
            return SimpleNamespace(rc=0, stdout=json.dumps({'ok': False, 'status': 401, 'index': 0}))
    lock = {'models': [], 'snapshots': [{'repo': 'neuphonic/neucodec-onnx-decoder-int8', 'revision': 'a'*40,
                                       'files': [{'filename': 'decoder.onnx'}]}]}
    with pytest.raises(ValueError, match='401/403') as caught:
        asyncio.run(check_remote_model_access(Executor(), lock, 'SECRET'))
    assert 'SECRET' not in str(caught.value)


def test_truncated_audio_is_rejected(tmp_path):
    path = tmp_path / 'bad.wav'
    wav(path)
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(ValueError, match='Truncated'):
        inspect_wav(path)


def test_segment_failure_cleans_temporary_files(tmp_path):
    class Silent:
        def infer(self, text, voice):
            return text
        def save(self, waveform, path):
            wav(path, silent=True)
    with pytest.raises(ValueError, match='silent'):
        synthesize_segments(Silent(), 'Thử giọng.', None, tmp_path / 'speech.wav')
    assert not list(tmp_path.glob('speech-segment-*'))


def test_remote_access_success_has_explicit_origin():
    class Executor:
        async def run_input(self, command, data, timeout):
            return SimpleNamespace(rc=0, stdout=json.dumps({'ok': True, 'checked_files': 1}))
    lock = {'models': [{'repo': 'example/model', 'revision': 'a'*40, 'filename': 'config.json'}], 'snapshots': []}
    result = asyncio.run(check_remote_model_access(Executor(), lock))
    assert result == {'status': 'accessible', 'method': 'HEAD', 'origin': 'pod', 'checked_files': 1}


def test_worker_errors_do_not_leak_credentials():
    result = SimpleNamespace(rc=1, stdout='', stderr='401 SECRET https://example.test/?token=SECRET')
    message = worker_failure(result)
    assert 'SECRET' not in message
    assert 'https://' not in message
    assert 'quyền' in message


def test_local_onnx_codec_is_a_file_not_a_repo_id(tmp_path):
    codec = tmp_path / "neucodec-onnx-decoder-int8"
    codec.mkdir()
    (codec / "model.onnx").write_bytes(b"onnx")
    assert local_codec_path(codec) == codec / "model.onnx"
    assert local_codec_path("neuphonic/neucodec-onnx-decoder-int8") is None
