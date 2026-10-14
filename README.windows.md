# Simultaneous-Interpretation（Windows）

[![tests](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml/badge.svg)](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml)

实时同声传译工具。采集电脑正在播放的声音，在本地用 Whisper 识别法语，调用大语言模型翻译成中文，以透明悬浮字幕显示在屏幕上。

[macOS](README.md) · **Windows**

> [!WARNING]
> Windows 版本尚未在实体机上运行验证。GitHub Actions 会在 Windows 虚拟机上安装全部依赖并运行单元测试，但系统声音采集、GPU 识别和图形界面都没有在真实硬件上测试过。遇到问题请提交 [Issue](https://github.com/YEKESONG/Simultaneous-Interpretation/issues)，附上[运行日志](#提交问题)。

![字幕窗](docs/overlay_preview.png)

## 功能

- 采集系统声音（WASAPI loopback），不改变声音输出，耳机和扬声器均可正常收听
- 本地流式语音识别（Whisper large-v3-turbo，基于 faster-whisper，支持 NVIDIA GPU）
- 边说边译：原文出现约 1 秒后显示译文草稿，整句结束后替换为正式译文
- 悬浮字幕窗：始终置顶，支持回看历史、收起、调整大小和字号
- 控制面板：开始 / 暂停监听，运行中切换源语言（法、英、西、德、意、葡）和目标语言
- 自动保存会话记录（Markdown）和录音（WAV），记录中标注每段在录音中的时间点

## 系统要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 / 11（64 位） |
| 显卡 | 推荐 NVIDIA 显卡（CUDA 12）。只用 CPU 时识别难以跟上语速 |
| 磁盘空间 | 约 6 GB（Python 环境、识别模型约 1.6 GB、CUDA 库） |
| 网络 | 安装时下载约 3–5 GB；运行时识别在本地完成，翻译需要联网 |
| 翻译服务 | 兼容 OpenAI 接口的 API，默认 DeepSeek（需要 API 密钥，按量计费）。不配置时只显示原文 |

## 安装

以下命令在 PowerShell 中逐条执行（开始菜单搜索 “PowerShell”）。完整安装约需 30–60 分钟，主要是下载时间。

### 1. 安装 Python 3.12 和 Git

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

安装完成后关闭并重新打开 PowerShell，使 `PATH` 生效。也可以从 [python.org](https://www.python.org/downloads/) 和 [git-scm.com](https://git-scm.com/download/win) 下载安装程序（安装 Python 时勾选 “Add python.exe to PATH”）。

验证：

```powershell
py -3.12 --version
git --version
```

可选：安装 ffmpeg（只在用 mp3、m4a 等音频文件测试时需要）：

```powershell
winget install -e --id Gyan.FFmpeg
```

### 2. 获取源码

```powershell
cd $HOME
git clone https://github.com/YEKESONG/Simultaneous-Interpretation.git
cd Simultaneous-Interpretation
```

项目位于 `%USERPROFILE%\Simultaneous-Interpretation`。后续命令都在这个目录下执行；新开 PowerShell 窗口时先运行 `cd $HOME\Simultaneous-Interpretation`。

### 3. 创建虚拟环境并安装依赖

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

依赖安装在项目内的 `.venv` 目录中，不影响系统中的其他 Python 环境。

可选：运行测试确认环境正常（最后一行为 `N passed`，没有 `failed`）：

```powershell
.venv\Scripts\python -m pytest
```

### 4. 配置 GPU 加速（NVIDIA 显卡）

faster-whisper 需要 CUDA 12 版本的 cuBLAS 和 cuDNN 9：

1. 安装最新的 [NVIDIA 显卡驱动](https://www.nvidia.com/Download/index.aspx)；
2. 安装 [CUDA Toolkit 12](https://developer.nvidia.com/cuda-downloads) 和 [cuDNN 9](https://developer.nvidia.com/cudnn)（NVIDIA 官方安装程序）。也可以下载 [Purfview 整理的 DLL 压缩包](https://github.com/Purfview/whisper-standalone-win/releases/tag/libs)，解压后放到 `PATH` 中的某个目录。

详细说明见 [faster-whisper 的 GPU 文档](https://github.com/SYSTRAN/faster-whisper#gpu)。

验证（输出大于 0 表示能找到 GPU）：

```powershell
.venv\Scripts\python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

没有 NVIDIA 显卡时跳过此步，并在配置文件中改用 CPU（见[配置](#配置)）：

```toml
[asr]
device = "cpu"
compute_type = "int8"
```

### 5. 下载语音识别模型

```powershell
.venv\Scripts\hf download mobiuslabsgmbh/faster-whisper-large-v3-turbo
```

模型约 1.6 GB，保存在 `%USERPROFILE%\.cache\huggingface\`。输出中的 `unauthenticated requests` 警告可以忽略。

中国大陆网络可以使用镜像：

```powershell
$env:HF_ENDPOINT = "https://hf-mirror.com"
.venv\Scripts\hf download mobiuslabsgmbh/faster-whisper-large-v3-turbo
```

也可以不手动下载，在配置文件中设置 `asr.allow_download = true`，首次启动时自动下载。

### 6. 配置翻译 API 密钥

1. 在 [DeepSeek 开放平台](https://platform.deepseek.com) 注册并充值；
2. 在 API keys 页面创建密钥（以 `sk-` 开头，只显示一次）；
3. 把密钥写入项目根目录的 `.env` 文件：

```powershell
copy .env.example .env
notepad .env
```

把 `DEEPSEEK_API_KEY=` 改为 `DEEPSEEK_API_KEY=sk-xxxxxxxx`（不加引号和空格），保存。

`.env` 已列入 `.gitignore`，不会被提交。跳过此步程序也能运行，但只显示原文。使用其他翻译服务见[更换翻译服务](#更换翻译服务)。

### 7. 检查系统声音采集

在浏览器中播放任意有声音的视频，然后运行：

```powershell
.venv\Scripts\python -m simul_interp record --seconds 10
```

Windows 上采集系统声音不需要授权。正常时终端每秒输出一行音量（dBFS，越接近 0 越响），录音保存在 `recordings\`。停止播放后音量应降到 -120 dBFS 左右，计时继续。

### 8. 启动

```powershell
.venv\Scripts\python -m simul_interp
```

首次启动加载模型需要一段时间，之后出现控制面板和字幕窗。

以后也可以在文件资源管理器中双击项目目录下的 `启动同传.bat` 启动。启动时会打开一个显示运行日志的命令行窗口，关闭这个窗口会结束程序。

## 使用

### 控制面板

- 语言：左侧选择源语言（法、英、西、德、意、葡），右侧选择目标语言。运行中切换，从下一句生效，会话记录中会插入一行说明。这里的选择只对本次运行有效，默认值在[配置文件](#配置)中设置。
- 开始 / 暂停监听：暂停期间不识别、不翻译、不录音；继续后记录中插入“暂停后从这里继续”。
- 字幕窗：显示 / 隐藏，收起 / 展开。
- 文件：显示本次的记录和录音文件名，可以打开所在文件夹。
- 关闭面板窗口不会退出程序，可以从字幕窗右键菜单或任务栏通知区域的“译”图标重新打开（图标可能收在 “^” 里）。退出请点击“退出”。

### 字幕窗

| 操作 | 方法 |
|---|---|
| 移动 | 拖动窗口中部 |
| 调整大小 | 拖动任意边或角。鼠标悬停时显示边框和右下角拉手 |
| 调整字号 | 右上角 A− / A+（鼠标悬停时显示）；Ctrl + 滚轮 |
| 回看历史 | 滚轮，或拖动右侧滑动条；点击顶部“回到最新”返回 |
| 收起 | 点击右上角“–”收起为小条，点击小条展开 |
| 背景深浅、鼠标穿透、清空、重新连接音频设备 | 右键菜单 |

字幕窗底部是当前句：原文中白色为已确认、灰色为待确认；下方是实时更新的译文草稿（颜色较暗），整句结束后替换为正式译文并移入上方历史。右上角圆点变绿表示检测到语音。开启鼠标穿透后，从通知区域的“译”图标关闭。窗口位置、大小、字号和背景透明度会自动保存。

切换输出设备（插拔耳机、连接蓝牙音箱）后，需要右键字幕窗，选择“重新连接音频设备”。

### 会话记录与录音

每次运行生成两个同名文件：

| 文件 | 内容 |
|---|---|
| `transcripts\同传_<日期>_<时间>_译文.md` | 原文和正式译文。每段前的 `[mm:ss]` 是该段在录音中的位置，通常比实际开始早 1 秒以内 |
| `recordings\同传_<日期>_<时间>.wav` | 系统声音录音，16 kHz 单声道，约 115 MB/小时 |

保存位置可以在[配置文件](#配置)中修改。录制他人发言前请征得对方同意。

### 命令行

在项目目录下执行：

| 命令 | 说明 |
|---|---|
| `.venv\Scripts\python -m simul_interp` | 启动（等同于 `run`） |
| `.venv\Scripts\python -m simul_interp run --paused` | 启动后暂不监听 |
| `.venv\Scripts\python -m simul_interp run --no-recording` | 本次不保存录音 |
| `.venv\Scripts\python -m simul_interp run --no-translate` | 只识别，不翻译 |
| `.venv\Scripts\python -m simul_interp run --ui console` | 不显示字幕窗，在终端中输出 |
| `.venv\Scripts\python -m simul_interp run --file <音频文件>` | 用音频文件代替系统声音（非 WAV 格式需要 ffmpeg） |
| `.venv\Scripts\python -m simul_interp record --seconds 10` | 录制 10 秒系统声音，检查采集是否正常 |
| `.venv\Scripts\python -m simul_interp config` | 显示当前生效的配置 |
| `.venv\Scripts\python -m simul_interp --set <段>.<项>=<值>` | 临时覆盖一项配置，例如 `--set asr.language=en` |
| `.venv\Scripts\python -m simul_interp --help` | 查看全部命令和参数 |

运行中按 Ctrl + C 退出。

## 配置

不修改配置也可以使用。需要修改时，复制示例配置并编辑：

```powershell
copy config.example.toml config.toml
notepad config.toml
```

每一项在 `config.example.toml` 中都有注释。保存后下次启动生效；写错项名或格式时，启动会报错并指出位置；删除 `config.toml` 即恢复默认。

常用配置项：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `asr.language` | `"fr"` | 源语言：`fr`、`en`、`es`、`de`、`it`、`pt` |
| `asr.device` | `"auto"` | 识别设备：`auto`、`cuda`、`cpu` |
| `asr.compute_type` | `"default"` | 计算精度：GPU 可用 `float16`，CPU 建议 `int8` |
| `asr.allow_download` | `false` | 本地没有模型时是否自动下载 |
| `translate.target_language` | `"简体中文"` | 目标语言 |
| `translate.glossary` | 无 | 术语表，例如 `{ "TP" = "实验课" }` |
| `translate.live_draft` | `true` | 边说边译。设为 `false` 时只显示正式译文（延迟更高，但不会出现先错后改） |
| `translate.speculative` | `true` | 句子确认前提前翻译。设为 `false` 可减少翻译请求 |
| `transcript.dir` | `"transcripts"` | 会话记录目录，可以写绝对路径 |
| `recording.dir` | `"recordings"` | 录音目录，可以写绝对路径 |
| `recording.enabled` | `true` | 是否保存录音 |
| `ui.autostart` | `true` | 启动后立即开始监听 |

在配置文件中写 Windows 路径时，使用正斜杠或双反斜杠，例如 `dir = "D:/同传记录"`。

### 更换翻译服务

翻译模块兼容 OpenAI Chat Completions 接口，在 `config.toml` 的 `[translate]` 中修改：

```toml
base_url = "https://api.example.com/v1"
model = "model-name"
api_key_env = "MY_API_KEY"   # 在 .env 中写 MY_API_KEY=...
extra_body = {}              # 默认值用于关闭 DeepSeek 的思考模式，其他服务改为空
```

离线使用可以在本机运行 [Ollama](https://ollama.com)，配置方法见 `config.example.toml` 中的示例（不需要密钥，对硬件要求较高）。

## 常见问题

### `py` 或 `git` 不是内部或外部命令

安装后没有重新打开 PowerShell，或安装 Python 时没有勾选 “Add python.exe to PATH”。重新打开 PowerShell 后再试；仍然不行时重新安装。

### 启动时提示“本地没有找到模型”

完成[安装第 5 步](#5-下载语音识别模型)，或在配置中设置 `asr.allow_download = true`。

### 报错找不到 `cublas64_12.dll` 或 `cudnn*64_9.dll`

CUDA 库没有安装，或所在目录不在 `PATH` 中，见[安装第 4 步](#4-配置-gpu-加速nvidia-显卡)。没有 NVIDIA 显卡时把 `asr.device` 设为 `"cpu"`。

### 识别明显跟不上语速

确认程序在使用 GPU（安装第 4 步的验证命令输出大于 0）。只用 CPU 时，大模型很难做到实时。

### 下载速度慢

依赖可以使用镜像源：

```powershell
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

模型下载见[安装第 5 步](#5-下载语音识别模型)中的镜像。

### 字幕窗没有文字

播放声音时观察右上角圆点是否变绿。如果不变绿，先运行[安装第 7 步](#7-检查系统声音采集)的检查命令；切换过输出设备时，右键字幕窗选择“重新连接音频设备”。字幕窗左上角的黄色文字会显示当前的错误信息。

### 只显示原文，没有译文

检查 `.env` 中的密钥和 DeepSeek 账户余额，修改后重新启动。启动日志会显示“没有设置翻译 API 密钥”或“翻译 API 密钥无效”等提示。

### 找不到控制面板

点击任务栏通知区域的“译”图标（可能收在 “^” 里），或右键字幕窗，选择“控制面板”。

### 译文草稿先显示错误内容，随后又改正

草稿根据尚未确认的识别结果翻译，识别结果修正后草稿随之更新，通常 1–2 秒内稳定。不需要草稿时设置 `translate.live_draft = false`。

### 翻译费用

说话期间大约每秒发送一次翻译请求。需要降低费用时，把 `translate.live_draft` 和 `translate.speculative` 设为 `false`，代价是译文出现得更晚。

### 提交问题

提交 Issue 时请附上 Windows 版本、显卡型号，以及带调试日志的运行输出：

```powershell
.venv\Scripts\python -m simul_interp -v run
```

## 更新

```powershell
cd $HOME\Simultaneous-Interpretation
git pull
.venv\Scripts\pip install -r requirements.txt
```

`.env`、`config.toml`、会话记录和录音不会被覆盖。

## 卸载

先备份 `transcripts\` 和 `recordings\` 中需要保留的文件，然后：

```powershell
cd $HOME
Remove-Item -Recurse -Force Simultaneous-Interpretation
Remove-Item -Recurse -Force "$HOME\.cache\huggingface\hub\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
```

Python、Git 和 CUDA 可以在“设置 → 应用”中卸载。

## 已知问题

- Windows 版本尚未在实体机上验证。
- 切换输出设备后不会自动重新连接，需要手动选择“重新连接音频设备”。
- 声音在半句处突然停止（例如暂停视频）时，最后半句可能被识别模型错误补全。
- 持续的类人声背景噪音中，识别模型偶尔生成不存在的短句；单独的 “Merci.” 已经过滤。
- 从半句中间开始识别（刚启动、暂停后继续）时，识别结果有时一段时间不带标点，正式译文会延后，且单段较长。

工作原理、性能数据和开发说明见 [macOS 版 README](README.md#工作原理)。
