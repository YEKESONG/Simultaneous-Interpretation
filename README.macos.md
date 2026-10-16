# Simultaneous-Interpretation（macOS）

[![tests](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml/badge.svg)](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)

实时同声传译：采集电脑正在播放的声音，在本地用 Whisper 识别法语，用大模型翻译成中文，显示在透明悬浮字幕窗中。

[Windows](README.md) · **macOS**

![字幕窗](docs/overlay_preview.png)

## 功能

- 采集电脑正在播放的声音（不影响正常收听），或者用麦克风外录现场的声音
- 本地流式语音识别（Whisper large-v3-turbo，MLX）
- 边说边译，译文比原文晚约 1 秒
- 悬浮字幕窗：回看历史、收起、调整大小和字号
- 控制面板：开始 / 暂停，切换源语言（法、英、西、德、意、葡、日语）和目标语言
- 自动保存会话记录和录音

## 要求

- Apple 芯片 Mac，macOS 14.2 及以上
- 内存 8 GB 以上（运行时约占 3.5 GB）
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

启动后出现控制面板和字幕窗。控制面板用于选择音源、开始 / 暂停监听和切换语言；关闭面板不会退出程序，可以从菜单栏的“译”图标重新打开。

音源在控制面板最上面选：“电脑播放的声音”是内录（网课、视频会议、视频），选某个麦克风就是外录（教室、会议室里现场的声音），运行中随时可以换。外录的效果取决于收音，离说话人越近越好，外接麦克风通常比电脑自带的好。第一次外录时 macOS 会询问能否使用麦克风，点“允许”。命令行里用 `--mic` 外录（可以写设备名的一部分，`devices` 命令列出所有麦克风）。

| 字幕窗 | 操作 |
|---|---|
| 移动 / 调整大小 | 拖动窗口中部 / 拖动边或角 |
| 字号 | 右上角 A− / A+，或 ⌘ + 滚轮 |
| 回看历史 | 滚轮，或拖动右侧滑动条 |
| 收起 | 右上角“–” |
| 背景、鼠标穿透等 | 右键菜单 |

会话记录和录音的存放位置见[输出文件](#输出文件)。

常用参数：`--mic` 用麦克风外录，`--paused` 启动后暂不监听，`--no-recording` 不录音，`--ui console` 只在终端输出。全部参数见 `.venv/bin/python -m simul_interp --help`。

## 输出文件

每次同传会在项目目录下生成两个同名文件，按开始的日期和时间命名：

| 内容 | 默认位置 |
|---|---|
| 会话记录（原文和译文） | `~/Simultaneous-Interpretation/transcripts/同传_<日期>_<时间>_译文.md` |
| 录音 | `~/Simultaneous-Interpretation/recordings/同传_<日期>_<时间>.wav` |

例如 `同传_2026-10-16_090000_译文.md` 和 `同传_2026-10-16_090000.wav`。控制面板上的“打开记录和录音所在的文件夹”可以直接打开这两个目录。

- 会话记录是 Markdown 文本。开头写着音源、识别语言、录音文件的路径和开始时间；正文每段是 `[mm:ss]` 加原文，下面一行是译文。`[mm:ss]` 是这段话在录音里的位置（通常比实际开始早 1 秒以内）。中途暂停、换语言、换音源时会插一行说明。记录边同传边写入，程序意外退出时已经翻好的部分也在。
- 录音是 16 kHz 单声道 WAV，约 115 MB/小时。内录时只录电脑有声音输出的时段。
- 用 `--file` 处理音频文件时不另存录音，记录命名为 `<文件名>_译文.md`。
- `record` 命令录的测试音频保存在 `recordings/system_<时间>.wav`（外录是 `mic_<时间>.wav`）。

修改保存位置：在 `config.toml` 中设置，可以写绝对路径，目录不存在时会自动创建：

```toml
[transcript]
dir = "/Users/你的用户名/Documents/同传记录"

[recording]
dir = "/Users/你的用户名/Documents/同传录音"
```

不想保存录音：设置 `[recording] enabled = false`，或启动时加 `--no-recording`。录制他人发言前请征得同意。

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
| 外录时提示“麦克风一直没有声音” | 系统设置 → 隐私与安全性 → 麦克风，允许启动程序的应用（如“终端”），然后重启程序 |
| 外录时冒出不成句的法语 | 麦克风会收进周围所有人的声音，别人说其他语言时会被硬识别成法语；尽量靠近说话人 |
| 依赖下载慢 | `pip install` 加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |

## 更新与卸载

```bash
git pull && .venv/bin/pip install -r requirements.txt
```

卸载时删除项目目录和 `~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo`。

工作原理见 [README](README.md#工作原理)。

## 许可证

[GPL-3.0](LICENSE)。可以自由使用、修改和再发布（包括商用），但发布修改后的版本时必须同样以 GPL-3.0 公开源代码。
