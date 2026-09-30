# 开发日志

每个阶段一条记录：遇到什么问题、怎么做的、改了哪些文件、怎么验证，以及值得记住的经验。
对应的代码改动可以用 `git log --grep "\[S0\]"` 这样按阶段号找到。

---

## S0 · 项目骨架与开发规范（2026-09-30）

**问题**：从空仓库开始，要先定好目录结构、依赖、配置方式和提交规范，后面每个阶段才能往里填。

**做法**
- 目录采用“平铺包”结构：`simul_interp/` 直接放在仓库根目录，不装包也能用 `python -m simul_interp` 运行，对初学者最直观。
- 配置分三层：代码默认值 → `config.toml` → 命令行参数。写错配置项名会直接报错，避免拼错了却悄悄不生效。
- API 密钥只放在 `.env` 或环境变量里，`config.toml`、`.env`、音频文件、会话记录都写进 `.gitignore`，防止把密钥和隐私内容推到 GitHub。
- 依赖用 `requirements.txt` 的平台标记（`; sys_platform == "darwin"`）区分 macOS 和 Windows，一份文件两边通用。
- 本机开发环境：用 `ml` conda 环境的 Python 建 `.venv --system-site-packages`，复用已装好的 torch / mlx-whisper，只把新依赖装进项目自己的环境，不污染 `ml`。

**主要文件**：`simul_interp/config.py`、`simul_interp/__main__.py`、`config.example.toml`、`.env.example`、`requirements.txt`、`.gitignore`、`CLAUDE.md`

**验证**：`python -m pytest` 通过配置相关的 4 个测试；`python -m simul_interp config` 能打印生效配置。

**开工前的测量**：完整版 whisper-large-v3（MLX）在 M5 上识别一次约 0.83～0.93 秒，基本与音频长短无关（Whisper 总是把输入补齐到 30 秒）；打开逐词时间戳会再多约 0.4 秒。所以后面的流式识别不用逐词时间戳，改用 Whisper 自带的分段时间戳。

**经验**：先做一次粗测再设计。这 1 秒的固定开销直接决定了“每隔多久识别一次”和整体能做到多低的延迟。

---

## S1 · macOS 内录系统声音（2026-09-30）

**问题**：要录到“电脑正在播放的声音”（会议软件、浏览器），同时不能影响用户用耳机或扬声器听原声，也不想让用户装虚拟声卡。

**做法**
- 自己写了一个约 250 行的 Swift 小程序 `native/macos/si_audio_tap.swift`，用 macOS 14.2+ 的 Core Audio Taps：
  1. `CATapDescription(monoGlobalTapButExcludeProcesses: [])`：把所有进程的声音混成一路单声道；
  2. `muteBehavior = .unmuted`：只复制不静音，原声照常播放（这就是不需要“多输出设备”的原因）；
  3. tap 不能直接读，要挂到一个**只包含这个 tap** 的私有聚合设备上，再注册 IOProc 回调取数据；
  4. 用 `AVAudioConverter` 从 48 kHz 重采样到 16 kHz，以 float32 写到 stdout；日志用 JSON 写到 stderr。
- 没有直接用 audiotee：它没有声明开源许可证，不能放进仓库；读它的代码学用法，自己实现更可控。
- 默认输出设备变化（插拔耳机、连蓝牙）时，小程序以退出码 3 退出，Python 端立刻重启它，重新挂到新设备上。
- Python 端 `simul_interp/audio/`：统一的 `AudioSource` 基类把任意长度的数据切成 512 点（32 ms）的小块并打上“音频流时间”；`record` 命令用来检查内录。

**踩坑：没权限时系统不报错，只给静音**
- 第一次测试收到的数据量完全正常（每秒 16000 个采样点），但全部是 0。
- 原因：macOS 把“系统录音”权限记在**负责进程**头上，也就是启动它的应用。当时是 Claude 应用在后台启动它，Claude 没有申请这项权限，所以既不弹窗也拿不到声音。
- 解决：小程序启动后用 `responsibility_spawnattrs_setdisclaim` 重新启动自己，声明“我自己负责”，同时把带 `NSAudioCaptureUsageDescription` 的 Info.plist 用 `-sectcreate` 嵌进可执行文件。之后系统以 si-audio-tap 的名义弹出权限框，从终端、VS Code 还是别的应用启动都一样。
- 排查方法：程序卡住时用 `sample <pid>` 看调用栈，发现停在创建 tap 的调用里、并且加载了 TCC（权限）框架，从而确认是在等权限弹窗。
- 重新编译会让 macOS 把它当成新程序、可能再要一次权限，所以 Python 端按**源码哈希**决定是否重新编译，而不是按修改时间。

**主要文件**：`native/macos/si_audio_tap.swift`、`native/macos/Info.plist`、`scripts/build_macos_tap.sh`、`simul_interp/audio/base.py`、`simul_interp/audio/macos_tap.py`、`simul_interp/audio/util.py`

**验证**
- 播放一段 6.9 秒的法语合成语音同时内录：音量 -14～-16 dBFS，播完后是真正的全零静音；
- 把录到的音频交给 Whisper，识别结果与原文一字不差；
- `python -m simul_interp record --seconds 9` 正常出音量条并保存 WAV；pytest 6 个测试通过。

**经验**：系统级功能先写一个最小可运行的探针，用“数据量对不对、是不是全零、内容对不对”三步验证，比一上来就接进完整流程好排查得多。
