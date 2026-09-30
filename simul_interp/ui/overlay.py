"""透明悬浮字幕窗（PySide6）。

- 半透明圆角背景：上面一行法语原文（小字、浅灰），下面一行中文译文（大字、白色），最底下是正在说的话；
- 始终置顶、不抢焦点；macOS 上还能浮在全屏视频、全屏会议的上面；
- 拖动移动，右下角调整大小；右键菜单调字号、背景深浅、鼠标穿透；菜单栏图标里可以解锁穿透和退出；
- 识别和翻译在后台线程跑，结果通过 Qt 信号交给界面线程，每 50 毫秒最多刷新一次。
"""

from __future__ import annotations

import html
import logging
import os
import signal
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import QObject, QPoint, QRect, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QLabel,
    QMenu,
    QSizeGrip,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..config import UiConfig
from ..translate import TranslationUnit

logger = logging.getLogger(__name__)


class UnitSnapshot(NamedTuple):
    """跨线程传给界面的是不可变的快照，避免界面读到翻译线程写了一半的数据。"""

    id: int
    source: str
    translation: str
    done: bool
    error: str


class Bridge(QObject):
    live = Signal(str, str)
    unit = Signal(object)
    status = Signal(str)
    finished = Signal()


class QtView:
    """实现流水线的 View 接口：在后台线程里被调用，只负责发信号，真正的界面更新在主线程里做。"""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge

    def on_live(self, pending: str, partial: str) -> None:
        self.bridge.live.emit(pending, partial)

    def on_unit(self, unit: TranslationUnit) -> None:
        self.bridge.unit.emit(UnitSnapshot(unit.id, unit.source, unit.translation, unit.done, unit.error))

    def on_status(self, text: str) -> None:
        self.bridge.status.emit(text)


class Overlay(QWidget):
    def __init__(self, cfg: UiConfig, settings: QSettings | None = None) -> None:
        """settings：保存位置、字号等的地方；默认是系统的用户设置，测试时可以传一个临时文件。"""
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if sys.platform == "win32":
            flags |= Qt.WindowType.Tool  # Windows 上不在任务栏占位置；macOS 上 Tool 窗口会在切换应用时自动隐藏，不能用
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("同传字幕")

        self.settings = settings or QSettings("SimulInterp", "Overlay")
        self.font_size = int(self.settings.value("font_size", cfg.font_size))
        self.opacity = float(self.settings.value("opacity", cfg.opacity))
        self.max_lines = cfg.max_lines
        self.click_through = False
        self.on_reconnect = None  # 由 run_overlay 设置：重新连接音频设备

        self.units: OrderedDict[int, UnitSnapshot] = OrderedDict()
        self.pending = self.partial = ""
        self.status = "正在启动……"

        self.label = QLabel(self)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        shadow = QGraphicsDropShadowEffect(self.label)  # 文字阴影：背景调得很透明时，压在亮色画面上也看得清
        shadow.setBlurRadius(8)
        shadow.setOffset(0, 1)
        shadow.setColor(QColor(0, 0, 0, 230))
        self.label.setGraphicsEffect(shadow)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 14)
        layout.addWidget(self.label)
        self.grip = QSizeGrip(self)
        self.grip.resize(16, 16)

        self._drag_from: QPoint | None = None
        self._dirty = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._render_if_dirty)
        self._timer.start(50)
        self._restore_geometry()

    # ---- 数据更新（主线程） ----

    def set_live(self, pending: str, partial: str) -> None:
        self.pending, self.partial = pending, partial
        self._dirty = True

    def set_unit(self, unit: UnitSnapshot) -> None:
        self.units[unit.id] = unit
        while len(self.units) > self.max_lines:
            self.units.popitem(last=False)
        self._dirty = True

    def set_status(self, text: str) -> None:
        self.status = text
        self._dirty = True

    def clear(self) -> None:
        self.units.clear()
        self.pending = self.partial = ""
        self._dirty = True

    # ---- 绘制 ----

    def _render_if_dirty(self) -> None:
        if self._dirty:
            self._dirty = False
            self.label.setText(self.render_html())

    def render_html(self) -> str:
        zh = self.font_size
        fr = max(11, round(zh * 0.62))
        esc = html.escape
        parts = []
        for unit in self.units.values():
            parts.append(f'<p style="margin:0; color:#d0d0d0; font-size:{fr}px;">{esc(unit.source)}</p>')
            if unit.error:
                text = f'<span style="color:#ff8a80;">翻译失败：{esc(unit.error[:80])}</span>'
            else:
                text = esc(unit.translation)
                if not unit.done:  # 还在流式输出：末尾加一个灰色省略号提示
                    text += '<span style="color:#9a9a9a;">…</span>'
            parts.append(f'<p style="margin:0 0 6px 0; color:#ffffff; font-size:{zh}px;">{text}</p>')
        if self.pending or self.partial:
            parts.append(
                f'<p style="margin:0; font-size:{fr}px; color:#e8e8e8;">▸ {esc(self.pending)} '
                f'<span style="color:#9a9a9a;">{esc(self.partial)}</span></p>'
            )
        if self.status:
            parts.append(f'<p style="margin:0; font-size:{fr}px; color:#ffd479;">{esc(self.status)}</p>')
        if not parts:
            parts.append(f'<p style="margin:0; font-size:{fr}px; color:#9a9a9a;">正在收听……</p>')
        return "".join(parts)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(12, 12, 14, round(255 * self.opacity)))
        painter.drawRoundedRect(self.rect(), 14, 14)

    def resizeEvent(self, event) -> None:
        self.grip.move(self.width() - self.grip.width() - 4, self.height() - self.grip.height() - 4)
        super().resizeEvent(event)

    # ---- 鼠标：拖动、右键菜单 ----

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None:
            self.move(event.globalPosition().toPoint() - self._drag_from)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_from = None

    def contextMenuEvent(self, event) -> None:
        self.build_menu(QMenu(self)).exec(event.globalPos())

    def build_menu(self, menu: QMenu) -> QMenu:
        menu.addAction("字号放大", lambda: self.change_font(+2))
        menu.addAction("字号缩小", lambda: self.change_font(-2))
        menu.addAction("背景加深", lambda: self.change_opacity(+0.1))
        menu.addAction("背景变浅", lambda: self.change_opacity(-0.1))
        menu.addSeparator()
        through = QAction("鼠标穿透（点击直接落到下面的窗口）", menu)
        through.setCheckable(True)
        through.setChecked(self.click_through)
        through.toggled.connect(self.set_click_through)
        menu.addAction(through)
        menu.addAction("清空字幕", self.clear)
        if self.on_reconnect is not None:
            menu.addAction("重新连接音频设备（切换耳机后）", self.on_reconnect)
        menu.addSeparator()
        menu.addAction("退出", QApplication.instance().quit)
        return menu

    def change_font(self, delta: int) -> None:
        self.font_size = max(14, min(64, self.font_size + delta))
        self._dirty = True

    def change_opacity(self, delta: float) -> None:
        self.opacity = round(max(0.0, min(0.95, self.opacity + delta)), 2)
        self.update()

    def set_click_through(self, enabled: bool) -> None:
        self.click_through = enabled
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, enabled)
        self.show()  # 改窗口标志后要重新 show，原生窗口会被重建
        float_over_fullscreen(self)

    # ---- 位置和设置的保存 ----

    def _restore_geometry(self) -> None:
        geometry = self.settings.value("geometry")
        if isinstance(geometry, QRect) and QGuiApplication.screenAt(geometry.center()) is not None:
            self.setGeometry(geometry)
            return
        screen = QGuiApplication.primaryScreen().availableGeometry()
        width = min(1100, int(screen.width() * 0.7))
        height = round(self.font_size * 8.5)
        self.setGeometry(screen.center().x() - width // 2, screen.bottom() - height - 60, width, height)

    def save_settings(self) -> None:
        self.settings.setValue("geometry", self.geometry())
        self.settings.setValue("font_size", self.font_size)
        self.settings.setValue("opacity", self.opacity)


def float_over_fullscreen(widget: QWidget) -> None:
    """macOS：让窗口出现在所有桌面空间、并能浮在全屏应用上面。

    Qt 没有提供这些设置，这里用 ctypes 直接给 NSWindow 发 Objective-C 消息，不需要额外安装 PyObjC。
    只有 Qt 用 macOS 原生窗口（cocoa 平台）时 winId 才是 NSView*；offscreen 等平台下它不是，
    对它发消息会直接崩溃（单元测试里踩过），所以先检查平台。"""
    if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
        return
    import ctypes
    import ctypes.util

    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value

    def send(receiver, selector: str, *args, restype=ctypes.c_void_p, argtypes=()):
        # arm64 上 objc_msgSend 必须按真实参数类型调用，所以每种签名单独生成一个函数指针
        function = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(address)
        return function(receiver, objc.sel_registerName(selector.encode()), *args)

    view = ctypes.c_void_p(int(widget.winId()))  # Qt 的 winId 在 macOS 上是 NSView*
    window = send(view, "window")
    if not window:
        return
    can_join_all_spaces, stationary, full_screen_auxiliary = 1 << 0, 1 << 4, 1 << 8
    behavior = can_join_all_spaces | stationary | full_screen_auxiliary
    send(window, "setCollectionBehavior:", behavior, restype=None, argtypes=(ctypes.c_ulong,))
    send(window, "setLevel:", 25, restype=None, argtypes=(ctypes.c_long,))  # NSStatusWindowLevel，比普通置顶更高
    send(window, "setHidesOnDeactivate:", False, restype=None, argtypes=(ctypes.c_bool,))


def make_accessory_app() -> None:
    """macOS：不在程序坞显示图标、不抢前台，像一个工具浮层（这样才能浮在别的应用的全屏空间上）。"""
    if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
        return
    import ctypes
    import ctypes.util

    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    get = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(address)
    set_policy = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(address)
    app = get(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
    set_policy(app, objc.sel_registerName(b"setActivationPolicy:"), 1)  # NSApplicationActivationPolicyAccessory


def tray_icon_pixmap() -> QPixmap:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(30, 30, 30))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 4, 56, 56, 14, 14)
    painter.setPen(QColor(255, 255, 255))
    font = QFont()
    font.setPixelSize(40)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "译")
    painter.end()
    return pixmap


def run_overlay(cfg, interpreter_factory) -> int:
    """主线程跑 Qt 界面，后台线程跑同传流水线。interpreter_factory(view) 返回 Interpreter。"""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    make_accessory_app()

    bridge = Bridge()
    overlay = Overlay(cfg.ui)
    bridge.live.connect(overlay.set_live)
    bridge.unit.connect(overlay.set_unit)
    bridge.status.connect(overlay.set_status)
    interpreter = interpreter_factory(QtView(bridge))
    overlay.on_reconnect = interpreter.reconnect_audio

    def work() -> None:
        try:
            interpreter.run()
        except Exception as exc:  # 在字幕窗里显示错误，而不是悄悄退出
            logger.exception("同传出错")
            bridge.status.emit(f"出错：{exc}")
        finally:
            bridge.finished.emit()

    if cfg.audio.source == "file":  # 用文件模拟时，放完 5 秒后自动退出；内录要用户自己退出
        bridge.finished.connect(lambda: QTimer.singleShot(5000, app.quit))
    worker = threading.Thread(target=work, name="interpreter", daemon=True)
    worker.start()

    snapshot_dir = os.environ.get("SI_OVERLAY_SNAPSHOTS")  # 开发调试：每 10 秒把字幕窗截图存下来
    if snapshot_dir:
        Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
        snapshots = QTimer(overlay)
        snapshots.timeout.connect(
            lambda: overlay.grab().save(str(Path(snapshot_dir) / f"overlay_{time.strftime('%H%M%S')}.png"))
        )
        snapshots.start(10_000)

    tray = QSystemTrayIcon(QIcon(tray_icon_pixmap()), app)
    tray.setToolTip("法中同传")
    tray_menu = QMenu()
    overlay.build_menu(tray_menu)
    tray_menu.aboutToShow.connect(lambda: (tray_menu.clear(), overlay.build_menu(tray_menu)))
    tray.setContextMenu(tray_menu)
    tray.show()

    overlay.show()
    float_over_fullscreen(overlay)

    # Qt 的事件循环在 C++ 里，Python 收不到 Ctrl+C；定时回到 Python 一下，让信号处理有机会执行
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(200)

    app.exec()
    overlay.save_settings()
    tray.hide()
    interpreter.stop()
    worker.join(timeout=15)
    return 0
