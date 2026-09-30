"""命令行入口：python -m simul_interp <子命令>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import load_config


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))
    print(f"\nAPI 密钥（{cfg.translate.api_key_env}）：{'已设置' if cfg.api_key() else '未设置'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simul_interp", description="法语→中文实时同声传译")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径，默认读取项目根目录的 config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("config", help="打印生效的配置")
    p.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
