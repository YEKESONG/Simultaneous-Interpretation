"""透明悬浮字幕窗（PySide6）。

设计目标是“稳”：眼睛盯着一个地方就能读到最新的译文，窗口不闪、不跳、不变大小。
- 只显示“原文 + 译文”成对的内容：一对的译文翻好了，原文和译文才一起出现。投机翻译命中时，
  原文确认的那一刻译文通常已经翻好了，所以几乎不增加延迟。正在识别的暂定文字不显示，免得一小段一小段地闪；
- 新的一对出现在最下面，旧的平滑地往上滑走（约 0.2 秒），不会一下子跳上去；最新的一对最亮，越旧越暗；
- 窗口大小固定，只随手动拖动改变，不会被文字撑大或缩小；
- 右上角一个小圆点：听到有人说话时变绿，表示它在工作，又不占字幕的位置；
- 始终置顶、不抢焦点；macOS 上还能浮在全屏视频、全屏会议的上面；
- 拖动移动，右下角调整大小；右键菜单调字号、背景深浅、鼠标穿透；菜单栏图标里可以解锁穿透和退出；
- 识别和翻译在后台线程跑，结果通过 Qt 信号交给界面线程。
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

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QSettings,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap, QTextDocument
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QMenu, QSizeGrip, QSystemTrayIcon, QWidget

from ..clock import now
from ..config import UiConfig
from ..translate import TranslationUnit

logger = logging.getLogger(__name__)

STYLE_VERSION = 2  # 字幕样式改版时加 1：旧版本保存的字号不再沿用
STREAM_FALLBACK_S = 1.0  # 译文开始出来后迟迟翻不完：最多等这么久，先显示已经翻出的部分
WAIT_FALLBACK_S = 4.0  # 译文一个字都没出来：最多等这么久，先只显示原文
SCROLL_MS = 220  # 新内容出现时，旧内容往上滑的时间
MAX_KEPT = 30  # 最多记住多少个翻译单元（显示的只是最后几对）
# 按新旧程度的颜色（原文, 译文）：最新的一对最亮，越旧越暗，眼睛自然落在最新的译文上
AGE_COLORS = [("#dcdcdc", "#ffffff"), ("#a0a0a0", "#c8c8c8"), ("#7c7c7c", "#989898")]


class UnitSnapshot(NamedTuple):
    """跨线程传给界面的是不可变的快照，避免界面读到翻译线程写了一半的数据。"""

    id: int
    source: str
    translation: str
    done: bool
    error: str


class Bridge(QObject):
    live = Signal(str, str, str)
    unit = Signal(object)
    status = Signal(str)
    finished = Signal()


class QtView:
    """实现流水线的 View 接口：在后台线程里被调用，只负责发信号，真正的界面更新在主线程里做。"""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge

    def on_live(self, pending: str, partial: str, translation: str = "") -> None:
        self.bridge.live.emit(pending, partial, translation)

    def on_unit(self, unit: TranslationUnit) -> None:
        self.bridge.unit.emit(UnitSnapshot(unit.id, unit.source, unit.translation, unit.done, unit.error))

    def on_status(self, text: str) -> None:
        self.bridge.status.emit(text)


def pair_html(unit: UnitSnapshot, age: int, zh: int, fr: int) -> str:
    """一对原文和译文的富文本。age = 0 是最新的一对，越旧颜色越暗。"""
    fr_color, zh_color = AGE_COLORS[min(age, len(AGE_COLORS) - 1)]
    esc = html.escape
    source = f'<p style="margin:0; color:{fr_color}; font-size:{fr}px;">{esc(unit.source)}</p>'
    if unit.error:
        translation = f'<span style="color:#ff8a80;">翻译失败：{esc(unit.error[:80])}</span>'
    elif unit.done and not unit.translation:
        return source  # 不翻译（没有密钥）时只有原文
    else:
        translation = esc(unit.translation)
        if not unit.done:  # 还没翻完（等太久才会走到这里）：末尾加一个灰色省略号
            translation += '<span style="color:#8a8a8a;">…</span>'
    return source + f'<p style="margin:0; color:{zh_color}; font-size:{zh}px;">{translation}</p>'


class CaptionView(QWidget):
    """字幕区：每一对原文译文排成一块，从下往上堆。内容变化时先让画面停在原位，再平滑地滑到新位置。"""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.gap = 8  # 两对之间的空隙
        self._blocks: list[tuple[object, str, QTextDocument]] = []  # (键, 富文本, 排好版的文档)，旧 → 新
        self._shift = 0.0  # 整体往下的额外位移，动画结束时回到 0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(SCROLL_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_shift)

    def keys(self) -> list:
        return [key for key, _, _ in self._blocks]

    def set_blocks(self, blocks: list[tuple[object, str]]) -> None:
        """blocks：从旧到新的 (键, 富文本)。键相同的块是同一段内容（颜色、译文可能变了）。"""
        if [(k, h) for k, h, _ in self._blocks] == blocks:
            return
        old_offsets = self._offsets()
        cache = {key: (text, doc) for key, text, doc in self._blocks}
        rebuilt = []
        for key, text in blocks:
            cached = cache.get(key)
            doc = cached[1] if cached and cached[0] == text else self._layout(text)
            rebuilt.append((key, text, doc))
        self._blocks = rebuilt
        new_offsets = self._offsets()
        # 以“原来就有、现在还在”的最新一块为锚：它先停在原来的位置，再平滑地滑到新位置。
        # 旧内容从上面移走不会带动别的块，因为所有块都是从底部往上排的
        anchor = next((key for key, _, _ in reversed(rebuilt) if key in old_offsets), None)
        start = self._shift + new_offsets[anchor] - old_offsets[anchor] if anchor is not None else 0.0
        self._anim.stop()
        if abs(start) > 0.5:
            self._anim.setStartValue(start)
            self._anim.setEndValue(0.0)
            self._anim.start()
        else:
            self._shift = 0.0
            self.update()

    def _layout(self, text: str) -> QTextDocument:
        doc = QTextDocument(self)
        doc.setDocumentMargin(0)
        doc.setHtml(text)
        doc.setTextWidth(max(1, self.width()))
        return doc

    def _offsets(self) -> dict:
        """每一块的顶边离字幕区底边多远（从最新的一块往上累加）。"""
        offsets, total = {}, 0.0
        for key, _, doc in reversed(self._blocks):
            total += doc.size().height()
            offsets[key] = total
            total += self.gap
        return offsets

    def _on_shift(self, value) -> None:
        self._shift = float(value)
        self.update()

    def resizeEvent(self, event) -> None:
        for _, _, doc in self._blocks:
            doc.setTextWidth(max(1, self.width()))
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setClipRect(self.rect())
        y = self.height() + self._shift
        for _, _, doc in reversed(self._blocks):
            height = doc.size().height()
            y -= height
            if y + height < 0:
                break  # 再往上的已经滑出字幕区了
            painter.save()
            painter.translate(0, y)
            doc.drawContents(painter)
            painter.restore()
            y -= self.gap


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
        if int(self.settings.value("style_version", 1)) < STYLE_VERSION:
            self.settings.remove("font_size")  # 样式改过：旧的字号不再沿用，位置和大小保留
            self.settings.setValue("style_version", STYLE_VERSION)
        self.font_size = int(self.settings.value("font_size", cfg.font_size))
        self.source_ratio = cfg.source_font_ratio
        self.opacity = float(self.settings.value("opacity", cfg.opacity))
        self.max_lines = cfg.max_lines
        self.click_through = False
        self.on_reconnect = None  # 由 run_overlay 设置：重新连接音频设备

        self.units: OrderedDict[int, UnitSnapshot] = OrderedDict()
        self._seen_at: dict[int, float] = {}  # 每个单元第一次出现的时刻
        self._first_text_at: dict[int, float] = {}  # 每个单元译文开始出现的时刻
        self.hearing = False
        self.status = "正在启动……"

        self.captions = CaptionView(self)
        shadow = QGraphicsDropShadowEffect(self.captions)  # 文字阴影：背景调得很透明时，压在亮色画面上也看得清
        shadow.setBlurRadius(8)
        shadow.setOffset(0, 1)
        shadow.setColor(QColor(0, 0, 0, 230))
        self.captions.setGraphicsEffect(shadow)
        self.grip = QSizeGrip(self)
        self.grip.resize(16, 16)

        self._drag_from: QPoint | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)  # “等太久先显示”要看时间，所以定时检查；内容没变时什么也不做
        self._timer.start(50)
        self._restore_geometry()

    # ---- 数据更新（主线程） ----

    def set_live(self, pending: str, partial: str, translation: str = "") -> None:
        """正在识别的文字不显示（会一小段一小段地闪），只用来点亮右上角的小圆点。"""
        hearing = bool(pending or partial)
        if hearing != self.hearing:
            self.hearing = hearing
            self.update()

    def set_unit(self, unit: UnitSnapshot) -> None:
        moment = now()
        self._seen_at.setdefault(unit.id, moment)
        if unit.translation:
            self._first_text_at.setdefault(unit.id, moment)
        self.units[unit.id] = unit
        while len(self.units) > MAX_KEPT:
            old_id, _ = self.units.popitem(last=False)
            self._seen_at.pop(old_id, None)
            self._first_text_at.pop(old_id, None)

    def set_status(self, text: str) -> None:
        self.status = text

    def clear(self) -> None:
        self.units.clear()
        self._seen_at.clear()
        self._first_text_at.clear()

    # ---- 显示什么 ----

    def visible_units(self, moment: float | None = None) -> list[UnitSnapshot]:
        """按顺序显示“准备好了”的单元：译文翻完了，或者等得太久了。
        前一个还没准备好时，后面的也先不显示，这样新内容总是出现在最下面，已显示的位置不会被插队打乱。"""
        moment = now() if moment is None else moment
        shown = []
        for unit in self.units.values():
            first = self._first_text_at.get(unit.id)
            ready = (
                unit.done
                or bool(unit.error)
                or (first is not None and moment - first >= STREAM_FALLBACK_S)
                or moment - self._seen_at.get(unit.id, moment) >= WAIT_FALLBACK_S
            )
            if not ready:
                break
            shown.append(unit)
        return shown[-self.max_lines :]

    def blocks(self, moment: float | None = None) -> list[tuple[object, str]]:
        zh = self.font_size
        fr = max(11, round(zh * self.source_ratio))
        shown = self.visible_units(moment)
        blocks: list[tuple[object, str]] = [
            (unit.id, pair_html(unit, len(shown) - 1 - index, zh, fr)) for index, unit in enumerate(shown)
        ]
        if self.status:
            status = html.escape(self.status)
            blocks.append(("status", f'<p style="margin:0; color:#ffd479; font-size:{fr}px;">{status}</p>'))
        return blocks

    def refresh(self) -> None:
        self.captions.gap = max(6, round(self.font_size * 0.4))
        self.captions.set_blocks(self.blocks())

    # ---- 绘制 ----

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(12, 12, 14, round(255 * self.opacity)))
        painter.drawRoundedRect(self.rect(), 14, 14)
        # 右上角的小圆点：听到有人说话时变绿
        painter.setBrush(QColor(92, 214, 128) if self.hearing else QColor(130, 130, 130, 150))
        painter.drawEllipse(QPointF(self.width() - 14, 12), 3.5, 3.5)

    def resizeEvent(self, event) -> None:
        # 手动摆放字幕区，不用布局管理器：布局会根据内容改窗口的最小尺寸，文字一多窗口就被撑大
        self.captions.setGeometry(20, 20, max(1, self.width() - 40), max(1, self.height() - 34))
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
        height = round(self.font_size * 9)
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

    # 开发调试：定时把字幕窗截图存下来（默认每 10 秒；SI_OVERLAY_SNAPSHOT_MS 可以改间隔，用来检查闪烁和跳动）
    snapshot_dir = os.environ.get("SI_OVERLAY_SNAPSHOTS")
    if snapshot_dir:
        Path(snapshot_dir).mkdir(parents=True, exist_ok=True)
        snapshots = QTimer(overlay)
        snapshots.timeout.connect(
            lambda: overlay.grab().save(str(Path(snapshot_dir) / f"overlay_{time.time():.2f}.png"))
        )
        snapshots.start(int(os.environ.get("SI_OVERLAY_SNAPSHOT_MS", "10000")))

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
