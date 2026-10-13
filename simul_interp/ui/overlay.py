"""透明悬浮字幕窗（PySide6）。

设计目标是“稳”：内容怎么更新，都不能带着别的文字跳来跳去。
- 底部是高度固定的“当前句”区域：正在识别的原文在这里实时更新（已确认的白色、暂定的灰色），
  译文的位置先空着，翻好了直接填进这块空白。区域里怎么变，都不会带动别的内容；
- 上方是已经翻好的历史，越旧越暗。下一句开始时，当前这一对才从底部区域平滑地滑上去（约 0.2 秒），
  底部区域清空，开始显示下一句；
- 历史可以往回翻：在字幕窗上滚动滚轮（触控板双指滑动），或者拖右边的滑动条。回看的时候，新的句子照常
  加在下面，但眼前这几对不动，而且全部用亮色显示；滚回最底下，或者点顶部的提示，就回到最新；
- 当前句区域至少能放两行原文和两行译文，只在遇到特别长的句子时才扩大；窗口大小只随手动拖动改变；
- 不想看的时候可以收起：点右上角的“–”，窗口缩成左下角的一个小条，点小条再展开。收起时照常识别、翻译、记录；
- 右上角一个小圆点：听到有人说话时变绿；启动时的状态提示是左上角的一行小字，都不占字幕的位置；
- 始终置顶、不抢焦点；macOS 上还能浮在全屏视频、全屏会议的上面；
- 拖动移动，右下角调整大小；右键菜单调字号、背景深浅、鼠标穿透；菜单栏图标里可以解锁穿透和退出；
- 识别和翻译在后台线程跑，结果通过 Qt 信号交给界面线程（程序的启动和各部分的接线在 ui/app.py）。
"""

from __future__ import annotations

import html
import logging
import sys
from collections import OrderedDict
from dataclasses import dataclass
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
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QPainter, QPixmap, QTextDocument
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QMenu, QSizeGrip, QWidget

from ..clock import now
from ..config import UiConfig
from ..translate import TranslationUnit

logger = logging.getLogger(__name__)

STYLE_VERSION = 2  # 字幕样式改版时加 1：旧版本保存的字号不再沿用
STREAM_FALLBACK_S = 1.0  # 译文开始出来后迟迟翻不完：最多等这么久，就先当它翻好了往下走
WAIT_FALLBACK_S = 4.0  # 译文一个字都没出来：最多等这么久
SCROLL_MS = 220  # 一对原文译文从当前句区域滑上去的时间
TEXT_COLORS = ("#dcdcdc", "#ffffff")  # 原文、译文
DRAFT_COLOR = "#c2c2c2"  # 译文草稿（话还没说完，还会变）：比正式译文暗一点
# 历史里的一对画得多亮（不透明度）：最新的一对、更旧的。当前句最亮，眼睛自然落在最新的译文上。
# 明暗在画的时候决定，不写进文字的颜色里：回看时全部按最亮画，不用把几百对重新排版
HISTORY_OPACITY = (0.76, 0.58)
LIVE_COLORS = ("#e6e6e6", "#8f8f8f")  # 正在识别的原文：已确认的部分、暂定的部分
RELAYOUT_NOW = 60  # 历史不超过这么多对时，窗口一变宽窄就全部重新排版；更多时拖动期间只排看得见的
PILL_SIZE = (176, 30)  # 收起之后那个小条的大小
CLICK_SLOP = 4  # 按下到松开，鼠标移动不超过这么多像素才算“点击”，否则是拖动


class UnitSnapshot(NamedTuple):
    """跨线程传给界面的是不可变的快照，避免界面读到翻译线程写了一半的数据。"""

    id: int
    source: str
    translation: str  # 屏幕上该显示的译文：正式译文，或者它出来之前先顶着的草稿
    done: bool
    error: str
    tentative: bool = False  # 显示的还是草稿


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
        self.bridge.unit.emit(
            UnitSnapshot(unit.id, unit.source, unit.shown, unit.done, unit.error, unit.tentative)
        )

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
        if unit.tentative:
            color = DRAFT_COLOR
        if not unit.done:  # 还在翻：末尾加一个灰色省略号
            text += '<span style="color:#8a8a8a;">…</span>'
    return f'<p style="margin:0; color:{color}; font-size:{zh}px;">{text}</p>'


def draft_html(text: str, zh: int) -> str:
    """正在说的这句话到目前为止的译文（草稿）；没有时返回空串（位置留白）。"""
    if not text:
        return ""
    return f'<p style="margin:0; color:{DRAFT_COLOR}; font-size:{zh}px;">{html.escape(text)}</p>'


def pair_html(unit: UnitSnapshot, zh: int, fr: int) -> str:
    """一对原文和译文。历史和当前句用同一份文字，历史的明暗在画的时候才决定。"""
    return source_html(unit.source, TEXT_COLORS[0], fr) + translation_html(unit, TEXT_COLORS[1], zh)


def live_html(pending: str, partial: str, fr: int) -> str:
    """正在识别的原文：已确认的部分白色，暂定的尾巴灰色。"""
    done_color, guess_color = LIVE_COLORS
    return (
        f'<p style="margin:0; color:{done_color}; font-size:{fr}px;">{html.escape(pending)} '
        f'<span style="color:{guess_color};">{html.escape(partial)}</span></p>'
    )


@dataclass
class _Pair:
    """历史里的一对：排好版的文档和它的高度。"""

    key: object
    html: str
    doc: QTextDocument
    height: float
    width: int  # 排版时用的宽度；和字幕区现在的宽度不一样，就要重新排


class CaptionView(QWidget):
    """字幕区，自己排版、自己画，大小不随内容变化，分两部分：

    - 底部的“当前句”区域：高度固定，至少能放两行原文和两行译文。里面的内容怎么变（识别中的原文更新、
      译文填进留白），都不会带动别的内容。区域高度只在遇到放不下的长句时扩大，一对滑上去时才可能恢复；
    - 上方的历史：原文译文对从下往上堆。一对从当前句区域滑上去时，先让它停在原来的位置，
      再平滑地滑到历史里；这 0.2 秒里先不画新的当前句，免得两者重叠。

    历史可以往回翻（scroll > 0）。回看时不做滑动动画：新的一对加在下面，同时把 scroll 加上它的高度，
    这样眼前的内容留在原位。
    """

    scrolled = Signal()  # 往回翻的位置或者历史的总高度变了：外面的滑动条要重画

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.gap = 8  # 两对之间的空隙
        self._fonts = (0, 0)
        self._items: list[_Pair] = []  # 历史，旧 → 新
        self._total = 0.0  # 历史的总高度：每一对的高度，加上每一对下面的空隙
        self._slot_html = ("", "")  # 当前句的 (原文, 译文)
        self._slot_docs = (self._layout(""), self._layout(""))
        self._slot_base = 0.0  # 当前句区域的最小高度：两行原文 + 两行译文
        self.slot_height = 0.0  # 当前句区域的实际高度
        self.scroll = 0.0  # 往回翻了多少像素；0 = 停在最新
        self._shift = 0.0  # 历史整体往下的额外位移，动画结束时回到 0
        self._slot_hidden = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(SCROLL_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_shift)
        self._anim.finished.connect(self._on_anim_done)
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(150)
        self._relayout_timer.timeout.connect(self.relayout_all)

    # ---- 给外面用的 ----

    @property
    def reviewing(self) -> bool:
        """是不是正在回看（没有停在最新）。"""
        return self.scroll > 0.5

    def history_keys(self) -> list:
        return [item.key for item in self._items]

    def history_top(self, key) -> float:
        """某一对历史（动画结束后）的顶边在字幕区里的纵坐标，测试用来检查位置有没有被带动。"""
        index = next(i for i, item in enumerate(self._items) if item.key == key)
        return self.height() - self._offset(index) + self.scroll

    def pair_opacity(self, age: int) -> float:
        """历史里的一对画得多亮。age = 0 是最新的一对，越旧越暗；回看的时候全部按最亮画。"""
        return 1.0 if self.reviewing else HISTORY_OPACITY[min(age, len(HISTORY_OPACITY) - 1)]

    def view_height(self) -> float:
        """历史区域的高度：字幕区减去底部的当前句区域。"""
        return max(0.0, self.height() - self.slot_height)

    def max_scroll(self) -> float:
        """最多能往回翻多少：翻到头时，最旧的一对正好顶着字幕区的上边。"""
        return max(0.0, self._total - self.view_height())

    def scroll_by(self, delta: float) -> None:
        """往回翻 delta 像素（负数是往新的方向翻）。"""
        self.scroll_to(self.scroll + delta)

    def scroll_to_latest(self) -> None:
        self.scroll_to(0.0)

    def scroll_to(self, value: float) -> None:
        value = min(max(0.0, value), self.max_scroll())
        if abs(value - self.scroll) < 0.01:
            return
        self._anim.stop()  # 手动翻的时候不做滑动动画
        self._shift, self._slot_hidden = 0.0, False
        self.scroll = value
        self.update()
        self.scrolled.emit()

    def set_fonts(self, fr: int, zh: int) -> None:
        if (fr, zh) == self._fonts:
            return
        self._fonts = (fr, zh)
        self._slot_base = self._text_height(fr, 2) + self._text_height(zh, 2)
        self.slot_height = max(self._slot_base, self._slot_needed())
        self.update()

    def set_content(self, history: list[tuple[object, str]], slot: tuple[str, str]) -> None:
        """history：从旧到新的 (键, 一对原文译文的富文本)；slot：当前句的 (原文富文本, 译文富文本)。"""
        same_history = len(history) == len(self._items) and all(
            item.key == key and item.html == text for item, (key, text) in zip(self._items, history)
        )
        if same_history and slot == self._slot_html:
            return
        # 先记下最后几对现在的位置：内容变了以后，原来就在的那一对要留在原位
        count = len(self._items)
        before = {self._items[i].key: self._offset(i) for i in range(max(0, count - 3), count)}
        old_keys = {item.key for item in self._items} if not same_history else None
        if not same_history:
            self._rebuild(history)
        if slot != self._slot_html:
            self._slot_html = slot
            self._slot_docs = (self._layout(slot[0]), self._layout(slot[1]))

        moved_up = old_keys is not None and bool(self._items) and self._items[-1].key not in old_keys
        # 当前句区域：平时只增不减（里面怎么变都不影响上面）；一对滑上去的时候才按新内容重新定高度
        needed = max(self._slot_base, self._slot_needed())
        self.slot_height = needed if moved_up else max(self.slot_height, needed)

        count = len(self._items)
        anchor = next((i for i in range(count - 1, max(-1, count - 4), -1) if self._items[i].key in before), None)
        if anchor is not None:
            delta = self._offset(anchor) - before[self._items[anchor].key]  # 原来就在的内容被顶起了多少
        elif moved_up:
            delta = self._offset(count - 1) - self.slot_height  # 第一条历史：从当前句区域的位置出发
        else:
            delta = 0.0
        self._anim.stop()
        if self.reviewing:
            # 正在回看：不做动画，把翻的位置跟着挪，眼前的内容就留在原位
            self._shift, self._slot_hidden = 0.0, False
            self.scroll = min(max(0.0, self.scroll + delta), self.max_scroll())
        else:
            start = self._shift + delta if anchor is not None else delta
            self._slot_hidden = moved_up and abs(start) > 0.5
            if abs(start) > 0.5:
                self._anim.setStartValue(start)
                self._anim.setEndValue(0.0)
                self._anim.start()
            else:
                self._shift = 0.0
        self.update()
        self.scrolled.emit()

    def relayout_all(self) -> None:
        """按现在的宽度把所有历史重新排一遍（窗口宽窄变了之后）。"""
        for item in self._items:
            self._ensure(item)
        self.scroll = min(self.scroll, self.max_scroll())
        self.update()
        self.scrolled.emit()

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

    def _rebuild(self, history: list[tuple[object, str]]) -> None:
        """按新的列表更新历史：没变的一对沿用排好的文档，只有新的、文字变了的才重新排版。"""
        cache = {item.key: item for item in self._items}
        width = max(1, self.width())
        items = []
        for key, text in history:
            item = cache.pop(key, None)
            if item is None:
                doc = self._layout(text)
                item = _Pair(key, text, doc, doc.size().height(), width)
            elif item.html != text:
                item.html = text
                item.doc.setHtml(text)
                item.doc.setTextWidth(width)
                item.height, item.width = item.doc.size().height(), width
            items.append(item)
        for item in cache.values():  # 不再显示的：把文档还给 Qt
            item.doc.deleteLater()
        self._items = items
        self._total = sum(item.height for item in items) + self.gap * len(items)

    def _ensure(self, item: _Pair) -> None:
        """这一对是按别的宽度排的版，就按现在的宽度重排。"""
        width = max(1, self.width())
        if item.width != width:
            item.doc.setTextWidth(width)
            height = item.doc.size().height()
            self._total += height - item.height
            item.height, item.width = height, width

    def _offset(self, index: int) -> float:
        """第 index 对历史的顶边离字幕区底边多远：先是当前句区域，再往上逐对累加。"""
        return self.slot_height + sum(item.height + self.gap for item in self._items[index:])

    # ---- 动画和绘制 ----

    def _on_shift(self, value) -> None:
        self._shift = float(value)
        self.update()

    def _on_anim_done(self) -> None:
        self._shift = 0.0
        self._slot_hidden = False
        self.update()

    def resizeEvent(self, event) -> None:
        if event.oldSize().width() != self.width():
            for doc in self._slot_docs:
                doc.setTextWidth(max(1, self.width()))
            if len(self._items) <= RELAYOUT_NOW:
                for item in self._items:
                    self._ensure(item)
            else:
                self._relayout_timer.start()
        self.slot_height = max(self._slot_base, self._slot_needed())
        self.scroll = min(self.scroll, self.max_scroll())
        super().resizeEvent(event)
        self.scrolled.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        slot_top = self.height() - self.slot_height
        # 历史不能画进当前句区域；只有一对正从当前句区域滑上去的那一下例外（这时当前句先不画）
        painter.setClipRect(0, 0, self.width(), self.height() if self._slot_hidden else int(slot_top))
        y = slot_top + self._shift + self.scroll  # 从最新的一对往上画
        for age, item in enumerate(reversed(self._items)):
            self._ensure(item)
            y -= self.gap + item.height
            if y + item.height < 0:
                break  # 再往上的已经在字幕区外面了
            if y < self.height():
                painter.save()
                painter.translate(0, y)
                painter.setOpacity(self.pair_opacity(age))
                item.doc.drawContents(painter)
                painter.restore()
        if self._slot_hidden:
            return
        source, translation = self._slot_docs
        painter.setOpacity(1.0)
        painter.setClipRect(0, int(slot_top), self.width(), int(self.slot_height) + 1)
        painter.translate(0, slot_top)
        source.drawContents(painter)
        painter.translate(0, 0 if source.isEmpty() else source.size().height())  # 译文紧接在原文下面
        translation.drawContents(painter)


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
        self.scrollback = max(10, cfg.scrollback)
        self.click_through = False
        self.collapsed = False  # 收起成小条
        self._expanded = QRect()  # 收起之前窗口的位置和大小
        self.on_reconnect = None  # 由 ui/app.py 设置：重新连接音频设备
        self.on_show_panel = None  # 由 ui/app.py 设置：打开控制面板

        self.units: OrderedDict[int, UnitSnapshot] = OrderedDict()  # 按编号从小到大
        self._seen_at: dict[int, float] = {}  # 每个单元第一次出现的时刻
        self._first_text_at: dict[int, float] = {}  # 每个单元译文开始出现的时刻
        self._open: set[int] = set()  # 还没翻完的单元：有它们在，就要定时检查“等太久就往下走”
        self._pairs: dict[int, tuple] = {}  # 每个单元排版用的富文本，文字和字号没变就不重新生成
        self._dirty = True  # 有东西变了，下一次 refresh 要重新安排显示
        self.live = ("", "")  # 正在识别的 (已确认但还没凑成一句的原文, 暂定的尾巴)
        self.live_translation = ""  # 正在说的这句话到目前为止的译文（草稿）
        self._promoted = -1  # 已经滑进历史的最新一个单元编号（滑进去就不再回到当前句区域）
        self.hearing = False
        self.status = "正在启动……"

        self.captions = CaptionView(self)
        shadow = QGraphicsDropShadowEffect(self.captions)  # 文字阴影：背景调得很透明时，压在亮色画面上也看得清
        shadow.setBlurRadius(8)
        shadow.setOffset(0, 1)
        shadow.setColor(QColor(0, 0, 0, 230))
        self.captions.setGraphicsEffect(shadow)
        self.captions.scrolled.connect(self.update)  # 滑动条画在这个窗口上
        self.grip = QSizeGrip(self)
        self.grip.resize(16, 16)

        self._drag_from: QPoint | None = None
        self._pressed_at: QPoint | None = None  # 鼠标按下的位置（屏幕坐标），用来区分点击和拖动
        self._pressed_button = False  # 鼠标是不是按在了右上角的“收起”按钮上
        self._thumb_grab: float | None = None  # 正在拖滑动条：按下时鼠标离滑块顶边多远
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)  # “等太久就往下走”要看时间，所以定时检查；内容没变时什么也不做
        self._timer.start(50)
        self._restore_geometry()

    # ---- 数据更新（主线程） ----

    def set_live(self, pending: str, partial: str, translation: str = "") -> None:
        """正在识别的原文和它到目前为止的译文（草稿），显示在底部的当前句区域。
        草稿会随着话往下说而更新，但只在这块预留的区域里变，不会带动上面的历史。"""
        if (pending, partial, translation) != (*self.live, self.live_translation):
            self.live, self.live_translation = (pending, partial), translation
            self._dirty = True
        hearing = bool(pending or partial)
        if hearing != self.hearing:
            self.hearing = hearing
            self.update()

    def set_unit(self, unit: UnitSnapshot) -> None:
        moment = now()
        self._seen_at.setdefault(unit.id, moment)
        if unit.translation and not unit.tentative:  # 草稿不算：要等正式译文开始出现
            self._first_text_at.setdefault(unit.id, moment)
        out_of_order = unit.id not in self.units and bool(self.units) and unit.id < next(reversed(self.units))
        self.units[unit.id] = unit
        if out_of_order:  # 两个单元几乎同时发出时，通知到达的先后可能反过来：按编号排回去
            self.units = OrderedDict(sorted(self.units.items()))
        if unit.done or unit.error:
            self._open.discard(unit.id)
        else:
            self._open.add(unit.id)
        while len(self.units) > self.scrollback:  # 更早的不留在字幕窗里了，完整的内容在会话记录里
            old_id, _ = self.units.popitem(last=False)
            for store in (self._seen_at, self._first_text_at, self._pairs):
                store.pop(old_id, None)
            self._open.discard(old_id)
        self._dirty = True

    def set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            self.update()

    def clear(self) -> None:
        self.units.clear()
        self._seen_at.clear()
        self._first_text_at.clear()
        self._open.clear()
        self._pairs.clear()
        self._promoted = -1
        self._dirty = True
        self.captions.scroll_to_latest()

    # ---- 显示什么 ----

    def _ready(self, unit: UnitSnapshot, moment: float) -> bool:
        """译文翻完了（或者等得太久了），这一对可以往下走。"""
        first = self._first_text_at.get(unit.id)
        return (
            unit.done
            or bool(unit.error)
            or unit.tentative  # 下面已经显示着草稿：不用占着当前句区域等正式译文，下一句一开始就让位
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
        return history, slot

    def _pair(self, unit: UnitSnapshot, zh: int, fr: int) -> str:
        cached = self._pairs.get(unit.id)
        if cached is None or cached[0] is not unit or cached[1:3] != (zh, fr):
            cached = self._pairs[unit.id] = (unit, zh, fr, pair_html(unit, zh, fr))
        return cached[3]

    def refresh(self) -> None:
        if self.collapsed or (not self._dirty and not self._open):
            return  # 收起来了；或者没有东西变、也没有正在等的译文：什么都不用做
        self._dirty = False
        zh = self.font_size
        fr = max(11, round(zh * self.source_ratio))
        history, slot = self.compose()
        history_blocks = [(unit.id, self._pair(unit, zh, fr)) for unit in history]
        if slot[0] == "unit":
            slot_html = (source_html(slot[1].source, TEXT_COLORS[0], fr), translation_html(slot[1], TEXT_COLORS[1], zh))
        elif slot[0] == "live":
            slot_html = (live_html(slot[1], slot[2], fr), draft_html(self.live_translation, zh))
        else:
            slot_html = ("", "")
        self.captions.gap = max(6, round(zh * 0.4))
        self.captions.set_fonts(fr, zh)
        self.captions.set_content(history_blocks, slot_html)

    # ---- 收起和展开 ----

    def set_collapsed(self, collapsed: bool) -> None:
        """收起成一个小条（只留“正在听”的圆点），或者展开回原来的样子。收起时识别、翻译、记录照常进行。"""
        if collapsed == self.collapsed:
            return
        if collapsed:
            self._expanded = self.geometry()
            self.collapsed = True
            self.captions.hide()
            self.grip.hide()
            pill = QRect(0, 0, *PILL_SIZE)
            pill.moveBottomLeft(self._expanded.bottomLeft())  # 小条留在原来窗口的左下角
            self.setGeometry(pill)
        else:
            geometry = self._expanded_geometry()
            self.collapsed = False
            self.setGeometry(geometry)
            self.captions.show()
            self.grip.show()
            self._dirty = True  # 收起期间来的句子，展开时一次补上
            self.refresh()
        self.update()

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self.collapsed)

    def _expanded_geometry(self) -> QRect:
        """展开后窗口该在哪：大小和收起前一样，左下角跟着小条走（小条可能被拖到了别处），并且不超出屏幕。"""
        rect = QRect(self._expanded)
        rect.moveBottomLeft(self.geometry().bottomLeft())
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        rect.moveLeft(max(area.left(), min(rect.left(), area.right() - rect.width() + 1)))
        rect.moveTop(max(area.top(), min(rect.top(), area.bottom() - rect.height() + 1)))
        return rect

    # ---- 绘制 ----

    def _collapse_button(self) -> QRect:
        """右上角“收起”按钮的位置（在小圆点左边）。"""
        return QRect(self.width() - 50, 3, 24, 16)

    def _review_hint(self) -> QRect:
        """回看时顶部那行提示的位置（点它回到最新）。"""
        return QRect(self.width() // 2 - 120, 2, 240, 17)

    def _scroll_track(self) -> QRect:
        """滑动条的轨道：贴着字幕区的右边，和历史区域一样高。"""
        area = self.captions.geometry()
        return QRect(self.width() - 13, area.top(), 6, max(1, int(self.captions.view_height())))

    def _scroll_thumb(self) -> QRect | None:
        """滑块的位置；历史没有多到要翻的时候返回 None。滑块在最下面 = 停在最新。"""
        longest = self.captions.max_scroll()
        if longest <= 0:
            return None
        track = self._scroll_track()
        view = self.captions.view_height()
        height = min(track.height(), max(24, round(track.height() * view / (view + longest))))
        top = track.top() + round((track.height() - height) * (1 - self.captions.scroll / longest))
        return QRect(track.left(), top, track.width(), height)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        font = QFont()
        font.setPixelSize(12)
        painter.setFont(font)
        dot = QColor(92, 214, 128) if self.hearing else QColor(130, 130, 130, 150)  # 听到有人说话时变绿
        if self.collapsed:  # 小条：背景至少要看得见，圆点照常亮，文字提示怎么展开
            painter.setBrush(QColor(12, 12, 14, round(255 * max(self.opacity, 0.45))))
            painter.drawRoundedRect(self.rect(), self.height() / 2, self.height() / 2)
            painter.setBrush(dot)
            painter.drawEllipse(QPointF(16, self.height() / 2), 3.5, 3.5)
            painter.setPen(QColor("#dcdcdc"))
            painter.drawText(self.rect().adjusted(28, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter, "同传字幕 · 点击展开")
            return
        painter.setBrush(QColor(12, 12, 14, round(255 * self.opacity)))
        painter.drawRoundedRect(self.rect(), 14, 14)
        painter.setBrush(dot)
        painter.drawEllipse(QPointF(self.width() - 14, 12), 3.5, 3.5)
        button = self._collapse_button()  # “收起”按钮：一个小圆角框，里面一道横线
        painter.setBrush(QColor(255, 255, 255, 60 if self._pressed_button else 26))
        painter.drawRoundedRect(button, 5, 5)
        painter.setPen(QColor(255, 255, 255, 190))
        painter.drawLine(button.center().x() - 5, button.center().y() + 1, button.center().x() + 5, button.center().y() + 1)
        painter.setPen(Qt.PenStyle.NoPen)
        thumb = self._scroll_thumb()
        if thumb is not None:  # 滑动条：平时很淡，回看或者拖动的时候亮一些
            active = self.captions.reviewing or self._thumb_grab is not None
            painter.setBrush(QColor(255, 255, 255, 22))
            painter.drawRoundedRect(self._scroll_track(), 3, 3)
            painter.setBrush(QColor(255, 255, 255, 150 if active else 70))
            painter.drawRoundedRect(thumb, 3, 3)
        if self.status:  # 状态提示：左上角一行小字，不占字幕的位置
            painter.setPen(QColor("#ffd479"))
            right = self._review_hint().left() - 8 if self.captions.reviewing else self._collapse_button().left() - 8
            painter.drawText(QRect(20, 3, max(0, right - 20), 16), Qt.AlignmentFlag.AlignLeft, self.status)
        if self.captions.reviewing:
            painter.setPen(QColor("#9fd0ff"))
            painter.drawText(self._review_hint(), Qt.AlignmentFlag.AlignCenter, "正在回看 · 点这里回到最新 ↓")

    def resizeEvent(self, event) -> None:
        # 手动摆放字幕区，不用布局管理器：布局会根据内容改窗口的最小尺寸，文字一多窗口就被撑大
        self.captions.setGeometry(20, 20, max(1, self.width() - 40), max(1, self.height() - 34))
        self.grip.move(self.width() - self.grip.width() - 4, self.height() - self.grip.height() - 4)
        super().resizeEvent(event)

    # ---- 鼠标：拖动、回看、右键菜单 ----

    def wheelEvent(self, event) -> None:
        """滚轮或触控板双指滑动：往回翻历史。"""
        if self.collapsed:
            return
        delta = event.pixelDelta().y()  # 触控板给的是像素，跟手
        if not delta:
            delta = event.angleDelta().y() / 120 * self.font_size * 3  # 鼠标滚轮：一格大约三行
        if delta:
            self.captions.scroll_by(delta)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        self._pressed_at = event.globalPosition().toPoint()
        thumb = None if self.collapsed else self._scroll_thumb()
        if self.collapsed:  # 小条：按住能拖走，原地点一下是展开（松开时判断）
            self._drag_from = self._pressed_at - self.frameGeometry().topLeft()
        elif self._collapse_button().adjusted(-3, -3, 3, 3).contains(pos):
            self._pressed_button = True
            self.update()
        elif thumb is not None and self._scroll_track().adjusted(-7, 0, 7, 0).contains(pos):
            if not thumb.adjusted(-7, 0, 7, 0).contains(pos):
                self._move_thumb(pos.y() - thumb.height() / 2)  # 点在滑块外面的轨道上：滑块直接跳过来
                thumb = self._scroll_thumb()
            self._thumb_grab = pos.y() - thumb.top()
            self.update()
        elif self.captions.reviewing and self._review_hint().contains(pos):
            self.captions.scroll_to_latest()
        else:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._thumb_grab is not None:
            self._move_thumb(event.position().y() - self._thumb_grab)
        elif self._drag_from is not None:
            self.move(event.globalPosition().toPoint() - self._drag_from)

    def mouseReleaseEvent(self, event) -> None:
        moved = self._pressed_at is None or (event.globalPosition().toPoint() - self._pressed_at).manhattanLength()
        clicked = self._pressed_at is not None and moved <= CLICK_SLOP
        on_button, self._pressed_button = self._pressed_button, False
        self._drag_from = self._pressed_at = None
        if self._thumb_grab is not None:
            self._thumb_grab = None
            self.update()
        if self.collapsed and clicked:
            self.set_collapsed(False)
        elif on_button:
            if self._collapse_button().adjusted(-3, -3, 3, 3).contains(event.position().toPoint()):
                self.set_collapsed(True)
            else:
                self.update()

    def _move_thumb(self, top: float) -> None:
        """把滑块的顶边放到 top（窗口坐标），历史跟着翻到对应的位置。"""
        track, thumb = self._scroll_track(), self._scroll_thumb()
        if thumb is None or track.height() <= thumb.height():
            return
        fraction = 1 - (top - track.top()) / (track.height() - thumb.height())
        self.captions.scroll_to(fraction * self.captions.max_scroll())

    def contextMenuEvent(self, event) -> None:
        self.build_menu(QMenu(self)).exec(event.globalPos())

    def build_menu(self, menu: QMenu) -> QMenu:
        if self.on_show_panel is not None:
            menu.addAction("控制面板（换语言、开始 / 暂停）", self.on_show_panel)
        if not self.isVisible():
            menu.addAction("显示字幕窗", self.show)
        menu.addAction("展开字幕窗" if self.collapsed else "收起字幕窗", self.toggle_collapsed)
        if self.captions.reviewing and not self.collapsed:
            menu.addAction("回到最新字幕", self.captions.scroll_to_latest)
        menu.addSeparator()
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
        height = round(self.font_size * 10)
        self.setGeometry(screen.center().x() - width // 2, screen.bottom() - height - 60, width, height)

    def save_settings(self) -> None:
        # 收起着退出的话，记下的是展开后的位置和大小：下次启动是展开的
        self.settings.setValue("geometry", self._expanded_geometry() if self.collapsed else self.geometry())
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


def activate_app() -> None:
    """macOS：把本程序切到前台。不在程序坞显示图标的程序（见 make_accessory_app）不会自己到前台来，
    普通窗口（控制面板）一显示就可能被别的窗口盖住。"""
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
    activate = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)(address)
    app = get(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
    activate(app, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)


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
