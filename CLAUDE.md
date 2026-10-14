# 项目约定（给 Claude Code 和协作者看）

## 协作方式
- 和用户交流用简体中文。
- 每完成一个小阶段，单独提交一次并推送到 `origin main`，用户要靠提交历史学习项目的搭建过程。
- 提交信息：`<type>(<scope>): [S<阶段号>] 中文标题`，正文写清楚“问题 / 做法 / 验证”。type 用 feat、fix、perf、docs、test、chore。
- 每个阶段同步在 `docs/DEVLOG.md` 追加一条中文记录（问题、做法、主要文件、验证、经验），并在它开头的进度清单里加一行。README（Windows 版 `README.md`、macOS 版 `README.macos.md`）只写给使用者看，不放开发进度。
- 提交前跑一遍 `.venv/bin/python -m pytest`（Windows 上是 `.venv\Scripts\python -m pytest`）。

## 运行环境
- macOS 本机：`.venv` 基于 `ml` conda 环境（`--system-site-packages`），直接用 `.venv/bin/python`。
- 实时同传的识别模型默认用 `mlx-community/whisper-large-v3-turbo`（用户 2026-10-01 决定，依据见 DEVLOG S12）。用户的全局规定：**音频文件转写任务仍然只用完整版 `mlx-community/whisper-large-v3-mlx`**。不要下载其他语音识别模型，需要换模型先问用户。
- `bench-asr` 的计时参照（`samples/*.ref.json`）由完整版 large-v3 生成，比较不同模型时沿用同一份，不要删。
- 翻译 API 的密钥只放在 `.env`，永远不要提交，也不要打印出来。

## 在 Windows 上继续开发
Windows 部分是在 Mac 上写的，2026-10-14 在一台 Windows 11 笔记本（RTX 4060 Laptop）上按清单实测通过（DEVLOG S27～S30）。改了 Windows 相关代码后，按 DEVLOG S30 记录的 10 项在 Windows 上回归测试，结果和那里的数字对比。用户看的安装说明在 `README.md`（GitHub 首页展示的就是它；macOS 版在 `README.macos.md`），实测和它不符的地方要同步修正。

## 不要提交
`config.toml`、`.env`、任何音频文件、`transcripts/`、`recordings/`、`bin/`。用户的真实录音只能在本地测试用，不能进仓库；录音里的原话和具体内容也不能出现在 DEVLOG、提交说明、测试数据和代码注释里（统计数字可以写；要举例子就编一句结构相同的。S16 差点把会议里的原话当测试句子推上去）。

## 做真实内录测试之前
内录是系统级的：用 `afplay` 播放测试音频时，用户自己正在运行的同传程序也会录到、显示并翻译它（S14 踩过）。
播放之前先确认没有别的实例在跑（macOS：`pgrep -fl "simul_interp|si-audio-tap"`；Windows：`Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Select-Object CommandLine`），测试的录音和记录写到临时目录
（`--set recording.dir=… --set transcript.dir=…`），不要写进用户真实的录音文件夹。
