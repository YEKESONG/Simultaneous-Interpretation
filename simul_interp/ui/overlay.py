"""透明悬浮字幕窗（PySide6）。

设计目标是“稳”：内容怎么更新，都不能带着别的文字跳来跳去。
- 底部是高度固定的“当前句”区域：正在识别的法语在这里实时更新（已确认的白色、暂定的灰色），
  译文的位置先空着，翻好了直接填进这块空白。区域里怎么变，都不会带动别的内容；
- 上方是已经翻好的历史，越旧越暗。下一句开始时，当前这一对才从底部区域平滑地滑上去（约 0.2 秒），
  底部区域清空，开始显示下一句；
- 当前句区域至少能放两行原文和两行译文，只在遇到特别长的句子时才扩大；窗口大小只随手动拖动改变；
- 右上角一个小圆点：听到有人说话时变绿；启动时的状态提示是左上角的一行小字，都不占字幕的位置；
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
STREAM_FALLBACK_S = 1.0  # 译文开始出来后迟迟翻不完：最多等这么久，就先当它翻好了往下走
WAIT_FALLBACK_S = 4.0  # 译文一个字都没出来：最多等这么久
SCROLL_MS = 220  # 一对原文译文从当前句区域滑上去的时间
MAX_KEPT = 30  # 最多记住多少个翻译单元（显示的只是最后几对）
# 按新旧程度的颜色（原文, 译文）：最新的一对最亮，越旧越暗，眼睛自然落在最新的译文上
AGE_COLORS = [("#dcdcdc", "#ffffff"), ("#a0a0a0", "#c8c8c8"), ("#7c7c7c", "#989898")]
LIVE_COLORS = ("#e6e6e6", "#8f8f8f")  # 正在识别的原文：已确认的部分、暂定的部分


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


def source_html(text: str, color: str, fr: int) -> str:
    return f'<p style="margin:0; color:{color}; font-size:{fr}px;">{html.escape(text)}</p>'


def translation_html(unit: UnitSnapshot, color: str, zh: int) -> str:
    """译文的富文本；还没有译文时返回空串（位置留白）。"""
    if unit.error:
        text = f'<span style="color:#ff8a80;">翻译失败：{html.escape(unit.error[:80])}</span>'
    elif not unit.translation:
        return ""
    else:
        text = html.escape(unit.translation)
        if not unit.done:  # 还在翻：末尾加一个灰色省略号
            text += '<span style="color:#8a8a8a;">…</span>'
    return f'<p style="margin:0; color:{color}; font-size:{zh}px;">{text}</p>'


def pair_html(unit: UnitSnapshot, age: int, zh: int, fr: int) -> str:
    """历史里的一对原文和译文。age = 0 是最新的一对，越旧颜色越暗。"""
    fr_color, zh_color = AGE_COLORS[min(age, len(AGE_COLORS) - 1)]
    return source_html(unit.source, fr_color, fr) + translation_html(unit, zh_color, zh)


def live_html(pending: str, partial: str, fr: int) -> str:
    """正在识别的原文：已确认的部分白色，暂定的尾巴灰色。"""
    done_color, guess_color = LIVE_COLORS
    return (
        f'<p style="margin:0; color:{done_color}; font-size:{fr}px;">{html.escape(pending)} '
        f'<span style="color:{guess_color};">{html.escape(partial)}</span></p>'
    )


class CaptionView(QWidget):
    """字幕区，自己排版、自己画，大小不随内容变化，分两部分：

    - 底部的“当前句”区域：高度固定，至少能放两行原文和两行译文。里面的内容怎么变（识别中的原文更新、
      译文填进留白），都不会带动别的内容。区域高度只在遇到放不下的长句时扩大，一对滑上去时才可能恢复；
    - 上方的历史：原文译文对从下往上堆。一对从当前句区域滑上去时，先让它停在原来的位置，
      再平滑地滑到历史里；这 0.2 秒里先不画新的当前句，免得两者重叠。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.gap = 8  # 两对之间的空隙
        self._fonts = (0, 0)
        self._history: list[tuple[object, str, QTextDocument]] = []  # (键, 富文本, 排好版的文档)，旧 → 新
        self._slot_html = ("", "")  # 当前句的 (原文, 译文)
        self._slot_docs = (self._layout(""), self._layout(""))
        self._slot_base = 0.0  # 当前句区域的最小高度：两行原文 + 两行译文
        self.slot_height = 0.0  # 当前句区域的实际高度
        self._shift = 0.0  # 历史整体往下的额外位移，动画结束时回到 0
        self._slot_hidden = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(SCROLL_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_shift)
        self._anim.finished.connect(self._on_anim_done)

    def history_keys(self) -> list:
        return [key for key, _, _ in self._history]

    def history_top(self, key) -> float:
        """某一对历史（动画结束后）的顶边在字幕区里的纵坐标，测试用来检查位置有没有被带动。"""
        return self.height() - self._offsets()[key]

    def set_fonts(self, fr: int, zh: int) -> None:
        if (fr, zh) == self._fonts:
            return
        self._fonts = (fr, zh)
        self._slot_base = self._text_height(fr, 2) + self._text_height(zh, 2)
        self.slot_height = max(self._slot_base, self._slot_needed())
        self.update()

    def set_content(self, history: list[tuple[object, str]], slot: tuple[str, str]) -> None:
        """history：从旧到新的 (键, 一对原文译文的富文本)；slot：当前句的 (原文富文本, 译文富文本)。"""
        history_changed = [(k, h) for k, h, _ in self._history] != history
        if not history_changed and slot == self._slot_html:
            return
        old_keys = self.history_keys()
        old_offsets = self._offsets()
        if history_changed:
            cache = {key: (text, doc) for key, text, doc in self._history}
            rebuilt = []
            for key, text in history:
                cached = cache.get(key)
                doc = cached[1] if cached and cached[0] == text else self._layout(text)
                rebuilt.append((key, text, doc))
            self._history = rebuilt
        if slot != self._slot_html:
            self._slot_html = slot
            self._slot_docs = (self._layout(slot[0]), self._layout(slot[1]))

        new_keys = self.history_keys()
        moved_up = bool(new_keys) and new_keys[-1] not in old_keys  # 有一对从当前句区域滑进了历史
        # 当前句区域：平时只增不减（里面怎么变都不影响上面）；一对滑上去的时候才按新内容重新定高度
        needed = max(self._slot_base, self._slot_needed())
        self.slot_height = needed if moved_up else max(self.slot_height, needed)

        new_offsets = self._offsets()
        anchor = next((key for key in reversed(new_keys) if key in old_offsets), None)
        if anchor is not None:
            start = self._shift + new_offsets[anchor] - old_offsets[anchor]  # 原来就在的内容先停在原位
        elif moved_up:
            start = new_offsets[new_keys[-1]] - self.slot_height  # 第一条历史：从当前句区域的位置出发
        else:
            start = 0.0
        self._anim.stop()
        self._slot_hidden = moved_up and abs(start) > 0.5
        if abs(start) > 0.5:
            self._anim.setStartValue(start)
            self._anim.setEndValue(0.0)
            self._anim.start()
        else:
            self._shift = 0.0
        self.update()

    # ---- 排版 ----

    def _layout(self, text: str) -> QTextDocument:
        doc = QTextDocument(self)
        doc.setDocumentMargin(0)
        doc.setHtml(text)
        doc.setTextWidth(max(1, self.width()))
        return doc

    def _text_height(self, px: int, lines: int) -> float:
        sample = "<br>".join(["国 Ag"] * lines)
        return self._layout(f'<p style="margin:0; font-size:{px}px;">{sample}</p>').size().height()

    def _slot_needed(self) -> float:
        return sum(doc.size().height() for doc in self._slot_docs if not doc.isEmpty())

    def _offsets(self) -> dict:
        """每一对历史的顶边离字幕区底边多远：先是当前句区域，再往上逐对累加。"""
        offsets, total = {}, self.slot_height + self.gap
        for key, _, doc in reversed(self._history):
            total += doc.size().height()
            offsets[key] = total
            total += self.gap
        return offsets

    # ---- 动画和绘制 ----

    def _on_shift(self, value) -> None:
        self._shift = float(value)
        self.update()

    def _on_anim_done(self) -> None:
        self._shift = 0.0
        self._slot_hidden = False
        self.update()

    def resizeEvent(self, event) -> None:
        for _, _, doc in self._history:
            doc.setTextWidth(max(1, self.width()))
        for doc in self._slot_docs:
            doc.setTextWidth(max(1, self.width()))
        self.slot_height = max(self._slot_base, self._slot_needed())
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setClipRect(self.rect())
        slot_top = self.height() - self.slot_height
        y = slot_top - self.gap + self._shift
        for _, _, doc in reversed(self._history):
            height = doc.size().height()
            y -= height
            if y + height < 0:
                break  # 再往上的已经滑出字幕区了
            painter.save()
            painter.translate(0, y)
            doc.drawContents(painter)
            painter.restore()
            y -= self.gap
        if self._slot_hidden:
            return
        source, translation = self._slot_docs
        painter.save()
        painter.setClipRect(0, int(slot_top), self.width(), int(self.slot_height) + 1)
        painter.translate(0, slot_top)
        source.drawContents(painter)
        painter.translate(0, 0 if source.isEmpty() else source.size().height())  # 译文紧接在原文下面
        translation.drawContents(painter)
        painter.restore()


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
        self.live = ("", "")  # 正在识别的 (已确认但还没凑成一句的原文, 暂定的尾巴)
        self._promoted = -1  # 已经滑进历史的最新一个单元编号（滑进去就不再回到当前句区域）
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
        self._timer.timeout.connect(self.refresh)  # “等太久就往下走”要看时间，所以定时检查；内容没变时什么也不做
        self._timer.start(50)
        self._restore_geometry()

    # ---- 数据更新（主线程） ----

    def set_live(self, pending: str, partial: str, translation: str = "") -> None:
        """正在识别的原文：显示在底部的当前句区域；投机译文不显示（它可能还会变）。"""
        self.live = (pending, partial)
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
        if text != self.status:
            self.status = text
            self.update()

    def clear(self) -> None:
        self.units.clear()
        self._seen_at.clear()
        self._first_text_at.clear()
        self._promoted = -1

    # ---- 显示什么 ----

    def _ready(self, unit: UnitSnapshot, moment: float) -> bool:
        """译文翻完了（或者等得太久了），这一对可以往下走。"""
        first = self._first_text_at.get(unit.id)
        return (
            unit.done
            or bool(unit.error)
            or (first is not None and moment - first >= STREAM_FALLBACK_S)
            or moment - self._seen_at.get(unit.id, moment) >= WAIT_FALLBACK_S
        )

    def compose(self, moment: float | None = None) -> tuple[list[UnitSnapshot], tuple]:
        """决定历史里放哪几对、当前句区域放什么。当前句区域的内容有三种：
        - ("unit", 单元)：一句已经确认、正在翻译（译文位置留白）或刚翻好、下一句还没开始；
        - ("live", 已确认的原文, 暂定的尾巴)：正在说的下一句；
        - ("empty",)：什么都没有。
        翻好的单元按顺序进入历史：前一句还没翻好时，后面的先不动，已显示的内容不会被插队打乱。"""
        moment = now() if moment is None else moment
        units = list(self.units.values())
        ready: list[UnitSnapshot] = []
        for unit in units:
            if not self._ready(unit, moment):
                break
            ready.append(unit)
        waiting = units[len(ready)] if len(ready) < len(units) else None
        pending, partial = self.live
        newest = ready[-1] if ready else None
        # 翻好的最后一对留在当前句区域，直到下一句开始（出现暂定文字，或者下一句已经在翻）。
        # 滑进历史是单向的：暂定文字常会短暂变空，如果那时让它退回当前句区域，就会上下来回跳
        if newest is not None and (waiting is not None or pending or partial):
            self._promoted = max(self._promoted, newest.id)
        stay = newest is not None and newest.id > self._promoted
        history = ready[:-1] if stay else ready
        if waiting is not None:
            slot = ("unit", waiting)
        elif stay:
            slot = ("unit", newest)
        elif pending or partial:
            slot = ("live", pending, partial)
        else:
            slot = ("empty",)
        return history[-self.max_lines :], slot

    def refresh(self) -> None:
        zh = self.font_size
        fr = max(11, round(zh * self.source_ratio))
        history, slot = self.compose()
        # 当前句区域最亮，历史从次一级开始、越旧越暗。明暗只看新旧，不随当前句区域的状态变化，
        # 否则当前句区域在“译文”和“暂定文字”之间切换时，整片历史会跟着变一次颜色
        history_blocks = [
            (unit.id, pair_html(unit, len(history) - index, zh, fr)) for index, unit in enumerate(history)
        ]
        if slot[0] == "unit":
            fr_color, zh_color = AGE_COLORS[0]
            slot_html = (source_html(slot[1].source, fr_color, fr), translation_html(slot[1], zh_color, zh))
        elif slot[0] == "live":
            slot_html = (live_html(slot[1], slot[2], fr), "")  # 译文的位置先留白
        else:
            slot_html = ("", "")
        self.captions.gap = max(6, round(zh * 0.4))
        self.captions.set_fonts(fr, zh)
        self.captions.set_content(history_blocks, slot_html)

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
        if self.status:  # 状态提示：左上角一行小字，不占字幕的位置
            font = QFont()
            font.setPixelSize(12)
            painter.setFont(font)
            painter.setPen(QColor("#ffd479"))
            painter.drawText(QRect(20, 3, self.width() - 60, 16), Qt.AlignmentFlag.AlignLeft, self.status)

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
        height = round(self.font_size * 10)
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
