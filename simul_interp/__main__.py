"""命令行入口：python -m simul_interp <子命令>"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

from . import __version__
from .config import ROOT, Config, apply_overrides, load_config


def get_config(args: argparse.Namespace) -> Config:
    """读配置文件，再用命令行覆盖：--set 段.项=值；--file 改用音频文件作为输入，--speed 控制播放倍速。"""
    cfg = load_config(args.config)
    apply_overrides(cfg, args.set)
    if getattr(args, "file", None):
        cfg.audio.source = "file"
        cfg.audio.file = str(args.file)
    if getattr(args, "speed", None) is not None:
        cfg.audio.file_speed = args.speed
    return cfg


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:  # httpx 会把每一次翻译请求都记一条 INFO 日志，同传时会刷屏
        logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_config(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    print(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))
    print(f"\nAPI 密钥（{cfg.translate.api_key_env}）：{'已设置' if cfg.api_key() else '未设置'}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """录一段电脑正在播放的声音存成 WAV，同时每秒显示一次音量，用来检查内录是否正常。"""
    from .audio import SAMPLE_RATE, create_source
    from .audio.util import meter, rms_dbfs, write_wav

    cfg = get_config(args)
    out = args.out or ROOT / "recordings" / f"system_{time.strftime('%Y%m%d_%H%M%S')}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)

    source = create_source(cfg)
    blocks: list[np.ndarray] = []
    second: list[np.ndarray] = []
    print(f"开始录制 {args.seconds:g} 秒（Ctrl+C 可提前结束）……")
    source.start()
    try:
        for chunk in source.chunks():
            blocks.append(chunk.samples)
            second.append(chunk.samples)
            if len(second) * len(chunk.samples) >= SAMPLE_RATE:
                level = rms_dbfs(np.concatenate(second))
                print(f"{chunk.end:5.1f}s  {meter(level)}  {level:6.1f} dBFS", flush=True)
                second = []
            if chunk.end >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()

    audio = np.concatenate(blocks) if blocks else np.zeros(0, dtype=np.float32)
    write_wav(out, audio)
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    print(f"\n已保存 {out}（{len(audio) / SAMPLE_RATE:.1f} 秒，整体音量 {rms_dbfs(audio):.1f} dBFS，峰值 {peak:.2f}）")
    if not np.any(audio):
        print("整段都是静音：请确认电脑在播放声音，并检查系统录音权限。")
        return 1
    return 0




def cmd_vad(args: argparse.Namespace) -> int:
    """只跑语音检测，打印检测到的每一段语音，用来调 VAD 参数。"""
    from .audio import create_source
    from .vad import SileroVad, VadSegmenter

    cfg = get_config(args)
    source = create_source(cfg)
    vad = SileroVad()
    segmenter = VadSegmenter(cfg.vad.threshold, cfg.vad.min_silence_ms, cfg.vad.speech_pad_ms)
    print(f"输入：{source.name}；阈值 {cfg.vad.threshold}，静音 {cfg.vad.min_silence_ms} ms 判定一句结束")
    cost, count, start, last = 0.0, 0, 0.0, 0.0
    source.start()
    try:
        for chunk in source.chunks():
            t = time.perf_counter()
            prob = vad(chunk.samples)
            cost += time.perf_counter() - t
            count += 1
            last = chunk.end
            for event in segmenter.process(chunk, prob):
                if event.type == "start":
                    start = event.time
                elif event.type == "end":
                    print(f"  语音 {start:6.2f}s → {event.time:6.2f}s  （{event.time - start:4.1f} 秒）", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
    for event in segmenter.flush(last):
        print(f"  语音 {start:6.2f}s → {event.time:6.2f}s  （{event.time - start:4.1f} 秒，未结束）")
    if count:
        print(f"共处理 {last:.1f} 秒音频，VAD 平均每 32 ms 的块耗时 {cost / count * 1000:.2f} ms")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """同传主程序。"""
    from .pipeline import Interpreter

    cfg = get_config(args)
    if args.ui:
        cfg.ui.mode = args.ui
    options = dict(
        translate=not args.no_translate,
        mock_translate=args.mock_translate,
        save_transcript=not args.no_transcript,
        save_recording=not args.no_recording,
    )
    if cfg.ui.mode == "overlay":
        from .ui.overlay import run_overlay

        return run_overlay(cfg, lambda view: Interpreter(cfg, view, **options))
    if cfg.ui.mode == "console":
        from .ui.console import ConsoleView

        interpreter = Interpreter(cfg, ConsoleView(), **options)
        print(f"输入：{interpreter.source.name}。按 Ctrl+C 结束。")
        try:
            interpreter.run()
        except KeyboardInterrupt:
            print("\n已停止")
        return 0
    raise SystemExit(f"未知的界面：{cfg.ui.mode}")


def cmd_bench_asr(args: argparse.Namespace) -> int:
    from .bench import bench_asr

    return bench_asr(get_config(args), args.file, speed=args.speed)


def cmd_bench_pipeline(args: argparse.Namespace) -> int:
    from .bench import bench_pipeline

    return bench_pipeline(get_config(args), args.file, speed=args.speed, mock=args.mock_translate)


def cmd_bench_translate(args: argparse.Namespace) -> int:
    from .bench import bench_translate

    return bench_translate(get_config(args), args.sentences)


def add_source_args(p: argparse.ArgumentParser, default_speed: float | None = None) -> None:
    p.add_argument("--file", type=Path, default=None, help="用音频文件代替内录（任何 ffmpeg 支持的格式）")
    p.add_argument("--speed", type=float, default=default_speed, help="文件播放倍速：1 = 实时，0 = 尽快")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simul_interp", description="法语→中文实时同声传译")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径，默认读取项目根目录的 config.toml")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument(
        "--set", action="append", default=[], metavar="段.项=值", help="临时覆盖配置，可重复，例如 --set asr.step_s=1.0"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="开始同传（默认内录系统声音）")
    add_source_args(p)
    p.add_argument("--ui", choices=["overlay", "console"], default=None, help="界面：透明悬浮窗或终端，默认看配置")
    p.add_argument("--no-translate", action="store_true", help="只识别不翻译")
    p.add_argument("--mock-translate", action="store_true", help="用模拟翻译（不联网），测试流程和界面")
    p.add_argument("--no-transcript", action="store_true", help="不保存会话记录")
    p.add_argument("--no-recording", action="store_true", help="这一次不保存录音（内录时默认会存一份 WAV）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("config", help="打印生效的配置")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("record", help="录一段系统声音存成 WAV，检查内录是否正常")
    p.add_argument("--seconds", type=float, default=10.0, help="录制时长（秒），默认 10")
    p.add_argument("--out", type=Path, default=None, help="输出文件，默认 recordings/system_时间.wav")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("vad", help="只跑语音检测，打印每一段语音的起止时间")
    add_source_args(p, default_speed=0.0)
    p.set_defaults(func=cmd_vad)

    p = sub.add_parser("bench-asr", help="用音频文件按真实语速跑流式识别，统计延迟和准确率")
    p.add_argument("file", type=Path, help="测试音频；同名 .txt 是标准答案（可选）")
    p.add_argument("--speed", type=float, default=1.0, help="播放倍速，默认 1 = 实时")
    p.set_defaults(func=cmd_bench_asr)

    p = sub.add_parser("bench-pipeline", help="识别 + 翻译端到端：统计一句话说完到出现中文要多久")
    p.add_argument("file", type=Path, help="测试音频")
    p.add_argument("--speed", type=float, default=1.0, help="播放倍速，默认 1 = 实时")
    p.add_argument("--mock-translate", action="store_true", help="用模拟翻译（不联网，首字固定约 0.3 秒）")
    p.set_defaults(func=cmd_bench_pipeline)

    p = sub.add_parser("bench-translate", help="逐句实测翻译服务的首字延迟和整句完成时间")
    p.add_argument("--sentences", type=Path, default=None, help="每行一句法语的文本文件，默认用内置的 5 句")
    p.set_defaults(func=cmd_bench_translate)
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":  # Windows 控制台被重定向时编码可能不是 UTF-8，打印中文会报错
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
