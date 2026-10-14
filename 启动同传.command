#!/bin/bash
# 双击启动同传（macOS）。会开一个终端窗口显示运行日志；在控制面板上点“退出”结束程序。
# 终端窗口要一直开着：关掉它，同传也就退出了。
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "还没有安装好：没有找到 .venv（项目的 Python 环境）。"
  echo "请先按 README 里“安装”一节的步骤操作。"
  read -r -p "按回车关闭这个窗口……"
  exit 1
fi
exec .venv/bin/python -m simul_interp "$@"
