"""图形界面入口的测试：流水线换成假的，界面用真的，在 offscreen 平台上把程序从启动跑到退出。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import signal  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from simul_interp.__main__ import with_default_command  # noqa: E402
from simul_interp.config import Config  # noqa: E402
from simul_interp.translate import TranslationUnit  # noqa: E402
from simul_interp.ui import app as app_module  # noqa: E402
from simul_interp.ui.overlay import Overlay  # noqa: E402


class FakeInterpreter:
    """只做流水线会对界面做的那几件事：报状态、出一句话、结束。"""

    def __init__(self, cfg, view):
        self.cfg, self.view = cfg, view
        self.state = "loading"
        self.recorder = self.transcript = None
        self.stopped = False

    def run(self):
        self.view.on_status("正在加载识别模型……")
        self.state = "listening"
        self.view.on_status("")
        self.view.on_unit(TranslationUnit(0, "Bonjour.", 0.0, translation="你好。", done_at=1.0))
        self.state = "ended"
        self.view.on_status("已结束")

    def stop(self):
        self.stopped = True

    def reconnect_audio(self):
        pass

    def output_folders(self):
        return []


@pytest.fixture
def launch(monkeypatch, tmp_path):
    """启动整个界面，返回 (run_app 的返回值, 假流水线, 控制面板, 字幕窗的设置)。

    程序该退出而没退出的话，测试会一直卡在事件循环里；这里加一道保险：10 秒还没退出就强行结束，并判测试失败。
    """
    QApplication.instance() or QApplication([])
    # 字幕窗的位置、字号存在临时文件里，不碰用户真实的设置
    settings = QSettings(str(tmp_path / "overlay.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(app_module, "Overlay", lambda ui: Overlay(ui, settings=settings))
    made = {}
    real_panel = app_module.ControlPanel
    monkeypatch.setattr(app_module, "ControlPanel", lambda it, ov: made.setdefault("panel", real_panel(it, ov)))

    def run(cfg, on_ready=None):
        def factory(view):
            made["interpreter"] = FakeInterpreter(cfg, view)
            return made["interpreter"]

        stuck = []
        guard = QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(lambda: (stuck.append(True), QApplication.exit(1)))
        guard.start(10_000)
        if on_ready is not None:  # 事件循环转起来之后要做的事（比如替用户点一下按钮）
            QTimer.singleShot(50, lambda: on_ready(made["panel"]))
        code = app_module.run_app(cfg, factory)
        guard.stop()
        made["panel"]._timer.stop()
        assert not stuck, "程序没有退出"
        return code, made["interpreter"], made["panel"], settings

    return run


def test_app_runs_from_start_to_quit(launch, monkeypatch):
    monkeypatch.setattr(app_module, "QUIT_AFTER_FILE_MS", 100)
    cfg = Config()
    cfg.audio.source = "file"  # 文件模式：流水线结束后自动退出
    handler = signal.getsignal(signal.SIGINT)
    code, interpreter, panel, settings = launch(cfg)
    assert code == 0
    assert signal.getsignal(signal.SIGINT) is handler  # Ctrl+C 的处理退出时还原
    assert interpreter.stopped
    assert panel.overlay.units[0].translation == "你好。"  # 流水线的结果通过信号到了字幕窗
    assert panel.message == "已结束" and "已结束" in panel.state_label.text()  # 状态也到了控制面板
    assert panel.overlay.on_show_panel is not None and panel.overlay.on_reconnect is not None
    assert settings.value("geometry") is not None  # 退出时保存了字幕窗的位置


@pytest.mark.parametrize("attempt", range(4))  # Qt 关窗口的先后顺序每次不一样，多跑几次才碰得到“退不掉”
def test_quit_button_really_quits(launch, attempt):
    cfg = Config()  # 内录模式：不会自动退出，只能用户自己退
    code, interpreter, panel, _ = launch(cfg, on_ready=lambda panel: panel.quit_button.click())
    assert code == 0 and interpreter.stopped
    assert not panel.isVisible() and not panel.overlay.isVisible()


def test_no_subcommand_means_run():
    assert with_default_command([]) == ["run"]  # 双击启动、python -m simul_interp
    assert with_default_command(["--set", "asr.language=en"]) == ["--set", "asr.language=en", "run"]
    assert with_default_command(["run", "--paused"]) == ["run", "--paused"]
    assert with_default_command(["--mic", "HyperX"]) == ["run", "--mic", "HyperX"]  # run 自己的选项也能直接写
    assert with_default_command(["-v", "--set", "a.b=1", "--paused"]) == ["-v", "--set", "a.b=1", "run", "--paused"]
    assert with_default_command(["bench-asr", "x.wav"]) == ["bench-asr", "x.wav"]
    assert with_default_command(["--help"]) == ["--help"] and with_default_command(["--version"]) == ["--version"]
