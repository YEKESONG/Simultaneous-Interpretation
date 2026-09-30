#!/usr/bin/env bash
# 编译 macOS 内录辅助程序 bin/si-audio-tap。
# 只需要 Xcode 命令行工具（xcode-select --install），不需要完整 Xcode。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/native/macos/si_audio_tap.swift"
PLIST="$ROOT/native/macos/Info.plist"
OUT="$ROOT/bin/si-audio-tap"

mkdir -p "$ROOT/bin"
# -sectcreate 把 Info.plist 嵌进可执行文件，里面的 NSAudioCaptureUsageDescription 是系统弹权限框时显示的说明
swiftc -O -swift-version 5 \
  -target "$(uname -m)-apple-macos14.2" \
  "$SRC" -o "$OUT" \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$PLIST"
echo "已生成 $OUT"
