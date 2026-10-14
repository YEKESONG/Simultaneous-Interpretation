# Windows 实机测试交接

写给在 Windows 电脑上接手的 Claude Code 和用户。读完这一份就可以开始，不需要通读 `docs/DEVLOG.md`（很长；需要时只看文中点名的几节）。

> **状态**：2026-10-14 在 Windows 11 笔记本（i7-13700H、RTX 4060 Laptop）上把下面 10 项全部跑通，修了 3 处（S27～S29），机器配置、每一项的结果和数字见 DEVLOG S30。以后改了 Windows 相关代码，照这份清单回归测试即可。

## 目标

Windows 版的代码（DEVLOG S8 起）是在 Mac 上写的，从没在 Windows 实机上运行过。GitHub Actions（windows-latest）只验证了依赖能安装、模块能导入、单元测试能通过。

这次在实机上按下面的清单逐项跑通，发现问题就修，每修一处单独提交。全部通过后：

- 去掉 `README.windows.md` 顶部“尚未在实体机上验证”的警告；
- 修改 `CLAUDE.md` 里“还没有在 Windows 真机上运行过”的说法。

## 用户的偏好

这些写在 Mac 上的全局设置里，Windows 上没有：

- 对话一律用简体中文。仓库里的文件沿用已有的语言习惯，提交格式见 `CLAUDE.md`。
- 下载语音识别模型之前先问用户（faster-whisper 的 large-v3-turbo 约 1.6 GB），不要换用别的识别模型。
- 用户希望少花 token：先照清单跑，不要通读代码；出了问题再读相关文件。

## 开始前要准备的

1. 按 `README.windows.md` 第 1～2 步装好 Python 3.12 和 Git，克隆仓库，建 `.venv` 并安装依赖。Claude 可以代为执行这些命令。
2. 从 Mac 拷过来两样东西（都不在 git 里）：
   - `samples/` 整个文件夹：合成的法语测试音频 `fr_meeting.wav`、`fr_continuous.wav`，对应的文本 `*.txt`，以及识别计时参照 `*.ref.json`。Windows 上没有 macOS 的 `say`，没法重新生成。
   - `.env`：翻译 API 密钥。不要把密钥贴进对话。
3. 有 NVIDIA 显卡时，装好驱动，再用 pip 把 CUDA 12 的 cuBLAS 和 cuDNN 9 装进 `.venv`（`README.windows.md` 第 3 步，S27）。
4. 推送需要这台电脑登录 GitHub：Git for Windows 第一次 `git push` 时会弹出登录窗口。

## 相关代码

| 文件 | 内容 | 要注意的 |
|---|---|---|
| `simul_interp/audio/windows_loopback.py` | WASAPI loopback（PyAudioWPatch）采集默认输出设备；48 kHz 立体声混成单声道，用 soxr 重采样到 16 kHz；`GapFiller` 在没有声音时按时间补静音 | 设备选择、采样格式、无声时是否卡住 |
| `simul_interp/asr/faster_whisper_backend.py` | faster-whisper 识别；`word_probability` 用于 S15 的 “Merci.” 核实 | CUDA 库能否加载、速度 |
| `simul_interp/ui/overlay.py` | 字幕窗。Windows 上额外加 `Qt.Tool` 标志；`float_over_fullscreen`、`make_accessory_app`、`activate_app` 在 Windows 上什么都不做 | 透明、置顶、不抢焦点、高 DPI |
| `simul_interp/ui/app.py`、`panel.py` | 控制面板、托盘图标、程序的启动和退出 | 托盘图标位置、退出 |
| `simul_interp/__main__.py` | Windows 上把 stdout / stderr 改成 UTF-8 | 控制台乱码 |
| `启动同传.bat` | 双击启动，先 `chcp 65001` | 中文输出、出错时窗口是否停住 |

## 测试清单

命令都在项目目录的 PowerShell 里执行。

**1. 单元测试**

```powershell
.venv\Scripts\python -m pytest
```

预期全部通过（S29 之后是 128 个）。

**2. GPU**

```powershell
.venv\Scripts\python -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

预期大于 0。注意这只说明驱动能看到显卡，cuBLAS、cuDNN 能不能加载要到第 5 步真正识别时才知道（S27）。没有 NVIDIA 显卡时，在 `config.toml` 的 `[asr]` 里设 `device = "cpu"`、`compute_type = "int8"`，并告诉用户 CPU 上大概率做不到实时，商量要不要继续。

**3. 识别模型**（先问用户）

```powershell
.venv\Scripts\hf download mobiuslabsgmbh/faster-whisper-large-v3-turbo
```

之后程序以 `local_files_only=True` 加载，启动时不应再提示“本地没有找到模型”。

**4. 系统声音采集**

测试前确认没有别的同传实例在跑（命令见 `CLAUDE.md`“做真实内录测试之前”）。播放声音时运行：

```powershell
.venv\Scripts\python -m simul_interp record --seconds 10 --out $env:TEMP\tap_test.wav
```

预期：每秒一行音量，有声音时大约 -10～-40 dBFS；停止播放后降到 -120 dBFS 左右，并且计时继续走（`GapFiller` 在补静音；如果卡住，问题在这里）。播放生成的 WAV，音调和速度要正常，否则是重采样或声道处理有问题。

**5. 识别的延迟和准确率**

```powershell
.venv\Scripts\python -m simul_interp bench-asr samples\fr_meeting.wav
.venv\Scripts\python -m simul_interp bench-asr samples\fr_continuous.wav
```

Mac 上的参考值（M5 MacBook Air，turbo，MLX，DEVLOG S12）：句末延迟中位数 0.89 / 0.91 秒，WER 6.2% / 5.6%。明显更慢时先查 GPU 有没有用上、`compute_type`。

**6. 端到端**（真实翻译接口）

```powershell
.venv\Scripts\python -m simul_interp bench-pipeline samples\fr_meeting.wav
```

Mac 上的参考值（DEVLOG S21）：法语出现到包含它的中文出现，中位数 1.2 秒、90% 在 2.1 秒以内；一句话说完到正式译文开始出现，中位数 1.1～1.3 秒。

**7. 界面**（模拟翻译，不联网）

```powershell
.venv\Scripts\python -m simul_interp run --mock-translate --file samples\fr_meeting.wav
```

需要用户在屏幕上操作并反馈，逐项确认：

- 字幕窗透明、始终置顶；点击它时不会抢走其他窗口的焦点；浏览器视频全屏时也能看到；
- 拖动窗口中部能移动；拖边、拖角能改大小，鼠标停在边上时光标会变；
- 右上角 A− / A+ 和 Ctrl + 滚轮能调字号；滚轮和右侧滑动条能回看历史；
- “–”收起成小条，点击小条展开，拖动小条能移动；
- 右键菜单各项可用；开启鼠标穿透后能从托盘图标关掉；
- 控制面板：开始 / 暂停、换语言、显示 / 隐藏字幕窗、打开文件夹；关闭面板后能从托盘找回；“退出”能退出；
- 托盘“译”图标出现在任务栏通知区域（可能收在 “^” 里）；
- 显示缩放 125% / 150% 时，文字、按钮的大小和位置正常；
- 文件放完约 5 秒后程序自动退出。

**8. 双击 `启动同传.bat`**

能启动；日志里的中文不乱码；出错时窗口停住、能看到错误。

**9. 真实内录全流程**

浏览器播放一段法语视频，输出写到临时目录：

```powershell
.venv\Scripts\python -m simul_interp --set transcript.dir=$env:TEMP\si_test --set recording.dir=$env:TEMP\si_test
```

预期：字幕正常；退出后记录和录音成对生成，记录里每段的 `[mm:ss]` 和录音里的位置对得上。中途插拔耳机，右键字幕窗选“重新连接音频设备”后恢复。

**10. 杂音里的 “Merci.”**（S15）

播放一段只有键盘声、提示音的音频，不应该一句接一句地出现 “Merci.”；播放一句真的 “Merci.”，应该照常出来。不对时打印 `FasterWhisperBackend.word_probability` 的返回值：真的约 0.9，杂音多半低于 0.5；判断的阈值是 `asr/streaming.py` 里的 `MIN_FILLER_PROB = 0.7`。

## 已知的可疑点

读代码时注意到、但没有验证过的：

- `word_probability` 在 faster-whisper 上要完整再解码一遍，比 Mac 上慢，单独一句 “Merci.” 时多出的延迟可能更明显。
- 切换输出设备不会自动跟随（PortAudio 只在初始化时枚举一次设备），只能手动重新连接。要自动跟随需要用 Windows 的 `IMMNotificationClient`，做之前先问用户。
- 控制台编码：`__main__` 已经把输出改成 UTF-8；仍然乱码或报 `UnicodeEncodeError` 时，先设 `$env:PYTHONUTF8 = "1"` 再运行（CI 就是这样做的，DEVLOG S8、S13c）。
- 字幕窗用了半透明窗口和文字阴影（`QGraphicsDropShadowEffect`），在某些显卡或远程桌面下可能变慢，或者背景变成黑色。

## 记录和提交

- 每修一处单独提交，格式见 `CLAUDE.md`，阶段号从 S27 开始。
- 在 DEVLOG 追加“Windows 实测”记录：写清楚机器配置（CPU、显卡、Windows 版本）、清单每一项的结果和数字。不要写真实录音的内容。
- 实测和 `README.windows.md` 不符的地方同步修正。
- 推送：`git push origin main`。查看 CI：GitHub 仓库的 Actions 页面。
