"""终端界面：每段原文和译文各打一行；最底下一行实时刷新“正在说的话”（灰色是还没确认的部分）。"""

from __future__ import annotations

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
        self._source_shown: set[int] = set()
        self._translation_shown: set[int] = set()
        self._tty = sys.stdout.isatty()

    def on_live(self, pending: str, partial: str) -> None:
        with self._lock:
            self._pending, self._partial = pending, partial
            self._redraw()

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
                        timing = f"首字 {unit.first_token_at - unit.ready_at:.2f}s，完成 {unit.done_at - unit.ready_at:.2f}s"
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
        split = len(pending)  # text 里暂定部分从这里开始
        if len(text) > room:  # 太长只显示最后面，也就是最新的部分
            cut = len(text) - room
            text, split = "…" + text[cut:], max(0, split - cut) + 1
        line = f"{text[:split]}{GREY}{text[split:]}{RESET}" if text else ""
        sys.stdout.write(f"\r\033[2K▸ {line}")
        sys.stdout.flush()
