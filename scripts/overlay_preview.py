#!/usr/bin/env python3
"""不跑识别和翻译，只用示例文字把悬浮字幕窗渲染成 PNG，用来调样式、给 README 配图。

窗口不会显示在屏幕上：QWidget.grab() 可以直接把没显示的窗口画到图片里。
为了看出半透明效果，图片背景画了一块模拟视频画面的渐变色。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QSettings  # noqa: E402
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from simul_interp.config import UiConfig  # noqa: E402
from simul_interp.ui.overlay import Overlay, UnitSnapshot  # noqa: E402

EXAMPLES = [  # 示例文字（译文为手写示例，不是 API 输出）
    UnitSnapshot(0, "Les résultats sont encourageants, le taux d'erreur est passé de 12 à 7%.",
                 "结果令人鼓舞，错误率从 12% 降到了 7%。", True, ""),
    UnitSnapshot(1, "Cependant, il reste plusieurs problèmes à résoudre, notamment la latence,",
                 "不过还有几个问题要解决，尤其是延迟，", True, ""),
    UnitSnapshot(2, "qui est encore trop élevée pour une utilisation en temps réel.",
                 "它对实时使用来说还是太高了。", True, ""),
]


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "overlay_preview.png"
    app = QApplication(sys.argv)
    # 用临时的设置文件，不读写你真实的字幕窗设置（位置、字号）
    settings = QSettings(str(Path(tempfile.mkdtemp()) / "preview.ini"), QSettings.Format.IniFormat)
    overlay = Overlay(UiConfig(), settings=settings)
    overlay.font_size, overlay.opacity = 22, 0.6
    overlay.resize(920, 300)
    for unit in EXAMPLES:
        overlay.set_unit(unit)
    # 下一句正在识别：显示在底部的当前句区域，暂定部分灰色，译文的位置先留白
    overlay.set_live("Est-ce que quelqu'un a des", "questions sur ce point", "")
    overlay.set_status("")
    overlay.refresh()
    overlay.captions._anim.stop()
    overlay.captions._on_anim_done()  # 截图前让滑动动画直接走完
    shot = overlay.grab()  # Retina 屏上是 2 倍像素，画布也要按同样的缩放比例建

    ratio = shot.devicePixelRatio()
    canvas = QPixmap(round((overlay.width() + 120) * ratio), round((overlay.height() + 140) * ratio))
    canvas.setDevicePixelRatio(ratio)
    painter = QPainter(canvas)
    width, height = overlay.width() + 120, overlay.height() + 140
    gradient = QLinearGradient(0, 0, width, height)
    gradient.setColorAt(0.0, QColor(58, 110, 165))
    gradient.setColorAt(0.5, QColor(214, 170, 96))
    gradient.setColorAt(1.0, QColor(40, 90, 70))
    painter.fillRect(0, 0, width, height, gradient)
    painter.drawPixmap(QPoint(60, 70), shot)
    painter.end()
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out))
    print(f"已保存 {out}")
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
