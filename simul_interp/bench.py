"""基准测试：用测试音频按真实语速跑一遍，统计延迟和准确率。

延迟怎么算：先对整个文件做一次离线识别（带逐词时间戳）作为参照，知道每个词在音频里的结束时刻；
流式运行时记下每个词第一次显示、以及被确认的时刻，两者相减就是这个词的延迟。
"""

from __future__ import annotations

import difflib
import json
import statistics
from pathlib import Path

from .asr.streaming import norm
from .asr.types import AsrBackend, AsrUpdate
from .config import Config

GREY, RESET = "\033[90m", "\033[0m"
SENTENCE_END = (".", "?", "!", "…")


def word_error_rate(reference: list[str], hypothesis: list[str]) -> float:
    ref = [norm(w) for w in reference]
    hyp = [norm(w) for w in hypothesis]
    row = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        cur = [i] + [0] * len(hyp)
        for j in range(1, len(hyp) + 1):
            cur[j] = min(row[j] + 1, cur[j - 1] + 1, row[j - 1] + (ref[i - 1] != hyp[j - 1]))
        row = cur
    return row[-1] / max(1, len(ref))


def matched_indices(reference: list[str], hypothesis: list[str]) -> list[tuple[int, int]]:
    """把两串词对齐，返回 (参照下标, 结果下标) 的配对。"""
    matcher = difflib.SequenceMatcher(None, [norm(w) for w in reference], [norm(w) for w in hypothesis], autojunk=False)
    return [(a + k, b + k) for a, b, size in matcher.get_matching_blocks() for k in range(size)]


def reference_words(backend: AsrBackend, path: Path, model_id: str) -> list[tuple[float, float, str]]:
    """离线识别整个文件，得到带时间戳的参照词表；结果缓存在音频旁边的 .ref.json。"""
    from .audio.file_source import load_audio

    cache = path.with_suffix(".ref.json")
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("model") == model_id:
            return [tuple(w) for w in data["words"]]
    segments = backend.transcribe(load_audio(path), word_timestamps=True)
    words = [w for segment in segments for w in segment.words]
    cache.write_text(json.dumps({"model": model_id, "words": words}, ensure_ascii=False), encoding="utf-8")
    return words


def summarize(values: list[float]) -> str:
    if not values:
        return "无数据"
    values = sorted(values)
    p90 = values[min(len(values) - 1, int(0.9 * len(values)))]
    return f"中位数 {statistics.median(values):.2f}s，90% 在 {p90:.2f}s 以内，最慢 {values[-1]:.2f}s"


def bench_asr(cfg: Config, path: Path, speed: float = 1.0) -> int:
    from .asr import create_backend
    from .asr.runner import AsrRunner
    from .audio.file_source import FileAudioSource

    backend = create_backend(cfg)
    backend.load()
    print("计算参照（离线识别整个文件，带逐词时间戳）……")
    ref = reference_words(backend, path, cfg.asr.model)
    ref_text = [w for _, _, w in ref]
    truth_file = path.with_suffix(".txt")
    truth = truth_file.read_text(encoding="utf-8").split() if truth_file.exists() else None

    source = FileAudioSource(path, speed=speed)
    updates: list[AsrUpdate] = []

    def on_update(update: AsrUpdate) -> None:
        updates.append(update)
        at = update.wall - source.started_at
        mark = "■" if update.final else "·"
        print(f"[{at:6.2f}s] {mark} {update.committed} {GREY}{update.partial}{RESET}  ({update.compute_s:.2f}s)", flush=True)

    print(f"开始流式识别 {path.name}（{speed:g} 倍速）……\n")
    AsrRunner(cfg, backend, on_update).run(source)

    # 每个参照词：第一次显示的时刻、被确认的时刻
    committed: list[str] = []
    commit_wall: list[float] = []
    first_shown: dict[int, float] = {}
    partial_total = partial_wrong = 0  # 暂定文字里对不上参照的词：屏幕上“闪过的错字”
    for update in updates:
        new = update.committed.split()
        committed += new
        commit_wall += [update.wall] * len(new)
        shown = committed + update.partial.split()
        pairs = matched_indices(ref_text, shown)
        for ref_index, _ in pairs:
            first_shown.setdefault(ref_index, update.wall)
        matched = {h for _, h in pairs}
        partial_total += len(shown) - len(committed)
        partial_wrong += sum(1 for h in range(len(committed), len(shown)) if h not in matched)

    def delay(ref_index: int, wall: float) -> float:
        return wall - source.started_at - ref[ref_index][1] / speed

    commit_delay = {r: delay(r, commit_wall[h]) for r, h in matched_indices(ref_text, committed)}
    shown_delay = [delay(r, wall) for r, wall in first_shown.items()]
    sentence_delay = [d for r, d in commit_delay.items() if ref_text[r].endswith(SENTENCE_END)]
    passes = [u.compute_s for u in updates if not u.final]

    print("\n==== 结果 ====")
    print(f"识别次数：{len(passes)}，每次平均 {statistics.mean(passes):.2f}s，最慢 {max(passes):.2f}s" if passes else "识别次数：0")
    print(f"显示延迟（词说完 → 第一次出现在屏幕上）：{summarize(shown_delay)}")
    print(f"确认延迟（词说完 → 确认，可以送去翻译）：{summarize(list(commit_delay.values()))}")
    print(f"句末延迟（句子最后一个词说完 → 整句确认）：{summarize(sentence_delay)}")
    print(f"对上参照的词：{len(commit_delay)}/{len(ref_text)}")
    print(f"暂定文字里的错词：{partial_wrong}/{partial_total}（{partial_wrong / max(1, partial_total):.0%}）")
    if truth:
        print(f"词错误率 WER：流式 {word_error_rate(truth, committed):.1%}，离线整段 {word_error_rate(truth, ref_text):.1%}")
    return 0
