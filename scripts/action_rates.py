#!/usr/bin/env python3
"""Summarize bot action rates from exported Fullhouse hand histories."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ACTIONS = ("fold", "check", "call", "raise", "all_in")
AGGRESSIVE = {"raise", "all_in"}
VOLUNTARY = {"call", "raise", "all_in"}
BLINDS = {"small_blind", "big_blind"}


def load_match(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def seat_maps(match: dict[str, Any]) -> tuple[dict[int, str], dict[str, str]]:
    seat_to_id = {}
    id_to_name = {}
    for bot in match.get("bots", []):
        bot_id = str(bot.get("bot_id"))
        id_to_name[bot_id] = str(bot.get("bot_name"))
        if isinstance(bot.get("seat"), int):
            seat_to_id[bot["seat"]] = bot_id
    return seat_to_id, id_to_name


def empty_stats() -> dict[str, Any]:
    return {
        "matches": set(),
        "hands": 0,
        "hands_with_action": 0,
        "voluntary_hands": 0,
        "aggressive_hands": 0,
        "showdown_hands": 0,
        "won_hands": 0,
        "action_opportunities": 0,
        "facing_bet_opportunities": 0,
        "actions": Counter(),
        "street_ended": Counter(),
        "chip_delta": 0,
        "final_stack_total": 0,
        "bot_name": None,
    }


def pre_action_owed(action_log: list[dict[str, Any]], index: int, seat: int) -> int:
    """Approximate whether the actor faced a bet before this action.

    Histories export a flat action log without street-reset markers, so this is
    approximate. It is still useful for identifying call/fold frequencies while
    facing pressure. Street-level exactness would require the richer event log.
    """

    current_bet = 0
    seat_bets: defaultdict[int, int] = defaultdict(int)
    for action in action_log[:index]:
        name = action.get("action")
        amount = int(action.get("amount") or 0)
        action_seat = action.get("seat")
        if name in BLINDS:
            seat_bets[action_seat] += amount
        elif name == "call":
            seat_bets[action_seat] += amount
        elif name in AGGRESSIVE:
            seat_bets[action_seat] = max(seat_bets[action_seat], amount)
            current_bet = max(current_bet, amount)
    return max(0, current_bet - seat_bets[seat])


def summarize_paths(paths: list[Path], target: str | None) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(empty_stats)

    for path in paths:
        match = load_match(path)
        seat_to_id, id_to_name = seat_maps(match)
        target_ids = {
            bot_id for bot_id, name in id_to_name.items()
            if target is None or target == bot_id or target.lower() in name.lower()
        }
        if target is not None and not target_ids:
            continue

        match_id = str(match.get("match_id", path.stem))
        bot_by_id = {str(bot.get("bot_id")): bot for bot in match.get("bots", [])}
        for bot_id, bot in bot_by_id.items():
            if target_ids and bot_id not in target_ids:
                continue
            row = stats[bot_id]
            row["matches"].add(match_id)
            row["bot_name"] = id_to_name.get(bot_id, bot_id)
            row["chip_delta"] += int(bot.get("chip_delta") or 0)
            row["final_stack_total"] += int(bot.get("final_stack") or 0)

        for hand in match.get("hands", []):
            actions_by_bot: defaultdict[str, list[str]] = defaultdict(list)
            for index, action in enumerate(hand.get("action_log", [])):
                name = action.get("action")
                seat = action.get("seat")
                bot_id = seat_to_id.get(seat)
                if bot_id is None or name in BLINDS:
                    continue
                if target_ids and bot_id not in target_ids:
                    continue
                row = stats[bot_id]
                row["actions"][name] += 1
                row["action_opportunities"] += 1
                row["street_ended"][hand.get("street_ended", "unknown")] += 1
                if pre_action_owed(hand.get("action_log", []), index, seat) > 0:
                    row["facing_bet_opportunities"] += 1
                actions_by_bot[bot_id].append(name)

            winners = {winner.get("bot_id") for winner in hand.get("winners", [])}
            revealed = set(hand.get("revealed_cards", {}))
            for bot_id, hand_actions in actions_by_bot.items():
                row = stats[bot_id]
                row["hands"] += 1
                row["hands_with_action"] += 1
                if any(action in VOLUNTARY for action in hand_actions):
                    row["voluntary_hands"] += 1
                if any(action in AGGRESSIVE for action in hand_actions):
                    row["aggressive_hands"] += 1
                if bot_id in winners:
                    row["won_hands"] += 1
                if bot_id in revealed:
                    row["showdown_hands"] += 1

    return dict(stats)


def pct(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def row_for(bot_id: str, stats: dict[str, Any]) -> dict[str, Any]:
    actions = stats["actions"]
    opportunities = stats["action_opportunities"]
    hands = stats["hands"]
    aggressive = actions["raise"] + actions["all_in"]
    non_check_actions = actions["fold"] + actions["call"] + aggressive
    return {
        "bot_id": bot_id,
        "bot_name": stats["bot_name"] or bot_id,
        "matches": len(stats["matches"]),
        "hands": hands,
        "chip_delta": stats["chip_delta"],
        "avg_delta": pct(stats["chip_delta"], len(stats["matches"])),
        "opportunities": opportunities,
        "folds": actions["fold"],
        "checks": actions["check"],
        "calls": actions["call"],
        "raises": actions["raise"],
        "all_ins": actions["all_in"],
        "call_rate": pct(actions["call"], opportunities),
        "raise_rate": pct(aggressive, opportunities),
        "fold_rate": pct(actions["fold"], opportunities),
        "check_rate": pct(actions["check"], opportunities),
        "aggression_factor": aggressive / max(1, actions["call"]),
        "vpip_rate": pct(stats["voluntary_hands"], hands),
        "pfr_rate": pct(stats["aggressive_hands"], hands),
        "won_hand_rate": pct(stats["won_hands"], hands),
        "showdown_rate": pct(stats["showdown_hands"], hands),
        "facing_bet_rate": pct(stats["facing_bet_opportunities"], opportunities),
        "non_check_aggression_rate": pct(aggressive, non_check_actions),
    }


def format_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = [
        "bot_name", "matches", "hands", "avg_delta", "vpip_rate", "pfr_rate",
        "call_rate", "raise_rate", "fold_rate", "aggression_factor", "all_ins",
    ]
    widths = {header: len(header) for header in headers}
    display_rows = []
    for row in rows:
        display = dict(row)
        for key in ("vpip_rate", "pfr_rate", "call_rate", "raise_rate", "fold_rate"):
            display[key] = format_pct(row[key])
        display["avg_delta"] = f"{row['avg_delta']:+.1f}"
        display["aggression_factor"] = f"{row['aggression_factor']:.2f}"
        for header in headers:
            widths[header] = max(widths[header], len(str(display[header])))
        display_rows.append(display)

    print("  ".join(header.ljust(widths[header]) for header in headers))
    print("  ".join("-" * widths[header] for header in headers))
    for row in display_rows:
        print("  ".join(str(row[header]).ljust(widths[header]) for header in headers))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["bot_id"])
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute bot action rates from match hand-history JSON.")
    parser.add_argument("--history-dir", type=Path, default=Path("poker_history"))
    parser.add_argument("--target", help="Bot name substring or bot_id. Omit to summarize every bot.")
    parser.add_argument("--csv", type=Path, help="Optional CSV output path.")
    parser.add_argument("--json", type=Path, help="Optional JSON output path.")
    parser.add_argument("--sort", default="avg_delta", help="Column to sort by. Default: avg_delta.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = sorted(args.history_dir.glob("match-*.json"))
    stats = summarize_paths(paths, args.target)
    rows = [row_for(bot_id, row) for bot_id, row in stats.items()]
    rows.sort(key=lambda row: row.get(args.sort, 0), reverse=True)

    if not rows:
        raise SystemExit("No matching bot actions found.")
    print_table(rows)
    if args.csv:
        write_csv(args.csv, rows)
        print(f"\nWrote CSV: {args.csv}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
