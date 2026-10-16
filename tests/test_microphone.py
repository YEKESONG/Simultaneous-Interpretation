"""麦克风外录的测试：用一个假的 sounddevice 模块模拟各种设备，不需要真的麦克风，CI 上也能跑。"""

import sys
import threading
import time
import types

import numpy as np
import pytest

from simul_interp.audio import microphone
from simul_interp.audio.base import SAMPLE_RATE
from simul_interp.audio.microphone import MicrophoneSource, find_input_device, list_input_devices

pytest.importorskip("soxr")


class FakeStatus:
    input_overflow = False


class FakeSoundDevice(types.ModuleType):
    """记下打开过的录音流；devices 是 query_devices() 的返回值，default 是默认输入设备的编号。"""

    class PortAudioError(Exception):
        pass

    def __init__(self, devices, default=0, fail_start=False):
        super().__init__("sounddevice")
        self.devices, self.default, self.fail_start = devices, default, fail_start
        self.streams, self.rescans = [], 0
        fake = self

        class InputStream:
            def __init__(self, device, samplerate, channels, dtype, callback):
                self.device, self.samplerate, self.channels, self.callback = device, samplerate, channels, callback
                self.closes = 0
                fake.streams.append(self)

            def start(self):
                if fake.fail_start:
                    raise fake.PortAudioError("Error opening InputStream: Internal PortAudio error")

            def close(self, ignore_errors=True):
                self.closes += 1

            def push(self, frames):
                self.callback(np.asarray(frames, dtype=np.float32), len(frames), None, FakeStatus())

        self.InputStream = InputStream

    def query_devices(self, kind=None):
        if kind == "input":
            if self.default is None:
                raise self.PortAudioError("no default input device")
            return {**self.devices[self.default], "index": self.default}
        return self.devices

    def _terminate(self):
        self.rescans += 1

    def _initialize(self):
        pass


def device(name, channels=1, rate=48000.0, hostapi=0):
    return {"name": name, "max_input_channels": channels, "default_samplerate": rate, "hostapi": hostapi}


@pytest.fixture
def fake_sd(monkeypatch):
    def install(devices, default=0, **kwargs):
        sd = FakeSoundDevice(devices, default, **kwargs)
        monkeypatch.setitem(sys.modules, "sounddevice", sd)
        monkeypatch.setattr(microphone, "_open_streams", 0)
        return sd

    return install


def tone(freq, seconds, rate, channels=1, amplitude=0.3):
    t = np.arange(int(seconds * rate)) / rate
    wave = (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.repeat(wave[:, None], channels, axis=1)


def drain(source):
    """取出音源已经送出的所有音频块，拼成一段。"""
    blocks = []
    while not source._queue.empty():
        item = source._queue.get()
        if item is not None:
            blocks.append(item.samples)
    return np.concatenate(blocks) if blocks else np.zeros(0, np.float32)


def push_in_blocks(stream, frames, block=480):
    for i in range(0, len(frames), block):
        stream.push(frames[i : i + block])


def test_device_is_opened_in_its_own_format_and_converted_to_16k_mono(fake_sd):
    sd = fake_sd([device("MacBook Air麦克风", channels=1, rate=48000.0)])
    source = MicrophoneSource()
    source.start()
    stream = sd.streams[0]
    assert (stream.samplerate, stream.channels) == (48000, 1)  # 按设备自己的格式打开
    push_in_blocks(stream, tone(440, 1.0, 48000))
    audio = drain(source)
    source.stop()
    # 1 秒的声音还是 1 秒左右：采样率转换器要预读几十毫秒，不满一块（32 毫秒）的零头也还没送出
    assert 0 <= SAMPLE_RATE - len(audio) < 0.1 * SAMPLE_RATE
    peak = np.argmax(np.abs(np.fft.rfft(audio))) * SAMPLE_RATE / len(audio)
    assert abs(peak - 440) < 5  # 音高不变（重采样对了）
    assert 0.25 < np.abs(audio[len(audio) // 4 :]).max() < 0.35  # 音量不变


@pytest.mark.parametrize("rate", [44100.0, 96000.0, 8000.0])
def test_other_sample_rates_are_converted_too(fake_sd, rate):
    sd = fake_sd([device("声卡", channels=1, rate=rate)])
    source = MicrophoneSource()
    source.start()
    push_in_blocks(sd.streams[0], tone(300, 1.0, int(rate)), block=int(rate // 100))
    audio = drain(source)
    source.stop()
    assert 0 <= SAMPLE_RATE - len(audio) < 0.1 * SAMPLE_RATE
    assert abs(np.argmax(np.abs(np.fft.rfft(audio))) * SAMPLE_RATE / len(audio) - 300) < 5


def test_stereo_is_mixed_and_16k_devices_are_passed_through(fake_sd):
    sd = fake_sd([device("无线耳机", channels=2, rate=16000.0)])
    source = MicrophoneSource()
    source.start()
    frames = np.stack([np.full(1024, 0.2, np.float32), np.full(1024, 0.4, np.float32)], axis=1)
    sd.streams[0].push(frames)
    audio = drain(source)
    source.stop()
    assert sd.streams[0].channels == 2
    assert np.allclose(audio, 0.3)  # 两个声道取平均；本来就是 16 kHz，不经过重采样


def test_multichannel_interfaces_only_use_two_channels(fake_sd):
    sd = fake_sd([device("8 路声卡", channels=8, rate=48000.0)])
    source = MicrophoneSource()
    source.start()
    source.stop()
    assert sd.streams[0].channels == 2


def test_device_can_be_chosen_by_name_or_number(fake_sd):
    fake_sd(
        [device("HyperX Cloud III Wireless", rate=16000.0), device("MacBook Air麦克风"), device("扬声器", channels=0)],
        default=1,
    )
    assert find_input_device("").name == "MacBook Air麦克风"  # 不写：系统默认
    assert find_input_device("hyperx").name == "HyperX Cloud III Wireless"  # 名字的一部分，不分大小写
    assert find_input_device("0").name == "HyperX Cloud III Wireless"  # 编号
    with pytest.raises(RuntimeError, match="现有的录音设备：HyperX Cloud III Wireless（0）、MacBook Air麦克风（1）"):
        find_input_device("扬声器")  # 只能录音的设备才算
    assert MicrophoneSource("air").name == "麦克风（MacBook Air麦克风）"


def test_only_devices_of_the_default_driver_are_listed(fake_sd):
    """Windows 上同一个麦克风会在几种驱动接口下各出现一次。"""
    fake_sd([device("麦克风 (Realtek)", hostapi=0), device("麦克风 (Realtek Audio)", hostapi=2)], default=0)
    assert [d.name for d in list_input_devices()] == ["麦克风 (Realtek)"]


def test_no_microphone_at_all_is_reported_clearly(fake_sd):
    fake_sd([device("扬声器", channels=0)], default=None)
    assert list_input_devices() == []
    with pytest.raises(RuntimeError, match="没有找到任何麦克风"):
        MicrophoneSource()


def test_device_that_cannot_be_opened_explains_what_to_check(fake_sd):
    fake_sd([device("MacBook Air麦克风")], fail_start=True)
    with pytest.raises(RuntimeError, match="打不开麦克风「MacBook Air麦克风」.*麦克风"):
        MicrophoneSource().start()


def test_all_zero_input_suggests_checking_the_permission(fake_sd):
    """没有权限时系统只送全零；真实的麦克风总有底噪，所以几秒全零就提示，有声音了再把提示清掉。"""
    sd = fake_sd([device("MacBook Air麦克风")])
    source = MicrophoneSource()
    reports = []
    source.on_status = reports.append
    source.start()
    push_in_blocks(sd.streams[0], np.zeros((int(48000 * 2.5), 1), np.float32))
    assert reports == []  # 刚开始安静几秒很正常
    push_in_blocks(sd.streams[0], np.zeros((48000, 1), np.float32))
    assert len(reports) == 1 and "隐私" in reports[0]
    push_in_blocks(sd.streams[0], np.zeros((48000, 1), np.float32))
    assert len(reports) == 1  # 只提示一次
    push_in_blocks(sd.streams[0], tone(440, 0.1, 48000, amplitude=0.01))
    assert reports[-1] == ""  # 有声音了：提示清掉
    source.stop()


def test_unplugged_device_is_reopened(fake_sd, monkeypatch):
    monkeypatch.setattr(microphone, "STALL_S", 0.2)
    sd = fake_sd([device("USB 麦克风")])
    source = MicrophoneSource()
    reports = []
    source.on_status = reports.append
    source.start()
    deadline = time.monotonic() + 3
    while len(sd.streams) < 2 and time.monotonic() < deadline:  # 一直没有数据：重新扫描设备、重新打开
        time.sleep(0.05)
    source.stop()
    assert len(sd.streams) >= 2 and sd.streams[0].closes == 1
    assert sd.rescans >= 1  # 重新扫描时，之前那个流已经关了
    assert "重新连接" in reports[0] and "" in reports


def test_stop_closes_the_stream_once_even_if_called_from_two_threads(fake_sd):
    sd = fake_sd([device("MacBook Air麦克风")])
    source = MicrophoneSource()
    source.start()
    threads = [threading.Thread(target=source.stop) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sd.streams[0].closes == 1  # 同一个流关两次会让程序崩溃（S29）
    assert list(source.chunks()) == []  # 音源结束：识别线程的循环能退出
    assert microphone._open_streams == 0


def test_reconnect_reopens_the_device(fake_sd):
    sd = fake_sd([device("MacBook Air麦克风")])
    source = MicrophoneSource()
    source.start()
    source.reconnect()
    assert len(sd.streams) == 2 and sd.streams[0].closes == 1 and sd.rescans == 1
    source.stop()
    source.reconnect()  # 停了以后不再打开
    assert len(sd.streams) == 2


def test_mic_option_and_devices_command(fake_sd, capsys):
    from simul_interp.__main__ import build_parser, get_config, main, with_default_command

    cfg = get_config(build_parser().parse_args(with_default_command(["run", "--mic"])))
    assert (cfg.audio.source, cfg.audio.device) == ("mic", "")
    cfg = get_config(build_parser().parse_args(with_default_command(["--mic", "HyperX"])))
    assert (cfg.audio.source, cfg.audio.device) == ("mic", "HyperX")
    fake_sd([device("HyperX Cloud III Wireless", rate=16000.0), device("MacBook Air麦克风")], default=1)
    assert main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "* [1] MacBook Air麦克风（1 声道，48 kHz）" in out and "[0] HyperX Cloud III Wireless（1 声道，16 kHz）" in out
