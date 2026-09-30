# Simultaneous-Interpretation · 法语 → 中文实时同传

内录电脑正在播放的声音（视频会议、视频网站），实时识别法语并翻译成中文，在一个透明悬浮窗里同时显示原文和译文。
内录不会影响你听原声：戴耳机或者用扬声器都照常能听到。

> 当前状态：开发中。每个阶段的进度见下方清单，详细过程见 [docs/DEVLOG.md](docs/DEVLOG.md)。

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
- [ ] S2 音频文件模拟源与测试音频
- [ ] S3 语音检测与分段
- [ ] S4 Whisper 流式识别
- [ ] S5 流式翻译
- [ ] S6 串起完整流程（终端版）
- [ ] S7 透明悬浮字幕窗
- [ ] S8 Windows 支持
- [ ] S9 延迟优化

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

## 怎么通过这个仓库学习

每完成一个小阶段就单独提交一次，提交历史本身就是一份搭建教程：

```bash
git log --reverse --stat
```

每个阶段遇到的问题、做法和验证方式记录在 [docs/DEVLOG.md](docs/DEVLOG.md)。
