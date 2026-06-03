#!/usr/bin/env python3
"""Run 6-max rotation benchmark and rank bots by average chip delta."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sandbox.match import run_match  # noqa: E402

BOTS = [
    ("tier2_current", "bots/github_bots/tier2_current/bot.py"),
    ("abstract_pressure_v5-2", "bots/abstract_pressure/v5-2/bot.py"),
    ("abstract_pressure_v5-2_copy", "bots/abstract_pressure/v5-2 copy/bot.py"),
    ("stageE", "bots/github_bots/stageE/bot.py"),
    ("equity_guard_v2", "bots/equity_guard/v2/bot.py"),
    ("abstract_control_v3", "bots/abstract_control/v3/bot.py"),
    ("position_bully_v3", "bots/position_bully/v3/bot.py"),
    ("adaptive_hybrid_v3-2", "bots/adaptive_hybrid/v3-2/bot.py"),
    ("abstract_pressure_v5", "bots/abstract_pressure/v5/bot.py"),
]

# 9 bots -> 3 six-max tables per seed; each bot plays 2 tables per seed.
ROTATIONS = [
    tuple(i for i in range(len(BOTS)) if i not in sit_out)
    for sit_out in ((6, 7, 8), (3, 4, 5), (0, 1, 2))
]


def run_benchmark(seeds: list[int], hands: int, out_path: Path) -> dict:
    deltas: dict[str, list[float]] = defaultdict(list)
    match_rows: list[dict] = []
    total_matches = len(seeds) * len(ROTATIONS)
    done = 0
    started = time.time()

    for seed in seeds:
        for table_idx, indices in enumerate(ROTATIONS):
            table = {BOTS[i][0]: str(ROOT / BOTS[i][1]) for i in indices}
            match_id = f"bench_s{seed}_t{table_idx}"
            match = run_match(match_id, table, n_hands=hands, seed=seed)
            done += 1
            row = {
                "match_id": match_id,
                "seed": seed,
                "table": table_idx,
                "bots": [BOTS[i][0] for i in indices],
                "chip_delta": match["chip_delta"],
                "duration_s": match["duration_s"],
                "bot_errors": {k: v for k, v in match["bot_errors"].items() if v},
            }
            match_rows.append(row)
            for bot_id, delta in match["chip_delta"].items():
                deltas[bot_id].append(float(delta))

            elapsed = time.time() - started
            print(
                f"[{done}/{total_matches}] seed={seed} table={table_idx} "
                f"duration={match['duration_s']}s elapsed={elapsed:.0f}s",
                flush=True,
            )

            out_path.write_text(
                json.dumps(
                    {
                        "hands": hands,
                        "seeds": seeds,
                        "matches_completed": done,
                        "matches_total": total_matches,
                        "match_rows": match_rows,
                        "rankings": _rankings(deltas),
                    },
                    indent=2,
                )
            )

    return {
        "hands": hands,
        "seeds": seeds,
        "matches": len(match_rows),
        "match_rows": match_rows,
        "rankings": _rankings(deltas),
        "elapsed_s": round(time.time() - started, 2),
    }


def _rankings(deltas: dict[str, list[float]]) -> list[dict]:
    rows = []
    for bot_id, values in deltas.items():
        avg = sum(values) / len(values)
        rows.append(
            {
                "bot_id": bot_id,
                "matches": len(values),
                "avg_delta": round(avg, 2),
                "total_delta": round(sum(values), 2),
                "best_delta": round(max(values), 2),
                "worst_delta": round(min(values), 2),
            }
        )
    rows.sort(key=lambda r: (-r["avg_delta"], -r["total_delta"]))
    for i, row in enumerate(rows, 1):
        row["rank"] = i
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="6-max rotation benchmark")
    parser.add_argument("--hands", type=int, default=400)
    parser.add_argument("--seeds", type=int, default=16)
    parser.add_argument("--seed-start", type=int, default=1000)
    parser.add_argument(
        "--out",
        default=str(ROOT / "poker_history" / "benchmark_sixmax_results.json"),
    )
    args = parser.parse_args()

    seeds = list(range(args.seed_start, args.seed_start + args.seeds))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    result = run_benchmark(seeds, args.hands, out_path)
    print("\n=== FINAL RANKINGS (avg chip delta) ===")
    for row in result["rankings"]:
        print(
            f"{row['rank']:2d}. {row['bot_id']:<28} "
            f"avg={row['avg_delta']:+8.2f}  "
            f"matches={row['matches']}  "
            f"total={row['total_delta']:+9.2f}"
        )
    print(f"\nSaved: {out_path}")
    print(f"Elapsed: {result['elapsed_s']}s")


if __name__ == "__main__":
    main()
