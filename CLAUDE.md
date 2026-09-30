# 项目约定（给 Claude Code 和协作者看）

## 协作方式
- 和用户交流用简体中文。
- 每完成一个小阶段，单独提交一次并推送到 `origin main`，用户要靠提交历史学习项目的搭建过程。
- 提交信息：`<type>(<scope>): [S<阶段号>] 中文标题`，正文写清楚“问题 / 做法 / 验证”。type 用 feat、fix、perf、docs、test、chore。
- 每个阶段同步在 `docs/DEVLOG.md` 追加一条中文记录（问题、做法、主要文件、验证、经验），并更新 README 的进度清单。
- 提交前跑一遍 `.venv/bin/python -m pytest`。

## 运行环境
- macOS 本机：`.venv` 基于 `ml` conda 环境（`--system-site-packages`），直接用 `.venv/bin/python`。
- 语音识别模型只用 `mlx-community/whisper-large-v3-mlx`（用户规定），不要下载其他语音识别模型；需要换模型先问用户。
- 翻译 API 的密钥只放在 `.env`，永远不要提交，也不要打印出来。

## 不要提交
`config.toml`、`.env`、任何音频文件、`transcripts/`、`bin/`。用户的真实录音只能在本地测试用，不能进仓库，也不能出现在 DEVLOG 里。
