#!/bin/bash
# 双击启动同传（macOS）。会开一个终端窗口显示运行日志；在控制面板上点“退出”结束程序。
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -m simul_interp "$@"
