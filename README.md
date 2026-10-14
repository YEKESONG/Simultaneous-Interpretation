# Simultaneous-Interpretation

[![tests](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml/badge.svg)](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)

实时同声传译：采集电脑正在播放的声音，在本地用 Whisper 识别法语，用大模型翻译成中文，显示在透明悬浮字幕窗中。

**macOS** · [Windows](README.windows.md)

![字幕窗](docs/overlay_preview.png)

## 功能

- 采集系统声音，不影响正常收听
- 本地流式语音识别（Whisper large-v3-turbo，MLX）
- 边说边译，译文比原文晚约 1 秒
- 悬浮字幕窗：回看历史、收起、调整大小和字号
- 控制面板：开始 / 暂停，切换源语言和目标语言
- 自动保存会话记录和录音

## 要求

- Apple 芯片 Mac，macOS 14.2 及以上
- 约 5 GB 磁盘空间
- 翻译 API 密钥（可选，默认 [DeepSeek](https://platform.deepseek.com)；不配置时只显示原文）

## 安装

以下命令在“终端”中执行。

### 1. 安装 Xcode Command Line Tools

```bash
xcode-select --install
```

### 2. 安装 Homebrew 和 Python

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

安装程序结束后再运行（已安装 Homebrew 的只需运行最后一行）：

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
brew install python@3.12
```

### 3. 下载项目并安装依赖

```bash
git clone https://github.com/YEKESONG/Simultaneous-Interpretation.git
cd Simultaneous-Interpretation
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4. 下载识别模型（约 1.6 GB）

```bash
.venv/bin/hf download mlx-community/whisper-large-v3-turbo
```

中国大陆网络可在命令前加 `HF_ENDPOINT=https://hf-mirror.com`。

### 5. 配置翻译密钥

```bash
cp .env.example .env
open -e .env
```

填入 `DEEPSEEK_API_KEY=sk-...` 并保存。

### 6. 授权并启动

播放任意有声音的视频，运行：

```bash
.venv/bin/python -m simul_interp record --seconds 10
```

在弹出的系统录音权限请求中点击“允许”。终端每秒输出一行音量即为正常；一直是 -120 dBFS 时，到“系统设置 → 隐私与安全性 → 屏幕与系统录音”中开启 `si-audio-tap`。

启动：

```bash
.venv/bin/python -m simul_interp
```

以后也可以双击项目目录中的 `启动同传.command` 启动。

## 使用

启动后出现控制面板和字幕窗。控制面板用于开始 / 暂停监听和切换语言；关闭面板不会退出程序，可以从菜单栏的“译”图标重新打开。

| 字幕窗 | 操作 |
|---|---|
| 移动 / 调整大小 | 拖动窗口中部 / 拖动边或角 |
| 字号 | 右上角 A− / A+，或 ⌘ + 滚轮 |
| 回看历史 | 滚轮，或拖动右侧滑动条 |
| 收起 | 右上角“–” |
| 背景、鼠标穿透等 | 右键菜单 |

会话记录保存在 `transcripts/`，录音保存在 `recordings/`；记录中每段前的 `[mm:ss]` 对应录音中的位置。录制他人发言前请征得同意。

常用参数：`--paused` 启动后暂不监听，`--no-recording` 不录音，`--ui console` 只在终端输出。全部参数见 `.venv/bin/python -m simul_interp --help`。

## 配置

```bash
cp config.example.toml config.toml
```

各项说明见文件中的注释。翻译服务可以换成任何兼容 OpenAI 接口的服务，或本地的 [Ollama](https://ollama.com)。用“文本编辑”修改时，先关闭“编辑 → 替换 → 智能引号”。

## 常见问题

| 问题 | 解决方法 |
|---|---|
| `command not found: brew` | 运行安装第 2 步中的两行 `eval` / `echo` |
| 提示本地没有找到识别模型 | 完成安装第 4 步 |
| 字幕窗没有文字 | 检查系统录音权限（安装第 6 步） |
| 只有原文，没有译文 | 检查 `.env` 中的密钥和账户余额 |
| 依赖下载慢 | `pip install` 加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |

## 更新与卸载

```bash
git pull && .venv/bin/pip install -r requirements.txt
```

卸载时删除项目目录和 `~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo`。

## 工作原理

```
系统声音 ─► 语音检测 ─► 流式识别 ─► 按句切分 ─► 大模型翻译 ─► 字幕窗
          Silero VAD   Whisper turbo            OpenAI 兼容 API   PySide6
```

系统声音由 Core Audio Taps 采集（Windows 上是 WASAPI loopback）。识别用 LocalAgreement 策略实现流式输出；翻译在句子说完前先出草稿，整句确认后替换为正式译文。设计过程和测试数据见 [docs/DEVLOG.md](docs/DEVLOG.md)。

## 开发

```bash
.venv/bin/python -m pytest
.venv/bin/python scripts/make_test_audio.py
.venv/bin/python -m simul_interp bench-pipeline samples/fr_meeting.wav
```

开发约定见 [CLAUDE.md](CLAUDE.md)。项目按阶段提交，可以用 `git log --reverse` 按顺序查看。

<details>
<summary>开发进度</summary>

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
- [x] S15 杂音时段不再输出 “Merci.”（整句只有它时向识别模型核实）
- [x] S16 没说完的半句话先等一等：说话人停下来想词时不再把一句话切成两半
- [x] S17 字幕窗可以往回翻看：滚轮或右侧的滑动条，回看时眼前的内容不动
- [x] S18 字幕窗可以收起成一个小条，点一下再展开
- [x] S19 流水线支持暂停 / 继续，以及运行中换语言（法、英、西、德、意、葡 → 任意目标语言）
- [x] S20 应用前端：控制面板（换语言、开始 / 暂停监听、管理字幕窗），双击就能启动
- [x] S21 边说边译：话还没说完就出中文草稿，看到法语之后约 1 秒就能看到中文（原来要等整句说完）
- [x] S22 字幕窗任意边角都能拖动改大小，文字跟着重新排满；字号直接在字幕窗上调（A− / A+、⌘ + 滚轮、双指捏合）
- [x] S23 README 改成从零开始的安装和使用教程（在全新的环境里照着做了一遍）
- [x] S24 README 按常见格式重写（macOS），新增 Windows 版 README
- [x] S25 采用 GPL-3.0 许可证；精简 README

</details>

## 许可证

[GPL-3.0](LICENSE)。可以自由使用、修改和再发布（包括商用），但发布修改后的版本时必须同样以 GPL-3.0 公开源代码。
