"""控制面板的测试：流水线换成一个假的（只记下被要求做了什么），字幕窗用真的，在 offscreen 平台上跑。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from simul_interp.config import Config, UiConfig  # noqa: E402
from simul_interp.ui.overlay import Overlay  # noqa: E402
from simul_interp.ui.panel import ControlPanel  # noqa: E402


class FakeInterpreter:
    def __init__(self):
        self.cfg = Config()
        self.state = "loading"
        self.calls = []
        self.recorder = None
        self.transcript = None

    def pause(self):
        self.calls.append("pause")
        self.state = "paused"

    def resume(self):
        self.calls.append("resume")
        self.state = "listening"

    def set_languages(self, source, target):
        self.calls.append((source, target))

    def set_source(self, kind, device=""):
        self.calls.append(("source", kind, device))
        if device == "坏掉的麦克风":
            raise RuntimeError("打不开麦克风")
        self.cfg.audio.source, self.cfg.audio.device = kind, device

    def output_folders(self):
        return [Path("某个文件夹")] if self.transcript else []


@pytest.fixture
def setup(tmp_path):
    QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "overlay.ini"), QSettings.Format.IniFormat)
    overlay = Overlay(UiConfig(), settings=settings)
    overlay.show()
    interpreter = FakeInterpreter()
    panel = ControlPanel(interpreter, overlay)
    panel.show()
    QApplication.processEvents()
    yield panel, interpreter, overlay
    panel._timer.stop()
    panel.deleteLater()
    overlay.close()
    QApplication.processEvents()


def test_button_follows_the_real_state(setup):
    panel, interpreter, _ = setup
    assert not panel.toggle.isEnabled() and "正在准备" in panel.state_label.text()  # 模型还在加载：按钮先不能点
    interpreter.state = "listening"
    panel.sync()
    assert panel.toggle.isEnabled() and panel.toggle.text() == "暂停监听"
    panel.toggle.click()
    assert interpreter.calls == ["pause"] and panel.toggle.text() == "开始监听"
    assert "已暂停" in panel.state_label.text()
    panel.toggle.click()
    assert interpreter.calls == ["pause", "resume"] and panel.toggle.text() == "暂停监听"
    interpreter.state = "ended"
    panel.sync()
    assert not panel.toggle.isEnabled()


def test_languages_start_from_the_config_and_changes_are_passed_on(setup):
    panel, interpreter, _ = setup
    assert panel.source.currentText() == "法语" and panel.target.currentText() == "简体中文"
    assert interpreter.calls == []  # 只是摆出当前的语言，不算切换
    panel.source.setCurrentIndex(panel.source.findData("en"))
    assert interpreter.calls == [("en", "简体中文")]
    panel.target.setCurrentText("English")
    assert interpreter.calls[-1] == ("en", "English")


def test_language_outside_the_list_is_still_shown(tmp_path):
    QApplication.instance() or QApplication([])
    interpreter = FakeInterpreter()
    interpreter.cfg.asr.language, interpreter.cfg.translate.target_language = "nl", "한국어"  # config.toml 里写的
    overlay = Overlay(UiConfig(), settings=QSettings(str(tmp_path / "o.ini"), QSettings.Format.IniFormat))
    panel = ControlPanel(interpreter, overlay)
    assert panel.source.currentData() == "nl" and panel.target.currentText() == "한국어"
    panel._timer.stop()


def test_overlay_can_be_hidden_and_collapsed_from_the_panel(setup):
    panel, _, overlay = setup
    assert panel.show_overlay.isChecked() and panel.collapse.text() == "收起字幕窗"
    panel.collapse.click()
    panel.sync()
    assert overlay.collapsed and panel.collapse.text() == "展开字幕窗"
    panel.show_overlay.setChecked(False)
    assert not overlay.isVisible()
    panel.sync()
    assert not panel.show_overlay.isChecked() and not panel.collapse.isEnabled()  # 藏起来了就谈不上收起 / 展开
    overlay.show()  # 从别处（比如菜单栏图标）又显示出来：面板跟着变
    panel.sync()
    assert panel.show_overlay.isChecked()


def test_status_message_and_files_are_shown(setup):
    panel, interpreter, _ = setup
    panel.set_message("正在加载识别模型……")
    assert panel.detail.text() == "正在加载识别模型……"
    assert "这次没有开" in panel.files.text() and not panel.open_folder.isEnabled()

    class Saved:
        path = Path("录音文件") / "同传_2026-10-13_090000_译文.md"
        active = True

    interpreter.transcript = interpreter.recorder = Saved()
    interpreter.state = "paused"
    panel.set_message("已暂停")
    assert "同传_2026-10-13_090000_译文.md" in panel.files.text() and panel.open_folder.isEnabled()
    assert "录音也跟着停" in panel.detail.text()  # 暂停时提示一句暂停意味着什么


def test_closing_the_panel_only_hides_it(setup):
    panel, _, overlay = setup
    assert panel.close()  # 必须同意关闭：拒绝的话，Qt 会把整个程序的退出也一起取消
    assert not panel.isVisible() and overlay.isVisible()  # 同传还在跑，字幕窗也还在
    panel.bring_to_front()
    assert panel.isVisible()


def wait_until(condition, timeout=2.0):
    import time

    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()


def test_audio_input_lists_system_sound_and_every_microphone(setup):
    panel, _, _ = setup
    combo = panel.audio_input
    items = [combo.itemText(i) for i in range(combo.count())]
    assert items == [
        "电脑播放的声音",
        "麦克风：系统默认（MacBook Air麦克风）",
        "麦克风：HyperX Cloud III Wireless",
        "麦克风：MacBook Air麦克风",
    ]
    assert combo.currentText() == "电脑播放的声音"  # 配置里默认是内录


def test_switching_the_audio_input_runs_in_the_background(setup):
    panel, interpreter, _ = setup
    interpreter.state = "listening"
    combo = panel.audio_input
    combo.setCurrentIndex(combo.findData("mic:HyperX Cloud III Wireless"))
    panel.apply_input()  # 用户在下拉框里选了一项
    assert wait_until(lambda: not panel._switching)
    assert interpreter.calls[-1] == ("source", "mic", "HyperX Cloud III Wireless")
    assert combo.currentText() == "麦克风：HyperX Cloud III Wireless" and panel.detail.text() == ""


def test_failed_switch_goes_back_to_the_input_in_use_and_says_why(setup):
    panel, interpreter, _ = setup
    interpreter.state = "listening"
    combo = panel.audio_input
    combo.addItem("麦克风：坏掉的麦克风", "mic:坏掉的麦克风")
    combo.setCurrentIndex(combo.count() - 1)
    panel.apply_input()
    assert wait_until(lambda: not panel._switching)
    assert combo.currentText() == "电脑播放的声音"  # 换回实际在用的
    assert "切换音源失败：打不开麦克风" in panel.detail.text()


def test_microphone_named_in_the_config_is_selected(tmp_path):
    QApplication.instance() or QApplication([])
    interpreter = FakeInterpreter()
    interpreter.cfg.audio.source, interpreter.cfg.audio.device = "mic", "hyperx"  # 名字的一部分
    overlay = Overlay(UiConfig(), settings=QSettings(str(tmp_path / "o.ini"), QSettings.Format.IniFormat))
    panel = ControlPanel(interpreter, overlay)
    assert panel.audio_input.currentText() == "麦克风：HyperX Cloud III Wireless"
    interpreter.cfg.audio.source, interpreter.cfg.audio.file = "file", "samples/demo.wav"
    panel.refresh_inputs()
    panel.sync()
    assert panel.audio_input.currentText() == "音频文件：demo.wav" and not panel.audio_input.isEnabled()
    panel._timer.stop()
