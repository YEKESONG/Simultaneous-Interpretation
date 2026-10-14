# Simultaneous-Interpretation · 法语 → 中文实时同传

[![tests](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml/badge.svg)](https://github.com/YEKESONG/Simultaneous-Interpretation/actions/workflows/tests.yml)

内录电脑正在播放的声音（视频会议、视频网站），实时识别法语并翻译成中文，在一个透明悬浮窗里成对显示原文和译文。
带一个控制面板：随时开始 / 暂停监听，也可以换成别的语言（英、西、德、意、葡语 → 任意目标语言）。
内录不会影响你听原声：戴耳机或者用扬声器都照常能听到。每次会话的原文、译文和一份录音都会保存下来，方便事后校对。

> 当前状态：开发中。每个阶段的进度见下方清单，详细过程见 [docs/DEVLOG.md](docs/DEVLOG.md)。

![悬浮字幕窗效果（示例文字）](docs/overlay_preview.png)

![控制面板](docs/panel_preview.png)

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
- **翻译**：边说边译。正在说的这句话每多出几个词，就把已经听到的部分先译一版草稿，屏幕上的中文紧跟着法语走；整句定下来（遇到句号，或者较长的分句）再出正式译文换掉草稿，会话记录里存的是正式译文。句子还没完全确认就先在后台翻好（投机翻译），确认时直接换上。说话人停下来想词时留下的半句话不急着定稿，等他接着说（最多等 1.5 秒）。
- **显示**：底部是高度固定的“当前句”区域，正在识别的原文和它的译文草稿都在里面实时更新；下一句开始时，这一对才平滑地滑进上方的历史。区域里怎么变都不会带动别的内容，窗口大小也固定。

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
- [x] S15 杂音时段不再输出 “Merci.”（整句只有它时向识别模型核实）
- [x] S16 没说完的半句话先等一等：说话人停下来想词时不再把一句话切成两半
- [x] S17 字幕窗可以往回翻看：滚轮或右侧的滑动条，回看时眼前的内容不动
- [x] S18 字幕窗可以收起成一个小条，点一下再展开
- [x] S19 流水线支持暂停 / 继续，以及运行中换语言（法、英、西、德、意、葡 → 任意目标语言）
- [x] S20 应用前端：控制面板（换语言、开始 / 暂停监听、管理字幕窗），双击就能启动
- [x] S21 边说边译：话还没说完就出中文草稿，看到法语之后约 1 秒就能看到中文（原来要等整句说完）
- [x] S22 字幕窗任意边角都能拖动改大小，文字跟着重新排满；字号直接在字幕窗上调（A− / A+、⌘ + 滚轮、双指捏合）

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

最简单的方式：在访达里双击 `启动同传.command`（Windows 双击 `启动同传.bat`），会出现控制面板和字幕窗。命令行：

```bash
.venv/bin/python -m simul_interp                                   # 同下一行：不写子命令就是 run
.venv/bin/python -m simul_interp run                               # 内录系统声音，控制面板 + 透明悬浮字幕窗
.venv/bin/python -m simul_interp run --paused                      # 启动后先不监听，等在控制面板上点“开始监听”
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

控制面板：
- 上面两个下拉框选语言：左边是说话人说的语言（法、英、西、德、意、葡语），右边是译成的语言。运行中随时可以换，从下一句话开始生效，不用重启，会话记录里会留一行说明。在这里选的只管这一次；想改每次启动的默认值，改 `config.toml` 的 `[asr] language` 和 `[translate] target_language`；
- 大按钮开始 / 暂停监听。暂停时不识别、不翻译，录音也跟着停（所以记录里的时间仍然对得上录音）；继续后记录里留一行“暂停后从这里继续”。想启动后先不监听：加 `--paused`，或者在配置里设 `[ui] autostart = false`；
- “显示字幕窗”和“收起 / 展开字幕窗”管字幕窗；最下面能看到这次的录音和记录的文件名，一键打开它们所在的文件夹；
- 点面板窗口的关闭按钮只是把面板藏起来，同传照常进行。要找回它：右键字幕窗，或者点菜单栏的“译”字图标，选“控制面板”。要结束程序，点面板上的“退出”。

悬浮字幕窗的操作：
- 底部是当前句区域：上面一行是正在说的原文（白色是已确认的，灰色是暂定的），下面一行是它到目前为止的译文草稿（比正式译文暗一点，会随着话往下说而更新，偶尔会改掉前面的字）；上方是翻好的历史，越旧越暗；
- 往回翻看：在字幕窗上滚动滚轮（触控板双指滑动），或者拖右边的滑动条。回看时新的句子照常加在下面，但眼前的内容不动；滚回最底下，或者点顶部的“回到最新”，就继续跟着最新的句子。字幕窗里留最近 500 对（`[ui] scrollback`），更早的在会话记录里；
- 收起：点右上角的“–”（或右键菜单、菜单栏图标里的“收起字幕窗”），窗口缩成左下角的一个小条，点小条再展开。收起的时候照常识别、翻译、记录，展开后期间的句子都在；
- 右上角的小圆点变绿，表示正在听到有人说话；
- 改大小：鼠标移到字幕窗上会显示窗口的边框，拖任意一条边或一个角都能改大小（右下角画着拉手）。字号不跟着窗口变，文字按新的宽度重新排：拉宽了每行放得下更多字，拉高了能看到更多历史；最小缩到刚好放得下当前句（两行原文、两行译文）；
- 调字号：鼠标移到字幕窗上，右上角会出现 A− / A+；也可以按住 ⌘（Windows 上是 Ctrl）滚动滚轮，或者在触控板上双指捏合。改完左上角会提示当前字号；
- 拖动窗口中间是移动窗口；
- 右键菜单：调字号（和上面一样）、背景深浅、鼠标穿透（点击直接落到下面的窗口）、清空、退出；
- 开启鼠标穿透后，用菜单栏的“译”字图标解锁或退出；
- 位置、大小、字号、背景透明度会自动保存。

测量工具：

```bash
.venv/bin/python scripts/make_test_audio.py                # 生成法语测试音频（macOS 合成语音）
.venv/bin/python -m simul_interp bench-asr samples/fr_meeting.wav   # 流式识别的延迟和准确率
.venv/bin/python -m simul_interp bench-translate           # 翻译服务的首字延迟
.venv/bin/python -m simul_interp bench-pipeline samples/fr_meeting.wav --mock-translate  # 端到端：看到法语之后多久看到中文
.venv/bin/python -m simul_interp --set asr.quantize_bits=0 bench-asr samples/fr_meeting.wav  # --set 临时改配置做对比
.venv/bin/python scripts/compare_models.py 录音.wav --configs 模型A:4,模型B:0  # 离线比较几种模型配置的准确率和速度
```

目前的延迟（M5 MacBook Air，Whisper turbo，法语合成语音，真实 DeepSeek API）：
- 一个法语词出现在屏幕上之后，**包含它的中文出现的中位数约 1.2～1.4 秒**（90% 在 2.1 秒以内）。没有边说边译时要等整句说完，中位数 2.1～2.3 秒，最慢的词要等 6～7 秒；
- 一句话说完后，屏幕上开始出现正式译文的中位数约 1.1～1.3 秒。

其中约 0.9 秒是翻译服务出第一个字的时间。各种配置的对比数据见 [docs/DEVLOG.md](docs/DEVLOG.md) 的 S9～S12 和 S21。

## 已知问题

- 声音在半句话处突然停止（比如暂停视频）时，最后半句可能被识别模型“补全”错。
- 没人对着麦克风说话、但一直有像人声的杂音（比如远处的交谈）时，语音检测会当成有人在说话，识别模型偶尔会编出几句短句。最常见的 “Merci.” 已经拦下（S15），别的句子还拦不住。
- 特别含糊、特别轻的一声 “Merci.”（识别模型自己的把握不到七成）会被当成杂音丢掉。
- 边说边译的草稿是照着还没确认的文字译的：识别模型一开始听错的词，草稿也会跟着译错，一两秒后随着识别结果改过来。想要“只显示确定的译文”，用 `translate.live_draft = false` 关掉（代价是要等整句说完才有中文）。
- 识别模型从半句话中间开始听（刚启动、暂停后继续）时，有时一段时间不加标点。切句靠标点，这时会一直攒到 25 个词才切成一段；有边说边译时中文不受影响，只是定稿晚。
- 翻译请求比较多：投机翻译约为“每句一次”的 3 倍，边说边译再多出约一倍（说话时大约每秒一次）。服务商有频率限制或在意费用时，用 `translate.live_draft = false`、`translate.speculative = false` 关掉，或者把 `translate.draft_step_words` 调大。
- 无风扇的 Mac 长时间满负荷识别会发热降频（实测约慢 40%），延迟会变大；实时识别默认用负载更低的 turbo 来减轻这个问题。
- Windows 部分还没有在真机上运行过（CI 只保证能安装、能导入、逻辑测试通过）。

## 怎么通过这个仓库学习

每完成一个小阶段就单独提交一次，提交历史本身就是一份搭建教程：

```bash
git log --reverse --stat
```

每个阶段遇到的问题、做法和验证方式记录在 [docs/DEVLOG.md](docs/DEVLOG.md)。
