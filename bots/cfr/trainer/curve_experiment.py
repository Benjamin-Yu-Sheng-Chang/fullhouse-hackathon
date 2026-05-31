#!/usr/bin/env python3
"""Run a cumulative abstract-CFR training curve experiment.

Every checkpoint is packaged as the real upload shape:

    bot.py
    data/cfr_strategy.npz

The benchmark then runs from that zip, so the curve measures the same artifact
you would submit.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import random
import sys
import time
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sandbox.compare import _resolve_bot_specs
from sandbox.match import run_match
from sandbox.validator import validate

from abstraction import legal_action_mask
from train import RegretTable, sample_state, save_npz, utilities_for_state
from abstraction import abstract_state_key


DEFAULT_OPPONENTS = [
    "adaptive_hybrid:v2",
    "equity_guard:v2",
    "position_bully:v2",
    "trap_steal:v2",
    "shark:v1",
]


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _safe_write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n")


def _train_more(
    table: RegretTable,
    *,
    start_iteration: int,
    target_iteration: int,
    max_iterations: int,
    samples: int,
    equity_samples: int,
    epsilon: float,
    min_epsilon: float,
    rng: random.Random,
    log_interval: int,
) -> None:
    for iteration in range(start_iteration + 1, target_iteration + 1):
        explore = max(min_epsilon, epsilon * (1.0 - iteration / max(max_iterations, 1)))
        for _ in range(samples):
            state = sample_state(rng)
            key = abstract_state_key(state)
            mask = np.array(legal_action_mask(state), dtype=np.float64)
            utilities = utilities_for_state(state, rng, equity_samples)
            table.update(key, utilities, mask, explore)
        if log_interval and iteration % log_interval == 0:
            print(f"training {iteration:,}/{max_iterations:,} - {len(table.keys):,} infosets", flush=True)


def _checkpoint_metadata(args: argparse.Namespace, iteration: int, table: RegretTable) -> dict:
    return {
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "iteration": iteration,
        "max_iterations": args.max_iterations,
        "step_iterations": args.step_iterations,
        "samples": args.samples,
        "equity_samples": args.equity_samples,
        "epsilon": args.epsilon,
        "min_epsilon": args.min_epsilon,
        "seed": args.seed,
        "infosets": len(table.keys),
    }


def _save_checkpoint(args: argparse.Namespace, table: RegretTable, iteration: int, checkpoint_dir: Path) -> Path:
    npz_path = checkpoint_dir / f"cfr_{iteration:06d}.npz"
    save_npz(str(npz_path), table, _checkpoint_metadata(args, iteration, table))
    return npz_path


def _package_zip(bot_py: Path, npz_path: Path, zip_dir: Path, iteration: int) -> Path:
    zip_path = zip_dir / f"cfr_{iteration:06d}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(bot_py, "bot.py")
        archive.write(npz_path, "data/cfr_strategy.npz")
    return zip_path


def _validate_zip(zip_path: Path) -> dict:
    result = validate(str(zip_path))
    if not result.get("passed"):
        raise RuntimeError(f"Zip validation failed for {zip_path}: {result.get('errors')}")
    return result


def _rank_for(match: dict, bot_id: str) -> int:
    ordered = sorted(match["bot_ids"], key=lambda bid: -match["chip_delta"][bid])
    return ordered.index(bot_id) + 1


def _summarize(values: list[float]) -> dict:
    if not values:
        return {"avg": 0.0, "median": 0.0, "best": 0.0, "worst": 0.0}
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        median = float(ordered[middle])
    else:
        median = float((ordered[middle - 1] + ordered[middle]) / 2)
    return {
        "avg": float(sum(values) / len(values)),
        "median": median,
        "best": float(max(values)),
        "worst": float(min(values)),
    }


def _benchmark_checkpoint(
    *,
    zip_path: Path,
    iteration: int,
    checkpoint_index: int,
    opponents: dict[str, str],
    benchmark_iterations: int,
    hands: int,
    seed: int,
    per_seed_handle,
) -> tuple[dict, list[dict]]:
    candidate_id = f"cfr_{iteration:06d}"
    rows = []
    start = time.time()

    for bench_index in range(benchmark_iterations):
        match_seed = seed + checkpoint_index * 1_000_000 + bench_index
        table = {candidate_id: str(zip_path)}
        table.update(opponents)
        match = run_match(
            f"cfr_curve_{iteration:06d}_{bench_index:02d}",
            table,
            n_hands=hands,
            seed=match_seed,
        )
        place = _rank_for(match, candidate_id)
        opponent_ids = [bot_id for bot_id in match["bot_ids"] if bot_id != candidate_id]
        opponents_beaten = sum(
            1
            for opponent_id in opponent_ids
            if match["chip_delta"][candidate_id] > match["chip_delta"][opponent_id]
        )
        row = {
            "checkpoint_iteration": iteration,
            "benchmark_index": bench_index,
            "seed": match_seed,
            "hands_requested": hands,
            "hands_played": match["n_hands"],
            "chip_delta": match["chip_delta"][candidate_id],
            "final_stack": match["final_stacks"][candidate_id],
            "place": place,
            "top_half": place <= max(1, len(match["bot_ids"]) // 2),
            "win": place == 1,
            "busted": match["final_stacks"][candidate_id] <= 0,
            "opponents_beaten": opponents_beaten,
            "opponent_count": len(opponent_ids),
            "opponent_ids": opponent_ids,
            "opponent_deltas": {bot_id: match["chip_delta"][bot_id] for bot_id in opponent_ids},
            "duration_s": match["duration_s"],
        }
        rows.append(row)
        per_seed_handle.write(json.dumps(row, default=_json_default) + "\n")
        per_seed_handle.flush()

    deltas = [row["chip_delta"] for row in rows]
    places = [row["place"] for row in rows]
    delta_summary = _summarize(deltas)
    total_beaten = sum(row["opponents_beaten"] for row in rows)
    total_opponents = sum(row["opponent_count"] for row in rows)
    summary = {
        "iteration": iteration,
        "candidate_id": candidate_id,
        "zip_path": str(zip_path),
        "matches": len(rows),
        "hands": hands,
        "avg_delta": delta_summary["avg"],
        "median_delta": delta_summary["median"],
        "best_delta": delta_summary["best"],
        "worst_delta": delta_summary["worst"],
        "avg_place": float(sum(places) / max(1, len(places))),
        "top_half_rate": float(sum(1 for row in rows if row["top_half"]) / max(1, len(rows))),
        "win_rate": float(sum(1 for row in rows if row["win"]) / max(1, len(rows))),
        "bust_rate": float(sum(1 for row in rows if row["busted"]) / max(1, len(rows))),
        "opponents_beaten_rate": float(total_beaten / max(1, total_opponents)),
        "benchmark_duration_s": round(time.time() - start, 3),
    }
    return summary, rows


def _passes_consistency(row: dict) -> bool:
    return (
        row["avg_delta"] > 0
        and row["top_half_rate"] >= 0.60
        and row["opponents_beaten_rate"] >= 0.60
        and row["bust_rate"] <= 0.20
    )


def _consistent_checkpoint(rows: list[dict]) -> int | None:
    for index in range(2, len(rows)):
        window = rows[index - 2 : index + 1]
        if all(_passes_consistency(row) for row in window):
            return int(rows[index]["iteration"])
    return None


def _best_checkpoint(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return max(rows, key=lambda row: row["avg_delta"])


def _write_curve_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "iteration",
        "candidate_id",
        "matches",
        "hands",
        "infosets",
        "avg_delta",
        "median_delta",
        "best_delta",
        "worst_delta",
        "avg_place",
        "top_half_rate",
        "win_rate",
        "bust_rate",
        "opponents_beaten_rate",
        "benchmark_duration_s",
        "npz_path",
        "zip_path",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def _plot_curve(path: Path, rows: list[dict]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xs = [row["iteration"] for row in rows]
    labels = [f"{row['iteration'] // 1000}k" for row in rows]

    fig, axes = plt.subplots(3, 2, figsize=(14, 12))
    axes = axes.flatten()
    specs = [
        ("avg_delta", "Average Chip Delta", None),
        ("avg_place", "Average Place", None),
        ("top_half_rate", "Top-Half Rate", (0, 1)),
        ("bust_rate", "Bust Rate", (0, 1)),
        ("opponents_beaten_rate", "Opponents Beaten Rate", (0, 1)),
        ("win_rate", "Win Rate", (0, 1)),
    ]
    for axis, (metric, title, ylim) in zip(axes, specs):
        axis.plot(xs, [row[metric] for row in rows], marker="o", linewidth=2)
        if metric == "avg_delta":
            axis.axhline(0, color="black", linewidth=1, alpha=0.45)
        if metric in {"top_half_rate", "opponents_beaten_rate"}:
            axis.axhline(0.60, color="green", linestyle="--", linewidth=1, alpha=0.5)
        if metric == "bust_rate":
            axis.axhline(0.20, color="red", linestyle="--", linewidth=1, alpha=0.5)
        if ylim:
            axis.set_ylim(*ylim)
        axis.set_title(title)
        axis.set_xlabel("Cumulative Training Iterations")
        axis.set_xticks(xs)
        axis.set_xticklabels(labels, rotation=35, ha="right")
        axis.grid(True, alpha=0.3)
    fig.suptitle("Abstract CFR Training Curve", fontsize=16)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _checkpoint_iterations(max_iterations: int, step_iterations: int) -> list[int]:
    values = [0]
    current = step_iterations
    while current < max_iterations:
        values.append(current)
        current += step_iterations
    if max_iterations not in values:
        values.append(max_iterations)
    return values


def _resolve_opponents(tokens: list[str]) -> dict[str, str]:
    resolved = _resolve_bot_specs(tokens)
    if len(resolved) != len(tokens):
        raise SystemExit(f"Expected {len(tokens)} opponents, resolved {len(resolved)}: {list(resolved)}")
    return resolved


def run_experiment(args: argparse.Namespace) -> dict:
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    checkpoint_dir = output_dir / "checkpoints"
    zip_dir = output_dir / "zips"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    zip_dir.mkdir(parents=True, exist_ok=True)

    opponents = _resolve_opponents(args.opponents)
    bot_py = ROOT / "bots" / "cfr" / "bot.py"
    table = RegretTable()
    rng = random.Random(args.seed)
    np.random.seed(args.seed % (2**32))
    checkpoints = _checkpoint_iterations(args.max_iterations, args.step_iterations)
    rows = []
    current_iteration = 0
    per_seed_path = output_dir / "per_seed_results.jsonl"

    started = time.time()
    with per_seed_path.open("w") as per_seed_handle:
        for checkpoint_index, checkpoint in enumerate(checkpoints):
            if checkpoint > current_iteration:
                _train_more(
                    table,
                    start_iteration=current_iteration,
                    target_iteration=checkpoint,
                    max_iterations=args.max_iterations,
                    samples=args.samples,
                    equity_samples=args.equity_samples,
                    epsilon=args.epsilon,
                    min_epsilon=args.min_epsilon,
                    rng=rng,
                    log_interval=args.train_log_interval,
                )
                current_iteration = checkpoint

            npz_path = _save_checkpoint(args, table, checkpoint, checkpoint_dir)
            zip_path = _package_zip(bot_py, npz_path, zip_dir, checkpoint)
            validation = _validate_zip(zip_path) if args.validate_zips else {"passed": None}
            print(f"benchmarking {checkpoint:,} iterations from {zip_path}", flush=True)
            summary, _seed_rows = _benchmark_checkpoint(
                zip_path=zip_path,
                iteration=checkpoint,
                checkpoint_index=checkpoint_index,
                opponents=opponents,
                benchmark_iterations=args.benchmark_iterations,
                hands=args.hands,
                seed=args.seed,
                per_seed_handle=per_seed_handle,
            )
            summary.update(
                {
                    "infosets": len(table.keys),
                    "npz_path": str(npz_path),
                    "zip_validated": validation.get("passed"),
                    "validation_errors": validation.get("errors", []),
                }
            )
            rows.append(summary)
            _write_curve_csv(output_dir / "curve.csv", rows)
            partial = _build_summary(args, output_dir, opponents, rows, started)
            _safe_write_json(output_dir / "summary.json", partial)

    _plot_curve(output_dir / "curve.png", rows)
    final = _build_summary(args, output_dir, opponents, rows, started)
    final["artifacts"]["curve_png"] = str(output_dir / "curve.png")
    _safe_write_json(output_dir / "summary.json", final)
    return final


def _build_summary(
    args: argparse.Namespace,
    output_dir: Path,
    opponents: dict[str, str],
    rows: list[dict],
    started: float,
) -> dict:
    return {
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "max_iterations": args.max_iterations,
            "step_iterations": args.step_iterations,
            "samples": args.samples,
            "equity_samples": args.equity_samples,
            "epsilon": args.epsilon,
            "min_epsilon": args.min_epsilon,
            "benchmark_iterations": args.benchmark_iterations,
            "hands": args.hands,
            "seed": args.seed,
            "opponents": args.opponents,
        },
        "opponents": [{"bot_id": bot_id, "path": path} for bot_id, path in opponents.items()],
        "metric_definitions": {
            "avg_delta": "Average CFR candidate chip delta across benchmark seeds.",
            "avg_place": "Average finishing place at the 6-bot table; lower is better.",
            "top_half_rate": "Fraction of benchmark matches where CFR finished top 3.",
            "win_rate": "Fraction of benchmark matches where CFR finished first.",
            "bust_rate": "Fraction of benchmark matches where CFR ended with zero chips.",
            "opponents_beaten_rate": "Fraction of opponent seats CFR finished ahead of across all benchmark matches.",
            "consistent_beat_checkpoint": "First checkpoint where the last 3 checkpoints pass avg_delta/top_half/opponents_beaten/bust thresholds.",
        },
        "success_rule": {
            "window": 3,
            "avg_delta": "> 0",
            "top_half_rate": ">= 0.60",
            "opponents_beaten_rate": ">= 0.60",
            "bust_rate": "<= 0.20",
        },
        "consistent_beat_checkpoint": _consistent_checkpoint(rows),
        "best_checkpoint": _best_checkpoint(rows),
        "checkpoints": rows,
        "artifacts": {
            "output_dir": str(output_dir),
            "summary_json": str(output_dir / "summary.json"),
            "curve_csv": str(output_dir / "curve.csv"),
            "per_seed_results_jsonl": str(output_dir / "per_seed_results.jsonl"),
            "curve_png": str(output_dir / "curve.png"),
        },
        "duration_s": round(time.time() - started, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train and benchmark an abstract CFR checkpoint curve.")
    parser.add_argument("--max-iterations", type=int, default=200_000)
    parser.add_argument("--step-iterations", type=int, default=10_000)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--equity-samples", type=int, default=48)
    parser.add_argument("--epsilon", type=float, default=0.08)
    parser.add_argument("--min-epsilon", type=float, default=0.01)
    parser.add_argument("--benchmark-iterations", type=int, default=5)
    parser.add_argument("--hands", type=int, default=400)
    parser.add_argument("--seed", type=int, default=7000)
    parser.add_argument("--output-dir", default="bots/cfr/experiments/curve_200k")
    parser.add_argument("--opponents", nargs="+", default=DEFAULT_OPPONENTS)
    parser.add_argument("--train-log-interval", type=int, default=10_000)
    parser.add_argument("--no-validate-zips", dest="validate_zips", action="store_false")
    parser.set_defaults(validate_zips=True)
    args = parser.parse_args()

    if args.max_iterations < 0:
        raise SystemExit("--max-iterations must be >= 0")
    if args.step_iterations < 1:
        raise SystemExit("--step-iterations must be >= 1")
    if args.samples < 1:
        raise SystemExit("--samples must be >= 1")
    if args.equity_samples < 1:
        raise SystemExit("--equity-samples must be >= 1")
    if args.benchmark_iterations < 1:
        raise SystemExit("--benchmark-iterations must be >= 1")
    if args.hands < 1:
        raise SystemExit("--hands must be >= 1")

    summary = run_experiment(args)
    print(json.dumps({
        "summary_json": summary["artifacts"]["summary_json"],
        "curve_png": summary["artifacts"]["curve_png"],
        "consistent_beat_checkpoint": summary["consistent_beat_checkpoint"],
        "best_checkpoint": summary["best_checkpoint"],
    }, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
