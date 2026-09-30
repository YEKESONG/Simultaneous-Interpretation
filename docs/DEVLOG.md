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
