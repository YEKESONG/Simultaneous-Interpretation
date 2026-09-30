from simul_interp.transcript import TranscriptWriter
from simul_interp.translate import TranslationUnit


def test_transcript_file_naming_and_content(tmp_path):
    writer = TranscriptWriter(tmp_path, "文件 demo.wav", {"识别模型": "whisper"}, stem="demo")
    assert writer.path.name == "demo_译文.md"
    unit = TranslationUnit(0, "Bonjour à tous.", ready_at=writer._started + 65, translation="大家好。", done_at=1.0)
    writer.add(unit)
    writer.add(TranslationUnit(1, "Merci.", ready_at=writer._started + 70, error="HTTP 500", done_at=2.0))
    writer.close()
    text = writer.path.read_text(encoding="utf-8")
    assert "# 同传记录：文件 demo.wav" in text and "- 识别模型：whisper" in text
    assert "**[01:05]** Bonjour à tous.\n\n> 大家好。" in text
    assert "（翻译失败：HTTP 500）" in text
    assert "共 2 句" in text

    again = TranscriptWriter(tmp_path, "文件 demo.wav", {}, stem="demo")
    assert again.path != writer.path  # 同名文件已存在时不覆盖


def test_live_session_is_named_by_time(tmp_path):
    writer = TranscriptWriter(tmp_path, "macOS 系统声音", {})
    assert writer.path.name.startswith("同传_") and writer.path.name.endswith("_译文.md")
