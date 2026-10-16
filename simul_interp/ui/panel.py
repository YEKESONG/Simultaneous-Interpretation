"""控制面板：选音源和语言、开始 / 暂停监听、管理字幕窗。

它只是一个遥控器：真正干活的是 Interpreter（后台线程里的同传流水线）和 Overlay（字幕窗）。
面板每 0.2 秒看一眼它们的状态，把按钮和文字改成对应的样子。所以状态是从哪里变的
（面板、字幕窗的右键菜单、菜单栏图标）都不要紧，面板总是显示真实情况。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..languages import SOURCE_LANGUAGES, TARGET_LANGUAGES, language_name

logger = logging.getLogger(__name__)

# 状态 → (显示的文字, 圆点的颜色)
STATES = {
    "loading": ("正在准备……", "#d9a441"),
    "listening": ("正在监听", "#3fb968"),
    "paused": ("已暂停", "#9a9a9a"),
    "ended": ("已结束", "#9a9a9a"),
}
BUTTON_STYLE = (
    "QPushButton {{ font-size: 17px; font-weight: 600; padding: 12px; border-radius: 10px;"
    " color: white; background: {color}; }}"
    "QPushButton:disabled {{ background: #8c8c8c; color: #e6e6e6; }}"
)


def input_devices() -> list:
    """能用来外录的麦克风；列不出来时（没装 sounddevice、系统没有录音设备）返回空列表，只能用内录。
    每次都重新扫描，刚插上的设备也能看到（有麦克风开着时不扫描，见 list_input_devices）。"""
    try:
        from ..audio.microphone import list_input_devices

        return list_input_devices(rescan=True)
    except Exception as exc:
        logger.warning("列不出麦克风：%s", exc)
        return []


class DeviceCombo(QComboBox):
    """打开下拉列表之前先刷新一遍选项。"""

    def __init__(self, before_open) -> None:
        super().__init__()
        self._before_open = before_open

    def showPopup(self) -> None:
        self._before_open()
        super().showPopup()


class ControlPanel(QWidget):
    switched = Signal(str)  # 换音源结束（在后台线程里发出）：空字符串 = 成功，否则是失败的原因

    def __init__(self, interpreter, overlay) -> None:
        """interpreter：同传流水线（pipeline.Interpreter）；overlay：字幕窗（ui.overlay.Overlay）。"""
        super().__init__()
        self.interpreter = interpreter
        self.overlay = overlay
        self.message = ""  # 流水线最近报告的一句状态（比如“正在加载识别模型……”或出错信息）
        self.setWindowTitle("实时同传")
        self.setFixedWidth(360)

        cfg = interpreter.cfg
        # 音源：电脑播放的声音，或者某一个麦克风。选项的数据是 "system"、"mic:"（系统默认的麦克风）、"mic:设备名"
        self.audio_input = DeviceCombo(self.refresh_inputs)
        self.audio_input.activated.connect(self.apply_input)  # 只响应用户自己选，程序改选项时不触发
        self._switching = False
        self.switched.connect(self._on_switched)
        self.refresh_inputs()

        self.source = QComboBox()
        for code, name in SOURCE_LANGUAGES.items():
            self.source.addItem(name, code)
        if cfg.asr.language not in SOURCE_LANGUAGES:  # 配置里写了表外的语言：照样列出来
            self.source.addItem(language_name(cfg.asr.language), cfg.asr.language)
        self.source.setCurrentIndex(self.source.findData(cfg.asr.language))
        self.target = QComboBox()
        self.target.addItems(TARGET_LANGUAGES)
        if cfg.translate.target_language not in TARGET_LANGUAGES:
            self.target.addItem(cfg.translate.target_language)
        self.target.setCurrentText(cfg.translate.target_language)
        self.source.currentIndexChanged.connect(self.apply_languages)
        self.target.currentIndexChanged.connect(self.apply_languages)

        self.toggle = QPushButton()
        self.toggle.clicked.connect(self.toggle_listening)
        self.state_label = QLabel()
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color: #808080;")

        self.show_overlay = QCheckBox("显示字幕窗")
        self.show_overlay.toggled.connect(self.overlay.setVisible)
        self.collapse = QPushButton()
        self.collapse.clicked.connect(self.overlay.toggle_collapsed)

        self.files = QLabel()
        self.files.setWordWrap(True)
        self.files.setStyleSheet("color: #808080;")
        self.open_folder = QPushButton("打开记录和录音所在的文件夹")
        self.open_folder.clicked.connect(self.open_folders)
        self.quit_button = QPushButton("退出")
        self.quit_button.clicked.connect(QApplication.instance().quit)

        inputs = QHBoxLayout()
        inputs.addWidget(QLabel("音源"))
        inputs.addWidget(self.audio_input, 1)
        languages = QHBoxLayout()
        languages.addWidget(self.source, 1)
        languages.addWidget(QLabel("→"))
        languages.addWidget(self.target, 1)
        overlay_row = QHBoxLayout()
        overlay_row.addWidget(self.show_overlay, 1)
        overlay_row.addWidget(self.collapse)
        bottom = QHBoxLayout()
        bottom.addWidget(self.open_folder, 1)
        bottom.addWidget(self.quit_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addLayout(inputs)
        layout.addLayout(languages)
        layout.addWidget(self.toggle)
        layout.addWidget(self.state_label)
        layout.addWidget(self.detail)
        layout.addWidget(self._line())
        layout.addLayout(overlay_row)
        layout.addWidget(self._line())
        layout.addWidget(self.files)
        layout.addLayout(bottom)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.sync)
        self._timer.start(200)
        self.sync()

    @staticmethod
    def _line() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: rgba(128, 128, 128, 80);")
        return line

    # ---- 用户的操作 ----

    def toggle_listening(self) -> None:
        if self.interpreter.state == "listening":
            self.interpreter.pause()
        elif self.interpreter.state == "paused":
            self.interpreter.resume()
        self.sync()

    def refresh_inputs(self) -> None:
        """重新列出音源，并选中现在实际在用的那个。"""
        cfg = self.interpreter.cfg
        combo = self.audio_input
        combo.blockSignals(True)
        combo.clear()
        if cfg.audio.source == "file":
            combo.addItem(f"音频文件：{Path(cfg.audio.file).name}", "file")
        else:
            combo.addItem("电脑播放的声音", "system")
            devices = input_devices()
            if devices:
                default = next((d for d in devices if d.is_default), devices[0])
                combo.addItem(f"麦克风：系统默认（{default.name}）", "mic:")
                for d in devices:
                    combo.addItem(f"麦克风：{d.name}", f"mic:{d.name}")
        current = "system" if cfg.audio.source == "system" else f"{cfg.audio.source}:{cfg.audio.device}"
        index = combo.findData("file" if cfg.audio.source == "file" else current)
        if index < 0 and cfg.audio.source == "mic":  # 配置里写的是名字的一部分：找名字里含它的那一项
            spec = cfg.audio.device.lower()
            index = next(
                (i for i in range(combo.count()) if combo.itemData(i).startswith("mic:") and spec in combo.itemData(i).lower()),
                -1,
            )
            if index < 0:  # 现在列表里没有（比如设备刚被拔掉）：照样显示出来
                combo.addItem(f"麦克风：{cfg.audio.device}", current)
                index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def apply_input(self) -> None:
        """换音源。在后台线程里换：打开麦克风、启动内录可能要一两秒，不能让界面卡住。"""
        key = self.audio_input.currentData()
        if self._switching or key in (None, "file"):
            return
        kind, _, device = key.partition(":")
        self._switching = True
        self.set_message("正在切换音源……")

        def work() -> None:
            try:
                self.interpreter.set_source(kind, device)
                self.switched.emit("")
            except Exception as exc:
                self.switched.emit(f"切换音源失败：{exc}")

        threading.Thread(target=work, name="switch-source", daemon=True).start()

    def _on_switched(self, error: str) -> None:
        self._switching = False
        self.refresh_inputs()  # 失败时选回实际在用的那个
        self.set_message(error)

    def apply_languages(self) -> None:
        self.interpreter.set_languages(self.source.currentData(), self.target.currentText())

    def open_folders(self) -> None:
        for folder in self.interpreter.output_folders():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def set_message(self, text: str) -> None:
        """流水线报告的状态文字（和字幕窗左上角那行小字是同一个来源）。"""
        self.message = text
        self.sync()

    def bring_to_front(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    # ---- 把界面改成和实际状态一致 ----

    def sync(self) -> None:
        state = self.interpreter.state
        text, color = STATES.get(state, (state, "#9a9a9a"))
        self.state_label.setText(f'<span style="color:{color};">●</span> {text}')
        if state == "listening":
            label, button_color = "暂停监听", "#d9822b"
        elif state == "ended":
            label, button_color = "已结束", "#8c8c8c"
        else:
            label, button_color = "开始监听", "#2e9d57"
        self.toggle.setText(label)
        self.toggle.setEnabled(state in ("listening", "paused"))
        self.audio_input.setEnabled(
            not self._switching and state != "ended" and self.interpreter.cfg.audio.source != "file"
        )
        self.toggle.setStyleSheet(BUTTON_STYLE.format(color=button_color))
        hint = "暂停时不识别、不翻译，录音也跟着停。" if state == "paused" else ""
        self.detail.setText(self.message if self.message not in ("", "已暂停") else hint)
        self.detail.setVisible(bool(self.detail.text()))

        self.show_overlay.blockSignals(True)  # 这里只是照着实际情况打勾，不能反过来又去触发一次显示 / 隐藏
        self.show_overlay.setChecked(self.overlay.isVisible())
        self.show_overlay.blockSignals(False)
        self.collapse.setText("展开字幕窗" if self.overlay.collapsed else "收起字幕窗")
        self.collapse.setEnabled(self.overlay.isVisible())

        lines = []
        recorder, transcript = self.interpreter.recorder, self.interpreter.transcript
        if recorder is not None:
            lines.append(f"录音：{recorder.path.name}" + ("" if recorder.active or state == "ended" else "（已停止）"))
        elif state != "ended":
            lines.append("录音：这次没有开")
        if transcript is not None:
            lines.append(f"记录：{transcript.path.name}")
        self.files.setText("\n".join(lines))
        self.open_folder.setEnabled(bool(self.interpreter.output_folders()))

    # 这里故意不重写 closeEvent。点窗口的关闭按钮，Qt 默认就只是把面板藏起来，同传照常进行
    # （ui/app.py 里关掉了“最后一个窗口关闭就退出”）。如果在 closeEvent 里拒绝关闭，程序就退不掉了：
    # Qt 退出之前会先把每个窗口关一遍，有一个不肯关，这次退出就作废（S20 踩过，表现是点“退出”时灵时不灵）
