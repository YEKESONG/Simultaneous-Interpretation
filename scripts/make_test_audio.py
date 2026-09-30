#!/usr/bin/env python3
"""用 macOS 自带的 say 生成法语测试音频（纯合成语音，不含任何真实录音），输出到 samples/。

生成两段内容相同、节奏不同的音频：
- fr_meeting.wav：句间停顿 0.7 秒，像正常开会发言；
- fr_continuous.wav：句间几乎不停、语速更快，用来测“一直有人说话、没有明显停顿”时的延迟。
同时生成 .txt 标准答案（每行一句），用于计算识别错误率。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICE = "Thomas"  # fr_FR

SENTENCES = [
    "Bonjour à toutes et à tous, et merci d'être venus à cette réunion.",
    "Aujourd'hui, nous allons faire le point sur l'avancement du projet de traduction automatique.",
    "La semaine dernière, l'équipe a terminé la première version du système de reconnaissance vocale.",
    "Les résultats sont encourageants : le taux d'erreur est passé de douze à sept pour cent.",
    "Cependant, il reste plusieurs problèmes à résoudre, notamment la latence, "
    "qui est encore trop élevée pour une utilisation en temps réel.",
    "Est-ce que quelqu'un a des questions sur ce point ?",
    "Très bien. Passons maintenant au budget.",
    "Nous avons dépensé environ soixante pour cent des fonds prévus, ce qui correspond à peu près à notre calendrier.",
    "Pour la suite, je propose que nous organisions une démonstration devant le comité de direction "
    "au début du mois de novembre, afin de présenter le prototype et de recueillir leurs commentaires "
    "avant la phase de test avec les utilisateurs.",
    "Merci à tous pour votre travail, et à la semaine prochaine.",
]

VARIANTS = {
    "fr_meeting": {"pause_ms": 700, "rate": 175},
    "fr_continuous": {"pause_ms": 120, "rate": 200},
}


def main() -> int:
    if sys.platform != "darwin":
        print("这个脚本依赖 macOS 的 say 命令")
        return 1
    out_dir = ROOT / "samples"
    out_dir.mkdir(exist_ok=True)
    for name, variant in VARIANTS.items():
        # [[slnc N]] 是 say 的内嵌指令：插入 N 毫秒静音
        text = f" [[slnc {variant['pause_ms']}]] ".join(SENTENCES)
        wav = out_dir / f"{name}.wav"
        subprocess.run(
            ["say", "-v", VOICE, "-r", str(variant["rate"]),
             "--file-format=WAVE", "--data-format=LEI16@16000", "-o", str(wav), text],
            check=True,
        )
        (out_dir / f"{name}.txt").write_text("\n".join(SENTENCES) + "\n", encoding="utf-8")
        print(f"已生成 {wav}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
