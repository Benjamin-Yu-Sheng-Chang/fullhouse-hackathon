"""Generate standalone versioned bots from strategy component profiles."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_components.registry import STRATEGIES, VERSION_OVERRIDES
from strategy_components.templates import BOT_TEMPLATE


def render_bot(strategy_name: str, version: str) -> str:
    if strategy_name not in STRATEGIES:
        available = ", ".join(sorted(STRATEGIES))
        raise SystemExit(f"Unknown strategy: {strategy_name}. Available: {available}")

    config = dict(STRATEGIES[strategy_name])
    config.update(VERSION_OVERRIDES.get(strategy_name, {}).get(version, {}))
    bot_name = config.pop("bot_name")
    description = config.pop("description")
    return BOT_TEMPLATE.format(
        bot_name=bot_name,
        description=description,
        config=repr(config),
    )


def build_bot(strategy_name: str, version: str, force: bool = False) -> Path:
    output_dir = ROOT / "bots" / strategy_name / version
    output_path = output_dir / "bot.py"
    if output_path.exists() and not force:
        raise SystemExit(f"Refusing to overwrite existing bot: {output_path}. Use --force.")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_bot(strategy_name, version))
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Build a standalone bot.py from a composable strategy profile")
    parser.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    parser.add_argument("--version", required=True, help="Version folder to write, e.g. v1")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing bot.py")
    parser.add_argument("--print", action="store_true", help="Print generated source instead of writing it")
    args = parser.parse_args()

    if args.print:
        print(render_bot(args.strategy, args.version))
        return

    output_path = build_bot(args.strategy, args.version, args.force)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
