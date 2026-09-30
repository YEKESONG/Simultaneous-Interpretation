"""macOS 内录：启动 Swift 小程序 bin/si-audio-tap，从它的 stdout 读 16 kHz float32 PCM。

小程序用 Core Audio Taps 复制系统声音，原声照常播放。这里负责：
- 需要时自动编译小程序（源码有改动才重新编译，避免每次都要重新授权）；
- 解析它在 stderr 上输出的 JSON 日志；
- 输出设备变化（插拔耳机、连蓝牙）时，小程序以退出码 3 退出，这里立刻重启它；
- 一直收到全零静音时，提示去系统设置检查权限。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from ..config import ROOT
from .base import SAMPLE_RATE, AudioSource

logger = logging.getLogger(__name__)

HELPER = ROOT / "bin" / "si-audio-tap"
HELPER_SOURCES = [ROOT / "native/macos/si_audio_tap.swift", ROOT / "native/macos/Info.plist"]
HELPER_STAMP = ROOT / "bin" / ".si-audio-tap.sha256"
BUILD_SCRIPT = ROOT / "scripts/build_macos_tap.sh"

EXIT_OUTPUT_DEVICE_CHANGED = 3
SILENCE_WARNING_S = 8.0
PERMISSION_HINT = (
    "一直收到全零的静音。如果电脑正在播放声音，多半是没有系统录音权限："
    "打开 系统设置 → 隐私与安全性 → 屏幕与系统录音，在“仅系统录音”里打开 si-audio-tap。"
)


def _sources_digest() -> str:
    digest = hashlib.sha256()
    for path in HELPER_SOURCES:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def ensure_helper() -> Path:
    """确保辅助程序已编译且与源码一致。按源码哈希判断，而不是按修改时间，
    因为重新编译会让 macOS 把它当成新程序、再要一次权限。"""
    digest = _sources_digest()
    if HELPER.exists() and HELPER_STAMP.exists() and HELPER_STAMP.read_text().strip() == digest:
        return HELPER
    if shutil.which("swiftc") is None:
        raise RuntimeError("找不到 swiftc：请先运行 xcode-select --install 安装 Xcode 命令行工具")
    logger.info("正在编译 macOS 内录辅助程序（只在源码改动后才需要）……")
    result = subprocess.run(["bash", str(BUILD_SCRIPT)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"编译 si-audio-tap 失败：\n{result.stderr}")
    HELPER_STAMP.write_text(digest)
    return HELPER


class MacSystemAudioSource(AudioSource):
    name = "macOS 系统声音"

    def __init__(self) -> None:
        super().__init__()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._started = threading.Event()
        self._heard_sound = False
        self._silent_samples = 0

    def start(self) -> None:
        binary = ensure_helper()
        self._thread = threading.Thread(target=self._run, args=(binary,), name="mac-audio-tap", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=3.0):
            logger.warning("内录还没开始，可能在等待系统录音权限：如果出现弹窗，请点“允许”。")

    def stop(self) -> None:
        self._stop_requested = True
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        if self._thread:
            self._thread.join(timeout=3.0)

    def _run(self, binary: Path) -> None:
        failures = 0
        try:
            while not self._stop_requested:
                code = self._run_helper_once(binary)
                if self._stop_requested:
                    break
                if code == EXIT_OUTPUT_DEVICE_CHANGED:
                    logger.info("输出设备变了（插拔耳机或切换蓝牙），重新开始内录")
                    continue
                failures += 1
                if failures > 5:
                    logger.error("内录辅助程序反复退出（退出码 %s），放弃", code)
                    break
                logger.warning("内录辅助程序意外退出（退出码 %s），0.5 秒后重启", code)
                time.sleep(0.5)
        finally:
            self._finish()

    def _run_helper_once(self, binary: Path) -> int:
        proc = subprocess.Popen(
            [str(binary), "--sample-rate", str(SAMPLE_RATE)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._proc = proc
        threading.Thread(target=self._read_logs, args=(proc,), name="mac-audio-tap-log", daemon=True).start()
        leftover = b""
        while True:
            data = proc.stdout.read1(8192)
            if not data:
                break
            data = leftover + data
            usable = len(data) // 4 * 4
            leftover = data[usable:]
            samples = np.frombuffer(data[:usable], dtype="<f4")
            self._check_silence(samples)
            self._feed(samples)
        return proc.wait()

    def _read_logs(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").strip()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("si-audio-tap: %s", line)
                continue
            kind = event.get("event")
            if kind == "started":
                self._started.set()
                logger.info(
                    "开始内录：输出设备「%s」，原始 %d Hz → %d Hz",
                    event.get("output_device"),
                    event.get("source_sample_rate", 0),
                    event.get("sample_rate", 0),
                )
            elif kind == "error":
                logger.error("si-audio-tap：%s", event.get("message"))
            else:
                logger.debug("si-audio-tap：%s", event)

    def _check_silence(self, samples: np.ndarray) -> None:
        if self._heard_sound:
            return
        if np.any(samples):
            self._heard_sound = True
            return
        before = self._silent_samples
        self._silent_samples += len(samples)
        limit = int(SILENCE_WARNING_S * SAMPLE_RATE)
        if before < limit <= self._silent_samples:
            logger.warning(PERMISSION_HINT)
