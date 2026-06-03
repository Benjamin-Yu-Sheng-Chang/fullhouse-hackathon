#!/usr/bin/env python3
"""Build a compact LLM-ready report for abstract_pressure_v1 losses.

The production history names this bot "codex_iterated_abstract_pressure",
while local iteration often calls it "abstract_pressure_v1".  This script
normalizes both names, finds losing matches, and prints the most useful context
for another model to debug the strategy.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_ALIASES = (
    "abstract_pressure_v1",
    "codex_iterated_abstract_pressure",
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def find_bot(match: dict[str, Any], aliases: set[str]) -> dict[str, Any] | None:
    for bot in match.get("bots", []):
        if bot.get("bot_name") in aliases or bot.get("bot_id") in aliases:
            return bot
    return None


def bot_name_by_id(match: dict[str, Any]) -> dict[str, str]:
    return {
        str(bot.get("bot_id")): str(bot.get("bot_name"))
        for bot in match.get("bots", [])
    }


def bot_id_by_seat(match: dict[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for bot in match.get("bots", []):
        seat = bot.get("seat")
        if isinstance(seat, int):
            result[seat] = str(bot.get("bot_id"))
    return result


def action_summary(hand: dict[str, Any], seat: int) -> dict[str, Any]:
    actions = [
        action
        for action in hand.get("action_log", [])
        if action.get("seat") == seat
    ]
    aggressive = [
        action
        for action in actions
        if action.get("action") in {"raise", "all_in"}
    ]
    passive_exit = any(action.get("action") == "fold" for action in actions)
    return {
        "actions": actions,
        "action_count": len(actions),
        "aggressive_action_count": len(aggressive),
        "folded": passive_exit,
    }


def estimated_seat_invested(hand: dict[str, Any], seat: int) -> int:
    """Estimate chips put in by a seat from the flat action log.

    The public hand histories do not include street markers or stack snapshots,
    so raise/all-in amounts can only be approximated.  This is still useful for
    ranking suspicious hands; the final full action log is included for review.
    """

    invested = 0
    for action in hand.get("action_log", []):
        if action.get("seat") != seat:
            continue
        name = action.get("action")
        amount = int(action.get("amount") or 0)
        if name in {"small_blind", "big_blind", "call"}:
            invested += amount
        elif name in {"raise", "all_in"}:
            invested += amount
    return invested


def hand_net_estimate(
    match: dict[str, Any],
    hand: dict[str, Any],
    bot_id: str,
    seat: int,
) -> int:
    won = sum(
        int(winner.get("amount") or 0)
        for winner in hand.get("winners", [])
        if winner.get("bot_id") == bot_id
    )
    return won - estimated_seat_invested(hand, seat)


def winner_names(match: dict[str, Any], hand: dict[str, Any]) -> list[dict[str, Any]]:
    names = bot_name_by_id(match)
    return [
        {
            "bot_name": names.get(str(winner.get("bot_id")), winner.get("bot_id")),
            "bot_id": winner.get("bot_id"),
            "amount": winner.get("amount"),
        }
        for winner in hand.get("winners", [])
    ]


def compact_hand(
    match: dict[str, Any],
    hand: dict[str, Any],
    bot: dict[str, Any],
) -> dict[str, Any]:
    seat_to_id = bot_id_by_seat(match)
    names = bot_name_by_id(match)
    seat = int(bot["seat"])
    bot_id = str(bot["bot_id"])
    revealed = hand.get("revealed_cards", {})
    bot_cards = revealed.get(bot_id)
    summary = action_summary(hand, seat)
    annotated_actions = []
    for action in hand.get("action_log", []):
        action_seat = action.get("seat")
        action_bot_id = seat_to_id.get(action_seat)
        annotated = dict(action)
        annotated["bot_name"] = names.get(action_bot_id, str(action_bot_id))
        if action_seat == seat:
            annotated["is_target_bot"] = True
        annotated_actions.append(annotated)

    return {
        "hand_num": hand.get("hand_num"),
        "estimated_target_net": hand_net_estimate(match, hand, bot_id, seat),
        "street_ended": hand.get("street_ended"),
        "pot": hand.get("pot"),
        "community_cards": hand.get("community_cards", []),
        "target_revealed_cards": bot_cards,
        "target_actions": summary["actions"],
        "target_action_count": summary["action_count"],
        "target_aggressive_action_count": summary["aggressive_action_count"],
        "target_folded": summary["folded"],
        "winners": winner_names(match, hand),
        "revealed_cards_by_bot_name": {
            names.get(bot_id, bot_id): cards
            for bot_id, cards in revealed.items()
        },
        "full_action_log_annotated": annotated_actions,
    }


def interesting_hands(
    match: dict[str, Any],
    bot: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    seat = int(bot["seat"])
    bot_id = str(bot["bot_id"])
    rows = []
    for hand in match.get("hands", []):
        actions = action_summary(hand, seat)
        won = any(winner.get("bot_id") == bot_id for winner in hand.get("winners", []))
        if not actions["actions"] and not won:
            continue
        rows.append(compact_hand(match, hand, bot))

    rows.sort(
        key=lambda hand: (
            hand["estimated_target_net"],
            -int(hand.get("pot") or 0),
        )
    )
    return rows[:limit]


def placement(match: dict[str, Any], bot: dict[str, Any]) -> int:
    ordered = sorted(
        match.get("bots", []),
        key=lambda row: int(row.get("chip_delta") or 0),
        reverse=True,
    )
    for index, row in enumerate(ordered, start=1):
        if row.get("bot_id") == bot.get("bot_id"):
            return index
    return len(ordered)


def match_summary(path: Path, match: dict[str, Any], bot: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": str(path),
        "match_id": match.get("match_id"),
        "completed_at": match.get("completed_at"),
        "tournament": match.get("tournament"),
        "round": match.get("round"),
        "hands_played": len(match.get("hands", [])),
        "target_bot_name_in_history": bot.get("bot_name"),
        "target_bot_id": bot.get("bot_id"),
        "target_seat": bot.get("seat"),
        "target_final_stack": bot.get("final_stack"),
        "target_chip_delta": bot.get("chip_delta"),
        "target_place": placement(match, bot),
        "table_size": len(match.get("bots", [])),
        "opponents": [
            {
                "bot_name": row.get("bot_name"),
                "seat": row.get("seat"),
                "final_stack": row.get("final_stack"),
                "chip_delta": row.get("chip_delta"),
            }
            for row in match.get("bots", [])
            if row.get("bot_id") != bot.get("bot_id")
        ],
        "bot_errors": bot.get("bot_errors", []),
    }


def read_source(path: Path | None, max_chars: int) -> str | None:
    if path is None:
        return None
    if not path.exists():
        return f"Source path not found: {path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n... truncated ..."


def render_markdown(
    summaries: list[dict[str, Any]],
    selected: list[tuple[Path, dict[str, Any], dict[str, Any]]],
    hand_limit: int,
    aliases: list[str],
    source_path: Path | None,
    source_text: str | None,
) -> str:
    lines: list[str] = []
    lines.append("# Deepseek Poker Loss Dossier")
    lines.append("")
    lines.append("## Task for Deepseek")
    lines.append(
        "Find strategy bugs or bad heuristics in abstract_pressure_v1. "
        "In production histories the same bot appears as "
        "`codex_iterated_abstract_pressure`, so treat those names as aliases."
    )
    lines.append("")
    lines.append("## Alias Mapping")
    lines.append("")
    for alias in aliases:
        lines.append(f"- `{alias}`")
    lines.append("")
    lines.append("## Loss Summary")
    lines.append("")
    lines.append(f"- Losing matches found: {len(summaries)}")
    if summaries:
        avg_delta = sum(int(row["target_chip_delta"]) for row in summaries) / len(summaries)
        busted = sum(1 for row in summaries if int(row["target_final_stack"] or 0) <= 0)
        lines.append(f"- Average chip delta in losses: {avg_delta:.1f}")
        lines.append(f"- Bust-outs in losses: {busted}/{len(summaries)}")
    lines.append("")
    lines.append("| chip_delta | final_stack | place | hands | match_id | file |")
    lines.append("|---:|---:|---:|---:|---|---|")
    for row in summaries:
        lines.append(
            "| {target_chip_delta} | {target_final_stack} | {target_place}/{table_size} | "
            "{hands_played} | {match_id} | {file} |".format(**row)
        )
    lines.append("")

    for path, match, bot in selected:
        summary = match_summary(path, match, bot)
        lines.append("## Match " + str(summary["match_id"]))
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(summary, indent=2))
        lines.append("```")
        lines.append("")
        lines.append(
            f"Most suspicious target-bot hands by estimated target net "
            f"(limit {hand_limit}). The estimate is approximate because the "
            "history action log is flat and omits street boundary stack snapshots."
        )
        lines.append("")
        lines.append("```json")
        lines.append(
            json.dumps(
                interesting_hands(match, bot, hand_limit),
                indent=2,
            )
        )
        lines.append("```")
        lines.append("")

    if source_text is not None:
        lines.append("## Bot Source")
        lines.append("")
        lines.append(f"Source path: `{source_path}`")
        lines.append("")
        lines.append("```python")
        lines.append(source_text)
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find abstract_pressure_v1/codex_iterated_abstract_pressure losses and print a Deepseek-ready report."
    )
    parser.add_argument(
        "--history-dir",
        default="poker_history",
        type=Path,
        help="Directory containing match-*.json files.",
    )
    parser.add_argument(
        "--alias",
        action="append",
        default=[],
        help="Additional bot name or bot_id to treat as the target. Can be repeated.",
    )
    parser.add_argument(
        "--worst-matches",
        type=int,
        default=5,
        help="How many losing matches to expand with hand details.",
    )
    parser.add_argument(
        "--hands-per-match",
        type=int,
        default=8,
        help="How many suspicious hands to include for each expanded match.",
    )
    parser.add_argument(
        "--source",
        default="bots/abstract_pressure/v1/bot.py",
        help="Bot source to include. Use --source '' to skip.",
    )
    parser.add_argument(
        "--source-max-chars",
        type=int,
        default=25000,
        help="Maximum source characters to include.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write Markdown report to this file instead of stdout.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    aliases = list(DEFAULT_ALIASES) + args.alias
    alias_set = set(aliases)
    history_paths = sorted(args.history_dir.glob("match-*.json"))
    losing: list[tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]] = []

    for path in history_paths:
        match = load_json(path)
        bot = find_bot(match, alias_set)
        if not bot:
            continue
        if int(bot.get("chip_delta") or 0) < 0:
            losing.append((path, match, bot, match_summary(path, match, bot)))

    losing.sort(key=lambda row: int(row[3]["target_chip_delta"]))
    summaries = [row[3] for row in losing]
    selected = [(path, match, bot) for path, match, bot, _ in losing[: args.worst_matches]]

    source_path = None if args.source == "" else Path(args.source)
    source_text = read_source(source_path, args.source_max_chars)
    report = render_markdown(
        summaries=summaries,
        selected=selected,
        hand_limit=args.hands_per_match,
        aliases=aliases,
        source_path=source_path,
        source_text=source_text,
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
