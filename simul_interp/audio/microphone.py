"""麦克风外录：电脑自带的麦克风、USB 麦克风、耳机麦克风，以及能当 USB 麦克风用的录音笔。

用 sounddevice（PortAudio）打开输入设备，macOS 和 Windows 通用。不同电脑、不同设备的原始格式差别很大：
同一台 MacBook Air 上，自带麦克风是 48 kHz 单声道，无线耳机的麦克风是 16 kHz，有的虚拟设备是 44.1 kHz 双声道。
所以总是按设备自己的默认采样率和声道数打开（每个设备都支持它自己的默认格式），再混成单声道、
用 soxr 重采样到 16 kHz，而不要求设备支持某种特定的格式。

- 没有麦克风权限时，系统不报错，只送全零的静音。真实的麦克风总有一点底噪，所以连续几秒全是 0，
  就提示去检查权限、静音开关和输入音量；
- 设备被拔掉后不再有数据送来：过一会儿就重新打开。用系统默认设备时会换到新的默认设备上
  （比如拔掉耳机后回到电脑自带的麦克风）。插上新设备、或者在系统设置里换了默认设备之后，
  也可以用菜单里的“重新连接音频设备”。
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass

import numpy as np

from ..clock import now
from .base import SAMPLE_RATE, AudioSource

logger = logging.getLogger(__name__)

SILENCE_WARNING_S = 3.0  # 连续这么久全是 0：多半是没有权限，或者设备被静音了
STALL_S = 2.0  # 这么久一点数据都没有：设备多半被拔掉了，重新打开
if sys.platform == "win32":
    PERMISSION_HINT = (
        "麦克风一直没有声音：打开 设置 → 隐私和安全性 → 麦克风，打开“麦克风访问”和“让桌面应用访问你的麦克风”；"
        "也检查一下麦克风有没有被静音、输入音量是不是调到了最低。"
    )
else:
    PERMISSION_HINT = (
        "麦克风一直没有声音：多半是没有麦克风权限。打开 系统设置 → 隐私与安全性 → 麦克风，"
        "允许启动本程序的应用（比如“终端”）使用麦克风，再重新启动程序；也检查一下输入音量是不是调到了最低。"
    )

# 重新扫描设备会关掉 sounddevice 里所有开着的录音流，所以记下开着几个，有流开着时不扫描
_open_streams = 0
_scan_lock = threading.Lock()


@dataclass(frozen=True)
class InputDevice:
    index: int  # PortAudio 里的设备编号
    name: str
    channels: int  # 最多几个输入声道
    sample_rate: float  # 设备默认的采样率
    is_default: bool = False  # 是不是系统默认的输入设备


def list_input_devices(rescan: bool = False) -> list[InputDevice]:
    """系统里所有能录音的设备。rescan：重新扫描（刚插上的设备要重新扫描才看得到；有录音流开着时不扫）。

    只列默认输入设备所在的那一种驱动接口下的设备：Windows 上同一个麦克风会在 MME、DirectSound、WASAPI
    下面各出现一次，全列出来会让人不知道选哪个。MME 名字最长 31 个字，但哪台 Windows 电脑都能用。"""
    import sounddevice as sd

    with _scan_lock:
        if rescan and _open_streams == 0:
            sd._terminate()
            sd._initialize()
        try:
            default = sd.query_devices(kind="input")
        except (sd.PortAudioError, ValueError):  # 一个录音设备都没有
            return []
        devices = []
        for index, info in enumerate(sd.query_devices()):
            if info["max_input_channels"] > 0 and info["hostapi"] == default["hostapi"]:
                devices.append(
                    InputDevice(
                        index,
                        info["name"],
                        int(info["max_input_channels"]),
                        float(info["default_samplerate"]),
                        index == default["index"],
                    )
                )
        return devices


def find_input_device(spec: str = "", rescan: bool = False) -> InputDevice:
    """spec 为空：系统默认的输入设备；是数字：按编号找；否则按名字找（不分大小写，写名字的一部分就行）。"""
    devices = list_input_devices(rescan)
    if not devices:
        raise RuntimeError("这台电脑上没有找到任何麦克风（录音设备）")
    spec = spec.strip()
    if not spec:
        return next((d for d in devices if d.is_default), devices[0])
    if spec.isdigit():
        matches = [d for d in devices if d.index == int(spec)]
    else:
        lower = spec.lower()
        matches = [d for d in devices if d.name.lower() == lower] or [d for d in devices if lower in d.name.lower()]
    if not matches:
        names = "、".join(f"{d.name}（{d.index}）" for d in devices)
        raise RuntimeError(f"没有找到「{spec}」这个麦克风。现有的录音设备：{names}")
    return matches[0]


class MicrophoneSource(AudioSource):
    def __init__(self, device: str = "") -> None:
        """device：设备名的一部分或编号；空 = 系统默认的输入设备。"""
        super().__init__()
        self.device_spec = device
        self.device = find_input_device(device)  # 先找一次：设备名写错了，在加载模型之前就能报出来
        self.name = f"麦克风（{self.device.name}）"
        self._stream = None
        self._resampler = None
        self._rate = SAMPLE_RATE
        # 打开、关闭录音流用的锁。回调里不拿它：关流要等正在执行的回调结束，回调再等这把锁就会互相卡住
        self._device_lock = threading.Lock()
        self._stopped = threading.Event()
        self._last_data = 0.0  # 最近一次收到数据的时刻（clock.now）
        self._silent_s = 0.0  # 连续多久全是 0
        self._warned = False
        self._watchdog: threading.Thread | None = None

    def start(self) -> None:
        with self._device_lock:
            self._open()
        self._watchdog = threading.Thread(target=self._watch, name="mic-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        # 退出时主线程和识别线程几乎同时调用这里；同一个录音流关两次会让程序崩溃（S29），所以只关一次
        with self._device_lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
            self._close()
        if self._watchdog is not None and self._watchdog is not threading.current_thread():
            self._watchdog.join(timeout=1.0)
        self._finish()

    def reconnect(self) -> None:
        """重新找一遍设备再打开（插上了新设备，或者在系统设置里换了默认输入设备之后）。"""
        with self._device_lock:
            if self._stopped.is_set():
                return
            self._close()
            self._open(rescan=True)

    def _open(self, rescan: bool = False) -> None:
        """（持 _device_lock 调用）"""
        global _open_streams
        import sounddevice as sd
        import soxr

        device = find_input_device(self.device_spec, rescan)
        rate = int(round(device.sample_rate)) or 48000
        channels = max(1, min(device.channels, 2))  # 多声道的声卡只取前两个声道混合
        # 先把转换器准备好再开流：流一开，回调马上就会用到它
        self._rate = rate
        self._resampler = None if rate == SAMPLE_RATE else soxr.ResampleStream(rate, SAMPLE_RATE, 1, dtype="float32")
        try:
            stream = sd.InputStream(
                device=device.index, samplerate=rate, channels=channels, dtype="float32", callback=self._callback
            )
            stream.start()
        except sd.PortAudioError as exc:
            raise RuntimeError(f"打不开麦克风「{device.name}」：{exc}。{PERMISSION_HINT}") from exc
        with _scan_lock:
            _open_streams += 1
        self._stream, self.device = stream, device
        self.name = f"麦克风（{device.name}）"
        self._last_data = now()
        logger.info("开始外录：%s，原始 %d Hz %d 声道 → %d Hz", device.name, rate, channels, SAMPLE_RATE)

    def _close(self) -> None:
        """（持 _device_lock 调用）"""
        global _open_streams
        if self._stream is None:
            return
        try:
            self._stream.close(ignore_errors=True)
        except Exception as exc:  # 设备已经没了的时候关流可能报错，不要紧
            logger.debug("关闭麦克风时出错：%s", exc)
        self._stream = None
        self._resampler = None  # 也要放掉：soxr 的对象留到程序退出时还没释放，退出时会打印一串“泄漏”警告
        with _scan_lock:
            _open_streams -= 1

    def _callback(self, indata, frames, time_info, status) -> None:
        """PortAudio 的录音线程。indata 是 (帧数, 声道数) 的 float32 数组，只在回调期间有效。"""
        try:
            if status.input_overflow:
                logger.debug("麦克风数据来不及处理，丢了一小段")
            mono = indata[:, 0] if indata.shape[1] == 1 else indata.mean(axis=1)
            mono = np.ascontiguousarray(mono, dtype=np.float32)
            self._last_data = now()
            self._check_silence(mono)
            samples = mono if self._resampler is None else self._resampler.resample_chunk(mono)
            if len(samples):
                self._feed(samples)
        except Exception:  # 回调里抛出异常会让录音流停掉，所以只记下来
            logger.exception("处理麦克风数据时出错")

    def _check_silence(self, mono: np.ndarray) -> None:
        if np.any(mono):
            self._silent_s = 0.0
            if self._warned:  # 之前提示过没声音，现在有了：把提示清掉
                self._warned = False
                self._report("")
            return
        self._silent_s += len(mono) / self._rate
        if self._silent_s >= SILENCE_WARNING_S and not self._warned:
            self._warned = True
            logger.warning(PERMISSION_HINT)
            self._report(PERMISSION_HINT)

    def _watch(self) -> None:
        """看着录音流：一直没有数据送来（设备被拔掉了），就隔一会儿重新打开一次。"""
        while not self._stopped.wait(0.5):
            if now() - self._last_data < STALL_S:
                continue
            logger.warning("麦克风 %.0f 秒没有送来数据（可能被拔掉了），重新连接……", STALL_S)
            self._report("麦克风断开了，正在重新连接……")
            try:
                with self._device_lock:
                    if self._stopped.is_set():
                        return
                    self._close()
                    self._open(rescan=True)
                self._report("")
            except Exception as exc:
                logger.warning("重新连接麦克风失败，稍后再试：%s", exc)
                self._last_data = now()  # 过一会儿再试

    def _report(self, text: str) -> None:
        if self.on_status is not None:
            self.on_status(text)
