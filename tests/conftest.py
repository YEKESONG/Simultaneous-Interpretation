"""所有测试共用的设置。"""

import pytest

from simul_interp.audio.microphone import InputDevice

FAKE_MICS = [
    InputDevice(1, "HyperX Cloud III Wireless", 1, 16000.0),
    InputDevice(2, "MacBook Air麦克风", 1, 48000.0, is_default=True),
]


@pytest.fixture(autouse=True)
def no_real_microphones(monkeypatch):
    """控制面板会列出电脑上的麦克风：测试里一律换成这两个假的，不碰真实的录音设备（CI 上也没有）。"""
    import simul_interp.ui.panel as panel

    monkeypatch.setattr(panel, "input_devices", lambda: list(FAKE_MICS))
