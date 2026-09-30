# Simultaneous-Interpretation · 法语 → 中文实时同传

[![tests](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml/badge.svg)](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml)

内录电脑正在播放的声音（视频会议、视频网站），实时识别法语并翻译成中文，在一个透明悬浮窗里同时显示原文和译文。
内录不会影响你听原声：戴耳机或者用扬声器都照常能听到。

> 当前状态：开发中。每个阶段的进度见下方清单，详细过程见 [docs/DEVLOG.md](docs/DEVLOG.md)。

![悬浮字幕窗效果（示例文字）](docs/overlay_preview.png)

## 工作原理

```
系统声音 ─► 语音检测 ─► 流式识别 ─► 按句切分 ─► 大模型翻译（流式）─► 透明字幕窗
   │           │            │                          │                  │
   │       Silero VAD   Whisper large-v3        OpenAI 兼容 API        PySide6
   ├─ macOS：Core Audio Taps（自写的 Swift 小程序）
   └─ Windows：WASAPI loopback
```

- **录音**：只“复制”一份系统声音，不改变声音的去向，所以不需要虚拟声卡，也不会出现戴耳机听不到的问题。
- **识别**：Whisper 不是流式模型，这里用“连续两次识别结果一致才确认”的策略（LocalAgreement）实现边听边出字。
- **翻译**：按句子（或较长的分句）送给大模型，流式返回，第一个字一出来就显示。

## 进度

- [x] S0 项目骨架与开发规范
- [x] S1 macOS 系统声音采集
- [x] S2 音频文件模拟源与测试音频
- [x] S3 语音检测与分段
- [x] S4 Whisper 流式识别
- [x] S5 流式翻译
- [x] S6 串起完整流程（终端版）
- [x] S7 透明悬浮字幕窗
- [x] S8 Windows 支持（代码和自动测试已完成，未在 Windows 真机上验证）
- [x] S9 延迟优化（测量工具、停顿时提前识别、量化选项、投机翻译）

## 开发环境

需要 Python 3.11 以上。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

配置：把 `config.example.toml` 复制为 `config.toml`，把 `.env.example` 复制为 `.env` 并填入翻译 API 的密钥。

```bash
.venv/bin/python -m simul_interp config
```

### macOS 内录

需要 macOS 14.2 以上和 Xcode 命令行工具（`xcode-select --install`）。第一次运行会自动编译 `bin/si-audio-tap`，并弹出“系统录音”权限框，点允许即可。

```bash
.venv/bin/python -m simul_interp record --seconds 10   # 录 10 秒系统声音，显示音量并保存 WAV
```

如果一直显示静音：打开 系统设置 → 隐私与安全性 → 屏幕与系统录音，在“仅系统录音”里打开 si-audio-tap。

### Windows

需要 Windows 10/11、Python 3.11 以上。强烈建议有 NVIDIA 显卡：large-v3 在纯 CPU 上基本做不到实时。

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

- 内录用系统自带的 WASAPI loopback，不需要虚拟声卡，也不需要授权；戴耳机、用扬声器都照常能听到。
- 识别用 faster-whisper（同一个 large-v3 模型的另一种格式）。第一次使用要下载约 3 GB 的模型：确认后在 `config.toml` 的 `[asr]` 里设 `allow_download = true`。
- 显卡加速需要按 [faster-whisper 的说明](https://github.com/SYSTRAN/faster-whisper#gpu) 安装 NVIDIA 的 CUDA 库。
- 切换输出设备（插耳机、连蓝牙）后，用悬浮窗右键菜单里的“重新连接音频设备”。
- 状态：Windows 部分还没有在真机上运行过。GitHub Actions 会在 Windows 虚拟机上完整安装依赖、检查模块能否导入、跑逻辑测试。

## 使用

```bash
.venv/bin/python -m simul_interp run                               # 内录系统声音，透明悬浮字幕窗
.venv/bin/python -m simul_interp run --ui console                  # 内录系统声音，终端显示
.venv/bin/python -m simul_interp run --ui console --file 录音.m4a  # 用音频文件模拟
.venv/bin/python -m simul_interp run --ui console --mock-translate # 不联网的模拟翻译，测试用
```

每次会话的原文和译文会保存为 Markdown，位置由 `config.toml` 的 `[transcript] dir` 决定。

悬浮字幕窗的操作：
- 拖动移动，右下角拖动调整大小；
- 右键菜单：调字号、背景深浅、鼠标穿透（点击直接落到下面的窗口）、清空、退出；
- 开启鼠标穿透后，用菜单栏的“译”字图标解锁或退出；
- 位置、大小、字号、背景透明度会自动保存。

测量工具：

```bash
.venv/bin/python scripts/make_test_audio.py                # 生成法语测试音频（macOS 合成语音）
.venv/bin/python -m simul_interp bench-asr samples/fr_meeting.wav   # 流式识别的延迟和准确率
.venv/bin/python -m simul_interp bench-translate           # 翻译服务的首字延迟
.venv/bin/python -m simul_interp bench-pipeline samples/fr_meeting.wav --mock-translate  # 端到端：说完到出现中文
.venv/bin/python -m simul_interp --set asr.quantize_bits=4 bench-asr samples/fr_meeting.wav  # --set 临时改配置做对比
```

目前的延迟（M5 MacBook Air，完整版 large-v3，法语合成语音；翻译用模拟服务，首字固定约 0.3 秒）：
一句话说完后，**屏幕上开始出现中文的中位数约 1.0 秒**（90% 在 1.6 秒以内）。换成真实翻译 API 后还要加上它的首字延迟，可以用 `bench-translate` 实测。

## 已知问题

- 声音在半句话处突然停止（比如暂停视频）时，最后半句可能被识别模型“补全”错。
- 投机翻译让翻译请求数约为正常的 3 倍；服务商有频率限制或在意费用时，用 `translate.speculative = false` 关掉。
- 无风扇的 Mac 连续运行 large-v3 会发热降频，长时间使用延迟会变大；默认的 4 位量化（`asr.quantize_bits = 4`）能减轻这个问题。
- Windows 部分还没有在真机上运行过（CI 只保证能安装、能导入、逻辑测试通过）。

## 怎么通过这个仓库学习

每完成一个小阶段就单独提交一次，提交历史本身就是一份搭建教程：

```bash
git log --reverse --stat
```

每个阶段遇到的问题、做法和验证方式记录在 [docs/DEVLOG.md](docs/DEVLOG.md)。
