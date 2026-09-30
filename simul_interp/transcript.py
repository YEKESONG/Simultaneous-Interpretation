"""会话记录：每翻完一句就追加写进 Markdown，程序中途退出也不会丢已经翻好的内容。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .clock import now
from .translate import TranslationUnit


class TranscriptWriter:
    def __init__(self, directory: Path, source_name: str, meta: dict[str, str], stem: str | None = None) -> None:
        """stem：用音频文件做输入时传文件名（不含扩展名），记录就叫 <stem>_译文.md；
        内录时不传，按时间命名为 同传_<日期>_<时间>_译文.md。"""
        directory.mkdir(parents=True, exist_ok=True)
        base = stem or f"同传_{time.strftime('%Y-%m-%d_%H%M%S')}"
        path = directory / f"{base}_译文.md"
        if path.exists():  # 不覆盖已有的记录
            path = directory / f"{base}_{time.strftime('%H%M%S')}_译文.md"
        self.path = path
        self._started = now()
        self._lock = threading.Lock()
        self._count = 0
        header = [f"# 同传记录：{source_name}", ""]
        header += [f"- {key}：{value}" for key, value in meta.items()]
        header += [f"- 开始时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", "", "---", ""]
        self._write("\n".join(header) + "\n", mode="w")

    def add(self, unit: TranslationUnit) -> None:
        elapsed = int(unit.ready_at - self._started)
        stamp = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        translation = unit.translation or (f"（翻译失败：{unit.error}）" if unit.error else "（未翻译）")
        self._write(f"**[{stamp}]** {unit.source}\n\n> {translation}\n\n")
        self._count += 1

    def close(self) -> None:
        self._write(f"---\n\n共 {self._count} 句，结束时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    def _write(self, text: str, mode: str = "a") -> None:
        with self._lock, self.path.open(mode, encoding="utf-8") as f:
            f.write(text)
