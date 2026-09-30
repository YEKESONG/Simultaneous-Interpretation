import os

import pytest

from simul_interp.config import load_config, load_dotenv


def test_empty_file_keeps_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.asr.language == "fr"
    assert cfg.asr.model == "mlx-community/whisper-large-v3-mlx"


def test_toml_overrides_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[vad]\nmin_silence_ms = 300\n[translate]\nglossary = { IA = "人工智能" }\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.vad.min_silence_ms == 300
    assert cfg.vad.threshold == 0.5  # 没写的项保持默认
    assert cfg.translate.glossary == {"IA": "人工智能"}


def test_unknown_key_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[vad]\nmin_silense_ms = 300\n", encoding="utf-8")  # 故意拼错
    with pytest.raises(ValueError, match="min_silense_ms"):
        load_config(path)


def test_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# 注释\nSI_TEST_A="from-file"\nSI_TEST_B=from-file\n', encoding="utf-8")
    monkeypatch.setenv("SI_TEST_B", "from-shell")
    monkeypatch.delenv("SI_TEST_A", raising=False)
    load_dotenv(env)
    assert os.environ["SI_TEST_A"] == "from-file"
    assert os.environ["SI_TEST_B"] == "from-shell"
