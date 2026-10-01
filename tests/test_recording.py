import wave

import numpy as np

from simul_interp.audio.base import CHUNK_SAMPLES, SAMPLE_RATE, AudioSource
from simul_interp.recording import WavRecorder


def read_wav(path):
    with wave.open(str(path), "rb") as f:
        assert (f.getframerate(), f.getnchannels(), f.getsampwidth()) == (SAMPLE_RATE, 1, 2)
        return np.frombuffer(f.readframes(f.getnframes()), dtype="<i2")


def tone(seconds, level=0.5):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (level * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def test_recording_is_a_playable_wav_with_the_same_audio(tmp_path):
    recorder = WavRecorder(tmp_path / "录音" / "同传_demo.wav")  # 文件夹不存在会自动建
    audio = tone(1.0)
    for i in range(0, len(audio), CHUNK_SAMPLES):
        recorder.write(audio[i : i + CHUNK_SAMPLES])
    recorder.write(np.array([2.0, -2.0, np.nan], dtype=np.float32))  # 超出范围的削平，坏数据当作 0
    recorder.close()
    pcm = read_wav(recorder.path)
    assert len(pcm) == len(audio) + 3 and recorder.seconds == len(pcm) / SAMPLE_RATE
    assert np.max(np.abs(pcm[: len(audio)] / 32767 - audio)) < 1e-4
    assert list(pcm[-3:]) == [32767, -32767, 0]
    assert not recorder.active
    recorder.write(audio)  # 结束之后再写：忽略
    assert len(read_wav(recorder.path)) == len(pcm)


def test_file_is_valid_while_still_recording(tmp_path):
    """程序被强制退出时来不及收尾：录到一半的文件也必须能正常打开。"""
    recorder = WavRecorder(tmp_path / "a.wav", sync_every_s=0.5)
    for _ in range(40):  # 40 块 × 32 毫秒 = 1.28 秒
        recorder.write(np.full(CHUNK_SAMPLES, 0.25, dtype=np.float32))
    # 不调用 close() 直接读：文件头每 0.5 秒更新一次，所以至少能读到 1 秒
    pcm = read_wav(recorder.path)
    assert 1.0 <= len(pcm) / SAMPLE_RATE <= 1.28
    assert np.all(pcm == int(0.25 * 32767))
    recorder.close()
    assert len(read_wav(recorder.path)) == 40 * CHUNK_SAMPLES


def test_existing_recording_is_never_overwritten(tmp_path):
    first = WavRecorder(tmp_path / "a.wav")
    first.write(tone(0.1))
    first.close()
    second = WavRecorder(tmp_path / "a.wav")
    second.write(tone(0.2))
    second.close()
    assert second.path.name == "a_2.wav"
    assert len(read_wav(first.path)) == int(0.1 * SAMPLE_RATE)  # 第一份原样还在


def test_empty_recording_leaves_no_file(tmp_path):
    recorder = WavRecorder(tmp_path / "a.wav")
    assert recorder.path.exists() and recorder.active
    recorder.close()
    assert not recorder.path.exists()


def test_disk_error_stops_recording_without_raising(tmp_path):
    """磁盘写满之类的错误只让录音停下，不能抛到同传的流程里去。"""
    errors = []
    recorder = WavRecorder(tmp_path / "a.wav", on_error=errors.append)
    recorder.write(tone(0.1))

    class FullDisk:
        def write(self, data):
            raise OSError("No space left on device")

        def close(self):
            pass

    real, recorder._file = recorder._file, FullDisk()
    recorder.write(tone(0.1))  # 不抛异常
    real.close()
    assert not recorder.active and recorder.error == "No space left on device"
    recorder.write(tone(0.1))  # 之后再写、再关闭都安全
    recorder.close()
    assert errors == ["No space left on device"]  # 只通知一次


def test_source_hands_every_chunk_to_the_recorder_in_stream_order(tmp_path):
    """录音挂在音频源上：识别听到的每一块都原样存下来，所以音频流时间就是录音里的位置。"""
    source = AudioSource()
    recorder = WavRecorder(tmp_path / "a.wav")
    source.on_chunk = lambda chunk: recorder.write(chunk.samples)
    ramp = np.linspace(-0.5, 0.5, 10 * CHUNK_SAMPLES, dtype=np.float32)
    for part in np.array_split(ramp, 7):  # 送入的长度不规则
        source._feed(part)
    source._finish()
    chunks = list(source.chunks())
    recorder.close()
    pcm = read_wav(recorder.path)
    assert len(chunks) == 10 and len(pcm) == len(ramp)
    for chunk in chunks:
        at = round(chunk.start * SAMPLE_RATE)
        assert np.array_equal(pcm[at : at + CHUNK_SAMPLES], (chunk.samples * 32767.0).astype("<i2"))
