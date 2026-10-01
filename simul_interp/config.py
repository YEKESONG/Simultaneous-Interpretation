"""配置加载。

优先级从低到高：代码里的默认值 → config.toml → 命令行参数。
API 密钥不写进 config.toml，而是从环境变量读取；项目根目录的 .env 文件会在启动时自动载入环境变量。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class AudioConfig:
    source: str = "system"  # system = 电脑正在播放的声音；file = 用音频文件模拟（调试用）
    file: str = ""
    file_speed: float = 1.0  # 文件模拟时的播放倍速，1.0 = 实时


@dataclass
class VadConfig:
    threshold: float = 0.5  # 判定为“有人说话”的概率阈值
    min_silence_ms: int = 400  # 静音超过这么久，认为一句话说完
    speech_pad_ms: int = 200  # 句首往前多留一点音频，避免吞掉第一个音


@dataclass
class AsrConfig:
    backend: str = "auto"  # auto：macOS 用 mlx，Windows 用 faster-whisper
    # 实时同传用 turbo：解码器只有 4 层，比 large-v3 快 1.4~1.9 倍、芯片负载更低，
    # 在真实会议录音上识别效果不比 large-v3 差（DEVLOG S12，用户 2026-10-01 决定）。
    # 注意：音频文件转写任务按用户规定仍然只用完整版 mlx-community/whisper-large-v3-mlx
    model: str = "mlx-community/whisper-large-v3-turbo"  # mlx 后端用的模型
    faster_whisper_model: str = "large-v3-turbo"  # faster-whisper 后端用的模型（同一个模型的另一种格式）
    device: str = "auto"  # faster-whisper：auto / cuda / cpu
    compute_type: str = "default"  # faster-whisper：显卡上可用 float16，纯 CPU 可用 int8
    allow_download: bool = False  # 本地没有模型时是否允许自动下载（turbo 约 1.6 GB，large-v3 约 3 GB）
    # mlx：在内存里把权重量化成 8 位或 4 位（0 = 不量化），不下载任何东西。对 large-v3 能快 15~25%，
    # 但 4 位在真实会议录音上和原始精度差异较大（DEVLOG S12）；turbo 本身够快，默认不量化
    quantize_bits: int = 0
    language: str = "fr"
    step_s: float = 0.6  # 说话期间每隔多久重新识别一次
    max_buffer_s: float = 12.0  # 识别缓冲区的上限，超过就强制切分


@dataclass
class TranslateConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"  # DeepSeek 目前最快的模型（2026-09 核对官方文档）
    api_key_env: str = "DEEPSEEK_API_KEY"  # 从哪个环境变量读 API 密钥
    target_language: str = "简体中文"
    context_sentences: int = 3  # 翻译时附带的上文句数
    temperature: float = 0.2
    # 投机翻译：句子最后几个词还没被“两次一致”确认时就先送去翻译，确认后文字没变就直接用，
    # 中文能早出现约一次确认的时间；文字变了会重翻，所以偶尔多花一次 API 调用
    speculative: bool = True
    speculative_max_words: int = 12  # 凑成一句还要靠超过这么多个暂定词时不投机（暂定部分太长多半不可靠）
    glossary: dict[str, str] = field(default_factory=dict)  # 术语表：原文 → 译法
    # 请求里额外附带的参数。DeepSeek 默认开启“思考模式”，会先思考再输出，同传必须关掉
    extra_body: dict = field(default_factory=lambda: {"thinking": {"type": "disabled"}})


@dataclass
class UiConfig:
    mode: str = "overlay"  # overlay = 透明悬浮窗；console = 只在终端输出
    font_size: int = 22  # 译文（中文）字号，像素
    source_font_ratio: float = 0.82  # 原文字号 = 译文字号 × 这个比例（两者别差太多，方便对照）
    opacity: float = 0.6  # 悬浮窗背景不透明度
    max_lines: int = 3  # 悬浮窗里最多保留几对原文译文（放不下的会从上边滑出去）


@dataclass
class TranscriptConfig:
    enabled: bool = True
    dir: str = "transcripts"  # 相对路径以项目根目录为基准


@dataclass
class RecordingConfig:
    # 内录时把听到的声音同步存成 WAV（16 kHz 单声道，每小时约 115 MB），事后可以对照录音校对译文。
    # 用音频文件做输入时不录：文件本身就是录音
    enabled: bool = True
    dir: str = "recordings"  # 相对路径以项目根目录为基准


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    transcript: TranscriptConfig = field(default_factory=TranscriptConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)

    def api_key(self) -> str | None:
        if not self.translate.api_key_env:  # 本地 Ollama 之类不需要密钥的服务
            return "no-key-needed"
        return os.environ.get(self.translate.api_key_env) or None

    def transcript_dir(self) -> Path:
        return resolve_path(self.transcript.dir)

    def recording_dir(self) -> Path:
        return resolve_path(self.recording.dir)

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_path(p: str) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else ROOT / path


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，忽略空行和 # 注释；已存在的环境变量不覆盖。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def apply_overrides(cfg: Config, overrides: list[str]) -> None:
    """命令行上的 --set 段.项=值，按原来的类型转换，比如 asr.step_s=1.0、vad.min_silence_ms=300。"""
    for item in overrides:
        key, sep, raw = item.partition("=")
        section_name, _, name = key.strip().partition(".")
        section = getattr(cfg, section_name, None)
        if not sep or section is None or name not in {f.name for f in fields(section)}:
            raise ValueError(f"--set {item}：格式是 段.项=值，例如 asr.step_s=1.0")
        current = getattr(section, name)
        if isinstance(current, bool):
            value = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(current, (int, float, str)):
            value = type(current)(raw.strip())
        else:
            raise ValueError(f"--set 不支持修改 {key}，请写在 config.toml 里")
        setattr(section, name, value)


def load_config(path: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg = Config()
    if path is None:
        path = ROOT / "config.toml"
        if not path.exists():
            return cfg
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    for section_name, values in data.items():
        section = getattr(cfg, section_name, None)
        if section is None or not isinstance(values, dict):
            raise ValueError(f"{path}: 未知的配置段 [{section_name}]")
        known = {f.name for f in fields(section)}
        for key, value in values.items():
            if key not in known:
                raise ValueError(f"{path}: [{section_name}] 里没有 {key} 这个配置项")
            setattr(section, key, value)
    return cfg
