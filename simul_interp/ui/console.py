"""终端界面：每段原文和译文各打一行；最底下一行实时刷新“正在说的话”（灰色是还没确认的部分）。"""

from __future__ import annotations

import os
import shutil
import sys
import threading

from ..translate import TranslationUnit

GREY, BOLD, CYAN, RED, RESET = "\033[90m", "\033[1m", "\033[36m", "\033[31m", "\033[0m"


class ConsoleView:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = ""
        self._partial = ""
        self._translation = ""
        self._source_shown: set[int] = set()
        self._translation_shown: set[int] = set()
        self._tty = sys.stdout.isatty()
        if sys.platform == "win32" and self._tty:
            os.system("")  # 常用小技巧：让旧版 Windows 控制台开启 ANSI 颜色码（虚拟终端模式）

    def on_live(self, pending: str, partial: str, translation: str = "") -> None:
        with self._lock:
            self._pending, self._partial, self._translation = pending, partial, translation
            self._redraw()

    def on_status(self, text: str) -> None:
        if text:
            with self._lock:
                self._println(f"{GREY}{text}{RESET}")

    def on_unit(self, unit: TranslationUnit) -> None:
        with self._lock:
            if unit.id not in self._source_shown:
                self._source_shown.add(unit.id)
                self._println(f"{BOLD}#{unit.id} 法{RESET} {unit.source}")
            if unit.done and unit.id not in self._translation_shown:
                self._translation_shown.add(unit.id)
                if unit.error:
                    self._println(f"{RED}#{unit.id} 中 翻译失败：{unit.error}{RESET}")
                elif unit.translation:
                    timing = ""
                    if unit.first_token_at is not None:
                        wait = unit.first_token_at - unit.ready_at
                        first = f"提前 {-wait:.2f}s" if wait < 0 else f"首字 {wait:.2f}s"
                        timing = f"{first}，完成 {unit.done_at - unit.ready_at:.2f}s"
                        if unit.speculative:
                            timing += "（投机翻译命中）"
                    self._println(f"{CYAN}#{unit.id} 中{RESET} {unit.translation}  {GREY}{timing}{RESET}")

    def _println(self, text: str) -> None:
        if self._tty:
            sys.stdout.write("\r\033[2K")
        sys.stdout.write(text + "\n")
        self._redraw()

    def _redraw(self) -> None:
        if not self._tty:
            sys.stdout.flush()
            return
        pending, partial = self._pending, self._partial
        text = f"{pending} {partial}".strip()
        room = max(10, shutil.get_terminal_size().columns - 4)
        tail = ""
        if self._translation:  # 投机译文：还没确认，青色显示在后面；中文在终端里占两格宽
            chinese = self._translation[-max(6, room // 4) :]
            tail = f"  {CYAN}⟶ {chinese}{RESET}"
            room = max(10, room - 2 * len(chinese) - 4)  # 整行不能超过终端宽度，否则换行后清不干净
        split = len(pending)  # text 里暂定部分从这里开始
        if len(text) > room:  # 太长只显示最后面，也就是最新的部分
            cut = len(text) - room
            text, split = "…" + text[cut:], max(0, split - cut) + 1
        line = f"{text[:split]}{GREY}{text[split:]}{RESET}" if text else ""
        sys.stdout.write(f"\r\033[2K▸ {line}{tail}")
        sys.stdout.flush()
