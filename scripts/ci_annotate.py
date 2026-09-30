#!/usr/bin/env python3
"""CI 失败时，把日志末尾写成 GitHub Actions 的错误注释（::error）。

GitHub 要登录才能看 Actions 日志，但“检查注释”可以通过公开 API 读取：
  GET /repos/<owner>/<repo>/check-runs/<job id>/annotations
这样不用登录也能知道 CI 为什么失败。用法：python scripts/ci_annotate.py <日志文件> <标题>
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_CHARS = 6000  # 注释内容有长度限制，取日志最后一段（报错一般在最后）


def escape(text: str) -> str:
    # 工作流命令里 % 和换行要转义
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> int:
    log = Path(sys.argv[1])
    title = sys.argv[2] if len(sys.argv) > 2 else log.name
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else "（没有日志）"
    print(f"::error title={escape(title)}::{escape(text[-MAX_CHARS:])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
