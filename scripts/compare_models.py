#!/usr/bin/env python3
"""离线对比几种识别配置：以第一种配置的结果为参照，统计其他配置与它的词差异率和识别速度。

用法：
  python scripts/compare_models.py 音频1 音频2 ... \\
      --configs mlx-community/whisper-large-v3-mlx:0,mlx-community/whisper-large-v3-mlx:4,mlx-community/whisper-large-v3-turbo:0

每个配置写成 “模型:量化位数”（0 = 不量化）。只输出统计数字，不输出识别出的文字——测试音频可能是隐私录音。
注意：模型必须已经在本地缓存里，这个脚本不会下载。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from simul_interp.asr.mlx_backend import MlxWhisperBackend  # noqa: E402
from simul_interp.asr.streaming import norm  # noqa: E402
from simul_interp.audio.base import SAMPLE_RATE  # noqa: E402
from simul_interp.audio.file_source import load_audio  # noqa: E402
from simul_interp.clock import now  # noqa: E402


def edit_counts(reference: list[str], hypothesis: list[str]) -> tuple[int, int, int]:
    """把两串词对齐，数出替换、漏掉（参照有、结果没有）、多出（结果有、参照没有）各多少个词。"""
    ref = [norm(w) for w in reference]
    hyp = [norm(w) for w in hypothesis]
    rows, cols = len(ref) + 1, len(hyp) + 1
    dist = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        dist[i][0] = i
    for j in range(cols):
        dist[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            dist[i][j] = min(dist[i - 1][j] + 1, dist[i][j - 1] + 1, dist[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    subs = dels = ins = 0
    i, j = len(ref), len(hyp)
    while i > 0 or j > 0:  # 回溯编辑路径
        if i > 0 and j > 0 and dist[i][j] == dist[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            subs += ref[i - 1] != hyp[j - 1]
            i, j = i - 1, j - 1
        elif i > 0 and dist[i][j] == dist[i - 1][j] + 1:
            dels, i = dels + 1, i - 1
        else:
            ins, j = ins + 1, j - 1
    return subs, dels, ins


def transcribe_all(model: str, bits: int, audios: list) -> tuple[list[list[str]], float]:
    backend = MlxWhisperBackend(model, "fr", quantize_bits=bits)
    backend.load()
    texts, busy = [], 0.0
    for audio in audios:
        started = now()
        segments = backend.transcribe(audio)
        busy += now() - started
        texts.append(" ".join(s.text for s in segments).split())
    return texts, busy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--configs", required=True, help="逗号分隔的 模型:量化位数，第一个作参照")
    args = parser.parse_args()

    configs = []
    for item in args.configs.split(","):
        model, _, bits = item.strip().rpartition(":")
        configs.append((model, int(bits)))
    audios = [load_audio(path) for path in args.files]
    duration = sum(len(a) for a in audios) / SAMPLE_RATE
    print(f"{len(audios)} 段音频，共 {duration:.0f} 秒\n")

    reference = None
    for model, bits in configs:
        texts, busy = transcribe_all(model, bits, audios)
        name = f"{model.split('/')[-1]}（{'不量化' if bits == 0 else f'{bits} 位'}）"
        line = f"{name:42s} 识别耗时 {busy:5.1f} s（{duration / busy:4.1f} 倍实时）"
        if reference is None:
            reference = texts
            print(line + "  ← 参照")
            continue
        words = sum(len(r) for r in reference)
        per_file = [edit_counts(r, t) for r, t in zip(reference, texts)]
        subs, dels, ins = (sum(c[k] for c in per_file) for k in range(3))
        print(line + f"  与参照的词差异率 {(subs + dels + ins) / max(1, words):.1%}")
        print(f"{'':44s}其中替换 {subs}、漏掉 {dels}、多出 {ins}（参照共 {words} 词）")
        worst = max(range(len(per_file)), key=lambda k: sum(per_file[k]) / max(1, len(reference[k])))
        rate = sum(per_file[worst]) / max(1, len(reference[worst]))
        print(f"{'':44s}差异最大的一段：第 {worst + 1} 段（{args.files[worst].name}），{rate:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
