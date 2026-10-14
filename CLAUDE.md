# 项目约定（给 Claude Code 和协作者看）

## 协作方式
- 和用户交流用简体中文。
- 每完成一个小阶段，单独提交一次并推送到 `origin main`，用户要靠提交历史学习项目的搭建过程。
- 提交信息：`<type>(<scope>): [S<阶段号>] 中文标题`，正文写清楚“问题 / 做法 / 验证”。type 用 feat、fix、perf、docs、test、chore。
- 每个阶段同步在 `docs/DEVLOG.md` 追加一条中文记录（问题、做法、主要文件、验证、经验），并更新 README 的进度清单。
- 提交前跑一遍 `.venv/bin/python -m pytest`。

## 运行环境
- macOS 本机：`.venv` 基于 `ml` conda 环境（`--system-site-packages`），直接用 `.venv/bin/python`。
- 实时同传的识别模型默认用 `mlx-community/whisper-large-v3-turbo`（用户 2026-10-01 决定，依据见 DEVLOG S12）。用户的全局规定：**音频文件转写任务仍然只用完整版 `mlx-community/whisper-large-v3-mlx`**。不要下载其他语音识别模型，需要换模型先问用户。
- `bench-asr` 的计时参照（`samples/*.ref.json`）由完整版 large-v3 生成，比较不同模型时沿用同一份，不要删。
- 翻译 API 的密钥只放在 `.env`，永远不要提交，也不要打印出来。

## 在 Windows 上继续开发（交接说明）
Windows 部分（`audio/windows_loopback.py`、`asr/faster_whisper_backend.py`）是在 Mac 上写的，**还没有在 Windows 真机上运行过**。面向用户的 Windows 安装和使用说明在 `README.windows.md`，在 Windows 上验证过哪一步，就同步修正它。CI 只保证能安装、能导入、逻辑测试通过。在 Windows 上建议按这个顺序验证：
1. 建环境：`python -m venv .venv`，`.venv\Scripts\pip install -r requirements.txt`；复制 `config.example.toml` 为 `config.toml`、`.env.example` 为 `.env`。
2. 内录：`python -m simul_interp record --seconds 10`，同时用浏览器播放声音，看音量条是否正常；停止播放后应该是静音（-120 dBFS），而不是卡住——WASAPI loopback 没声音时不回调，靠 `GapFiller` 补静音。
3. 识别模型：faster-whisper 的 large-v3-turbo 约 1.6 GB（和 Mac 上一样用 turbo），**下载前先问用户**，同意后在 `config.toml` 里设 `[asr] allow_download = true`。有 NVIDIA 显卡时确认用上了 CUDA（没有显卡的话很难做到实时，要和用户商量）。
4. 延迟：把 Mac 上用 `scripts/make_test_audio.py` 生成的 `samples/` 拷过来（合成语音不能进公开仓库），跑 `python -m simul_interp bench-asr samples/fr_meeting.wav`，和 DEVLOG 里 Mac 的数据对比。
5. 悬浮窗：`python -m simul_interp run --mock-translate --file samples/fr_meeting.wav`，确认透明、置顶、不抢焦点，拖动和右键菜单可用，全屏视频上能看到；插拔耳机后用“重新连接音频设备”。
   控制面板（S20）也没有在 Windows 上跑过：双击 `启动同传.bat` 能不能启动，面板上的开始 / 暂停、换语言、收起 / 展开字幕窗、“退出”是否正常；字幕窗的滚轮回看和右侧滑动条、收起成小条后点击展开。
6. 杂音：S15 加的 `FasterWhisperBackend.word_probability`（核实单独一句的 “Merci.” 是不是对着杂音编的）同样没在真机跑过。播放一段没人说话、只有键盘声或提示音的音频，确认不会一句接一句地出 “Merci.”；再播放一句真的 “Merci.”，应该照常出来。不对的话先看这个方法返回的概率：真的应该在 0.9 上下，杂音上多半不到 0.5。
每修一处照常单独提交，并在 DEVLOG 记录 Windows 上的实测结果。

## 不要提交
`config.toml`、`.env`、任何音频文件、`transcripts/`、`recordings/`、`bin/`。用户的真实录音只能在本地测试用，不能进仓库；录音里的原话和具体内容也不能出现在 DEVLOG、提交说明、测试数据和代码注释里（统计数字可以写；要举例子就编一句结构相同的。S16 差点把会议里的原话当测试句子推上去）。

## 做真实内录测试之前
内录是系统级的：用 `afplay` 播放测试音频时，用户自己正在运行的同传程序也会录到、显示并翻译它（S14 踩过）。
播放之前先确认没有别的实例在跑（`pgrep -fl "simul_interp|si-audio-tap"`），测试的录音和记录写到临时目录
（`--set recording.dir=… --set transcript.dir=…`），不要写进用户真实的录音文件夹。
