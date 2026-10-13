"""图形界面的入口：把控制面板、字幕窗、菜单栏图标和后台的同传流水线接在一起。

主线程跑 Qt 的界面；流水线（录音、识别、翻译）在一个后台线程里跑，结果通过 Qt 信号回到主线程。
界面对流水线的操作（暂停、换语言）是直接调用它的方法，这些方法本身可以在任何线程里调用。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .overlay import (
    Bridge,
    Overlay,
    QtView,
    activate_app,
    float_over_fullscreen,
    make_accessory_app,
    tray_icon_pixmap,
)
from .panel import ControlPanel

logger = logging.getLogger(__name__)

QUIT_AFTER_FILE_MS = 5000  # 用音频文件模拟时，放完之后过这么久自动退出


def run_app(cfg, interpreter_factory) -> int:
    """interpreter_factory(view) 返回 Interpreter。阻塞到程序退出。"""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关掉控制面板不等于退出：字幕窗和同传还在
    make_accessory_app()

    bridge = Bridge()
    overlay = Overlay(cfg.ui)
    bridge.live.connect(overlay.set_live)
    bridge.unit.connect(overlay.set_unit)
    bridge.status.connect(overlay.set_status)
    interpreter = interpreter_factory(QtView(bridge))
    panel = ControlPanel(interpreter, overlay)
    bridge.status.connect(panel.set_message)

    def show_panel() -> None:
        panel.bring_to_front()
        activate_app()

    overlay.on_reconnect = interpreter.reconnect_audio
    overlay.on_show_panel = show_panel

    def work() -> None:
        try:
            interpreter.run()
        except Exception as exc:  # 在界面上显示错误，而不是悄悄退出
            logger.exception("同传出错")
            bridge.status.emit(f"出错：{exc}")
        finally:
            bridge.finished.emit()

    if cfg.audio.source == "file":  # 用文件模拟时放完就自动退出；内录要用户自己退出
        # finished 是从后台线程发出的；接收方是主线程里的定时器对象，Qt 会把这次调用排进主线程的事件队列
        quit_timer = QTimer(app)
        quit_timer.setSingleShot(True)
        quit_timer.setInterval(QUIT_AFTER_FILE_MS)
        quit_timer.timeout.connect(app.quit)
        bridge.finished.connect(quit_timer.start)
    worker = threading.Thread(target=work, name="interpreter", daemon=True)
    worker.start()

    # 开发调试：定时把字幕窗和控制面板截图存下来（默认每 10 秒；SI_OVERLAY_SNAPSHOT_MS 可以改间隔）
    snapshot_dir = os.environ.get("SI_OVERLAY_SNAPSHOTS")
    if snapshot_dir:
        Path(snapshot_dir).mkdir(parents=True, exist_ok=True)

        def snapshot() -> None:
            stamp = f"{time.time():.2f}"
            overlay.grab().save(str(Path(snapshot_dir) / f"overlay_{stamp}.png"))
            panel.grab().save(str(Path(snapshot_dir) / f"panel_{stamp}.png"))

        snapshots = QTimer(overlay)
        snapshots.timeout.connect(snapshot)
        snapshots.start(int(os.environ.get("SI_OVERLAY_SNAPSHOT_MS", "10000")))

    # 菜单栏图标：控制面板关掉了、字幕窗收起或穿透了，都能从这里找回来
    tray = QSystemTrayIcon(QIcon(tray_icon_pixmap()), app)
    tray.setToolTip("实时同传")
    tray_menu = QMenu()
    overlay.build_menu(tray_menu)
    tray_menu.aboutToShow.connect(lambda: (tray_menu.clear(), overlay.build_menu(tray_menu)))
    tray.setContextMenu(tray_menu)
    tray.show()

    overlay.show()
    float_over_fullscreen(overlay)
    area = app.primaryScreen().availableGeometry()
    panel.move(area.right() - panel.width() - 40, area.top() + 60)  # 面板放在右上角，不挡屏幕下方的字幕
    show_panel()

    # Qt 的事件循环在 C++ 里，Python 收不到 Ctrl+C；定时回到 Python 一下，让信号处理有机会执行
    previous = signal.signal(signal.SIGINT, lambda *_: app.quit())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(200)

    app.exec()
    signal.signal(signal.SIGINT, previous)
    overlay.save_settings()
    tray.hide()
    panel.hide()
    overlay.hide()
    interpreter.stop()
    worker.join(timeout=15)
    return 0
