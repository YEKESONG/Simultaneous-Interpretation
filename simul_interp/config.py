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
    model: str = "mlx-community/whisper-large-v3-mlx"  # mlx 后端用的模型
    faster_whisper_model: str = "large-v3"  # faster-whisper 后端用的模型（同一个模型的另一种格式）
    device: str = "auto"  # faster-whisper：auto / cuda / cpu
    compute_type: str = "default"  # faster-whisper：显卡上可用 float16，纯 CPU 可用 int8
    allow_download: bool = False  # 本地没有模型时是否允许自动下载（large-v3 约 3 GB）
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
    glossary: dict[str, str] = field(default_factory=dict)  # 术语表：原文 → 译法
    # 请求里额外附带的参数。DeepSeek 默认开启“思考模式”，会先思考再输出，同传必须关掉
    extra_body: dict = field(default_factory=lambda: {"thinking": {"type": "disabled"}})


@dataclass
class UiConfig:
    mode: str = "overlay"  # overlay = 透明悬浮窗；console = 只在终端输出
    font_size: int = 26
    opacity: float = 0.6  # 悬浮窗背景不透明度
    max_lines: int = 3  # 悬浮窗里保留几句


@dataclass
class TranscriptConfig:
    enabled: bool = True
    dir: str = "transcripts"  # 相对路径以项目根目录为基准


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    transcript: TranscriptConfig = field(default_factory=TranscriptConfig)

    def api_key(self) -> str | None:
        if not self.translate.api_key_env:  # 本地 Ollama 之类不需要密钥的服务
            return "no-key-needed"
        return os.environ.get(self.translate.api_key_env) or None

    def transcript_dir(self) -> Path:
        return resolve_path(self.transcript.dir)

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
