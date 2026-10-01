# Simultaneous-Interpretation · 法语 → 中文实时同传

[![tests](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml/badge.svg)](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml)

内录电脑正在播放的声音（视频会议、视频网站），实时识别法语并翻译成中文，在一个透明悬浮窗里成对显示原文和译文。
内录不会影响你听原声：戴耳机或者用扬声器都照常能听到。每次会话的原文、译文和一份录音都会保存下来，方便事后校对。

> 当前状态：开发中。每个阶段的进度见下方清单，详细过程见 [docs/DEVLOG.md](docs/DEVLOG.md)。

![悬浮字幕窗效果（示例文字）](docs/overlay_preview.png)

## 工作原理

```
系统声音 ─► 语音检测 ─► 流式识别 ─► 按句切分 ─► 大模型翻译（流式）─► 透明字幕窗
   │           │            │                          │                  │
   │       Silero VAD   Whisper turbo           OpenAI 兼容 API        PySide6
   ├─ macOS：Core Audio Taps（自写的 Swift 小程序）
   └─ Windows：WASAPI loopback
```

- **录音**：只“复制”一份系统声音，不改变声音的去向，所以不需要虚拟声卡，也不会出现戴耳机听不到的问题。
- **识别**：Whisper 不是流式模型，这里用“连续两次识别结果一致才确认”的策略（LocalAgreement）实现边听边出字。
- **翻译**：按句子（或较长的分句）送给大模型；句子还没完全确认就先翻（投机翻译），确认时译文通常已经翻好。
- **显示**：底部是高度固定的“当前句”区域，正在识别的原文在里面实时更新，译文的位置先留白、翻好了直接填进去；下一句开始时，这一对才平滑地滑进上方的历史。区域里怎么变都不会带动别的内容，窗口大小也固定。

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
- [x] S10 真实内录端到端验证；全程序统一高精度时钟
- [x] S11 默认 4 位量化；真实翻译 API 实测
- [x] S12 实时识别改用 turbo（对比实验后由用户决定）
- [x] S13 字幕窗改版：原文译文成对同步出现，不闪不跳
- [x] S13b 当前句区域：暂定文字只在预留的空白里更新，译文填进留白
- [x] S14 录音保存：内录时同步存一份 WAV，方便事后对照录音校对译文
- [x] S14b 会话记录里的时间改成录音里的位置

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

需要 Windows 10/11、Python 3.11 以上。强烈建议有 NVIDIA 显卡：Whisper 在纯 CPU 上很难做到实时。

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

- 内录用系统自带的 WASAPI loopback，不需要虚拟声卡，也不需要授权；戴耳机、用扬声器都照常能听到。
- 识别用 faster-whisper（和 Mac 一样用 large-v3-turbo，只是格式不同）。第一次使用要下载约 1.6 GB 的模型：确认后在 `config.toml` 的 `[asr]` 里设 `allow_download = true`。
- 显卡加速需要按 [faster-whisper 的说明](https://github.com/SYSTRAN/faster-whisper#gpu) 安装 NVIDIA 的 CUDA 库。
- 切换输出设备（插耳机、连蓝牙）后，用悬浮窗右键菜单里的“重新连接音频设备”。
- 状态：Windows 部分还没有在真机上运行过。GitHub Actions 会在 Windows 虚拟机上完整安装依赖、检查模块能否导入、跑逻辑测试。

## 使用

```bash
.venv/bin/python -m simul_interp run                               # 内录系统声音，透明悬浮字幕窗
.venv/bin/python -m simul_interp run --ui console                  # 内录系统声音，终端显示
.venv/bin/python -m simul_interp run --ui console --file 录音.m4a  # 用音频文件模拟
.venv/bin/python -m simul_interp run --ui console --mock-translate # 不联网的模拟翻译，测试用
.venv/bin/python -m simul_interp run --no-recording                # 这一次不保存录音
```

每次会话的原文和译文会保存为 Markdown，位置由 `config.toml` 的 `[transcript] dir` 决定。

内录时还会把听到的声音同步存成一份录音，方便事后对照录音校对译文：
- 位置由 `[recording] dir` 决定，和会话记录同名：`同传_日期_时间.wav` 对应 `同传_日期_时间_译文.md`，记录的开头写着录音的路径；
- 记录里每段前面的时间（如 `[12:34]`）就是这段话在录音里的位置，一般比这段话的开头早不到一秒：校对时把播放器拖到这个时间，就能听到这段话；
- 格式是 16 kHz 单声道 WAV（就是识别模型听到的那一路），每小时约 115 MB；
- 边录边写：程序被强制退出、电脑死机，已经录下的部分也能正常播放（最多少最后 2 秒）；
- 只录“电脑有声音输出”的时间段：没有任何程序在播放声音时，系统不送音频数据，这段空档不会录进去；
- 不想录：在配置里设 `[recording] enabled = false`，或者只这一次加 `--no-recording`；
- 录下别人的发言之前，请先征得对方同意。

悬浮字幕窗的操作：
- 底部是当前句区域（白色是已确认的原文，灰色是暂定的，下面留白等译文），上方是翻好的历史，越旧越暗；
- 右上角的小圆点变绿，表示正在听到有人说话；
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
.venv/bin/python -m simul_interp --set asr.quantize_bits=0 bench-asr samples/fr_meeting.wav  # --set 临时改配置做对比
.venv/bin/python scripts/compare_models.py 录音.wav --configs 模型A:4,模型B:0  # 离线比较几种模型配置的准确率和速度
```

目前的延迟（M5 MacBook Air，Whisper turbo，法语合成语音，真实 DeepSeek API）：
一句话说完后，**屏幕上开始出现中文的中位数约 0.9～1.0 秒**（90% 在 1.2 秒以内），连续说话时也不会越积越大。
各种配置的对比数据见 [docs/DEVLOG.md](docs/DEVLOG.md) 的 S9～S12。

## 已知问题

- 声音在半句话处突然停止（比如暂停视频）时，最后半句可能被识别模型“补全”错。
- 投机翻译让翻译请求数约为正常的 3 倍；服务商有频率限制或在意费用时，用 `translate.speculative = false` 关掉。
- 无风扇的 Mac 长时间满负荷识别会发热降频（实测约慢 40%），延迟会变大；实时识别默认用负载更低的 turbo 来减轻这个问题。
- Windows 部分还没有在真机上运行过（CI 只保证能安装、能导入、逻辑测试通过）。

## 怎么通过这个仓库学习

每完成一个小阶段就单独提交一次，提交历史本身就是一份搭建教程：

```bash
git log --reverse --stat
```

每个阶段遇到的问题、做法和验证方式记录在 [docs/DEVLOG.md](docs/DEVLOG.md)。
