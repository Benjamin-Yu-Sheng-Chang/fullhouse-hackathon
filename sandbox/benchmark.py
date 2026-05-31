"""Benchmark one bot version against another.

The benchmark answers two questions:
  1. Does the new version beat the old version directly?
  2. Does the new version perform better than the old version against fields?

Examples:
  python3 sandbox/benchmark.py
  python3 sandbox/benchmark.py --old equity_guard:v1 --new equity_guard:v2
  python3 sandbox/benchmark.py --old mybot:v3 --new mybot:v4 --opponents shark mathematician ref_bot_2
  python3 sandbox/benchmark.py --iterations 30 --hands 150 --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shlex
import shutil
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sandbox.compare import SETUPS, _resolve_bot_specs, _safe_name, _source_file_for_bot, _infer_version
from sandbox.match import run_match


DEFAULT_OLD = "equity_guard:v1"
DEFAULT_NEW = "equity_guard:v2"
DEFAULT_FIELD_SETUPS = {
    "passive": ["template", "mathematician", "ref_bot_2"],
    "reference": ["template", "mathematician", "ref_bot_2", "shark"],
    "aggressive": ["aggressor", "shark", "position_bully", "trap_steal", "anti_aggro"],
    "mixed": [
        "template",
        "mathematician",
        "ref_bot_2",
        "shark",
        "position_bully",
        "equity_position",
        "adaptive_hybrid",
    ],
}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_spec(token: str, role: str):
    resolved = _resolve_bot_specs([token])
    if len(resolved) != 1:
        raise SystemExit(f"--{role} must resolve to exactly one bot, got {list(resolved)}")
    bot_id, path = next(iter(resolved.items()))
    return bot_id, path


def _distinct_candidate_specs(old, new):
    if old[0] != new[0]:
        return old, new
    return old, (f"{new[0]}_new", new[1])


def _resolve_opponents(tokens: list[str]):
    resolved = _resolve_bot_specs(tokens)
    return [(bot_id, path) for bot_id, path in resolved.items()]


def _table_paths(candidate_id: str, candidate_path: str, opponents: list[tuple[str, str]]):
    paths = {candidate_id: candidate_path}
    for bot_id, path in opponents:
        if bot_id == candidate_id:
            bot_id = f"{bot_id}_opponent"
        suffix = 2
        base = bot_id
        while bot_id in paths:
            bot_id = f"{base}_{suffix}"
            suffix += 1
        paths[bot_id] = path
    return paths


def _rank_for(result: dict, bot_id: str):
    ordered = sorted(result["bot_ids"], key=lambda bid: -result["chip_delta"][bid])
    return ordered.index(bot_id) + 1


def _top_half(result: dict, bot_id: str):
    return _rank_for(result, bot_id) <= max(1, len(result["bot_ids"]) // 2)


def _mean(values):
    return sum(values) / max(1, len(values))


def _median(values):
    return statistics.median(values) if values else 0


def _summarize_candidate(results: list[dict], bot_id: str):
    deltas = [result["chip_delta"][bot_id] for result in results]
    places = [_rank_for(result, bot_id) for result in results]
    return {
        "total_delta": sum(deltas),
        "avg_delta": _mean(deltas),
        "median_delta": _median(deltas),
        "best_delta": max(deltas) if deltas else 0,
        "worst_delta": min(deltas) if deltas else 0,
        "avg_place": _mean(places),
        "top_half_rate": _mean([1 if _top_half(result, bot_id) else 0 for result in results]),
        "win_rate": _mean([1 if _rank_for(result, bot_id) == 1 else 0 for result in results]),
        "bust_rate": _mean([1 if result["final_stacks"][bot_id] <= 0 else 0 for result in results]),
    }


def _run_direct_contest(old, new, iterations: int, hands: int, seed_start: int):
    old_id, old_path = old
    new_id, new_path = new
    results = []

    for index in range(iterations):
        seed = seed_start + index
        result = run_match(
            f"benchmark_direct_{seed}",
            {old_id: old_path, new_id: new_path},
            n_hands=hands,
            seed=seed,
        )
        results.append(result)

    old_summary = _summarize_candidate(results, old_id)
    new_summary = _summarize_candidate(results, new_id)
    per_seed = [
        {
            "seed": seed_start + index,
            "old_delta": result["chip_delta"][old_id],
            "new_delta": result["chip_delta"][new_id],
            "improvement": result["chip_delta"][new_id] - result["chip_delta"][old_id],
            "old_place": _rank_for(result, old_id),
            "new_place": _rank_for(result, new_id),
        }
        for index, result in enumerate(results)
    ]
    improvements = [row["improvement"] for row in per_seed]
    return {
        "name": "direct_head_to_head",
        "old_bot_id": old_id,
        "new_bot_id": new_id,
        "old": old_summary,
        "new": new_summary,
        "paired": {
            "mean_improvement": _mean(improvements),
            "median_improvement": _median(improvements),
            "improvement_win_rate": _mean([1 if value > 0 else 0 for value in improvements]),
            "worst_improvement": min(improvements) if improvements else 0,
            "best_improvement": max(improvements) if improvements else 0,
        },
        "per_seed": per_seed,
    }


def _run_field_setup(name: str, old, new, opponents, iterations: int, hands: int, seed_start: int):
    old_id, old_path = old
    new_id, new_path = new
    old_results = []
    new_results = []

    for index in range(iterations):
        seed = seed_start + index
        old_results.append(
            run_match(
                f"benchmark_{name}_old_{seed}",
                _table_paths(old_id, old_path, opponents),
                n_hands=hands,
                seed=seed,
            )
        )
        new_results.append(
            run_match(
                f"benchmark_{name}_new_{seed}",
                _table_paths(new_id, new_path, opponents),
                n_hands=hands,
                seed=seed,
            )
        )

    old_summary = _summarize_candidate(old_results, old_id)
    new_summary = _summarize_candidate(new_results, new_id)
    per_seed = []
    for index, (old_result, new_result) in enumerate(zip(old_results, new_results)):
        old_delta = old_result["chip_delta"][old_id]
        new_delta = new_result["chip_delta"][new_id]
        per_seed.append(
            {
                "seed": seed_start + index,
                "old_delta": old_delta,
                "new_delta": new_delta,
                "improvement": new_delta - old_delta,
                "old_place": _rank_for(old_result, old_id),
                "new_place": _rank_for(new_result, new_id),
                "old_top_half": _top_half(old_result, old_id),
                "new_top_half": _top_half(new_result, new_id),
                "old_busted": old_result["final_stacks"][old_id] <= 0,
                "new_busted": new_result["final_stacks"][new_id] <= 0,
            }
        )

    improvements = [row["improvement"] for row in per_seed]
    return {
        "name": name,
        "opponents": [bot_id for bot_id, _path in opponents],
        "old_bot_id": old_id,
        "new_bot_id": new_id,
        "old": old_summary,
        "new": new_summary,
        "paired": {
            "mean_improvement": _mean(improvements),
            "median_improvement": _median(improvements),
            "improvement_win_rate": _mean([1 if value > 0 else 0 for value in improvements]),
            "worst_improvement": min(improvements) if improvements else 0,
            "best_improvement": max(improvements) if improvements else 0,
            "top_half_rate_change": new_summary["top_half_rate"] - old_summary["top_half_rate"],
            "bust_rate_change": new_summary["bust_rate"] - old_summary["bust_rate"],
            "avg_place_change": new_summary["avg_place"] - old_summary["avg_place"],
        },
        "per_seed": per_seed,
    }


def _acceptance(results: dict, min_mean_improvement: float, max_bust_rate_increase: float, max_bad_setups: int):
    field_setups = results["field_setups"]
    direct_ok = results["direct"]["paired"]["mean_improvement"] > 0
    improved_setups = [
        setup for setup in field_setups
        if setup["paired"]["mean_improvement"] >= min_mean_improvement
    ]
    bad_setups = [
        setup for setup in field_setups
        if setup["paired"]["mean_improvement"] < min_mean_improvement
    ]
    bust_regressions = [
        setup for setup in field_setups
        if setup["paired"]["bust_rate_change"] > max_bust_rate_increase
    ]
    passed = direct_ok and len(bad_setups) <= max_bad_setups and not bust_regressions
    return {
        "passed": passed,
        "direct_head_to_head_ok": direct_ok,
        "improved_setup_count": len(improved_setups),
        "regressed_setup_count": len(bad_setups),
        "bust_regression_count": len(bust_regressions),
        "regressed_setups": [setup["name"] for setup in bad_setups],
        "bust_regression_setups": [setup["name"] for setup in bust_regressions],
        "rules": {
            "min_mean_improvement": min_mean_improvement,
            "max_bust_rate_increase": max_bust_rate_increase,
            "max_bad_setups": max_bad_setups,
        },
    }


def _create_run_dir(args):
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = args.run_name or f"{timestamp}_benchmark_seed{args.seed}_h{args.hands}_i{args.iterations}"
    root = Path(args.runs_dir)
    if not root.is_absolute():
        root = ROOT / root
    candidate = root / _safe_name(base)
    suffix = 2
    while candidate.exists():
        candidate = root / f"{_safe_name(base)}_{suffix}"
        suffix += 1
    (candidate / "bots").mkdir(parents=True)
    return candidate


def _snapshot_bots(run_dir: Path, bot_paths: dict[str, str]):
    snapshots = {}
    for bot_id, path_text in bot_paths.items():
        source = _source_file_for_bot(path_text)
        snapshot_path = run_dir / "bots" / f"{_safe_name(bot_id)}.pybak"
        shutil.copyfile(source, snapshot_path)
        inferred = _infer_version(path_text)
        snapshots[bot_id] = {
            "bot_id": bot_id,
            "type": inferred["type"],
            "version": inferred["version"],
            "source_path": str(source),
            "snapshot_path": str(snapshot_path),
            "sha256": _file_sha256(snapshot_path),
        }
    return snapshots


def _write_artifacts(run_dir: Path, args, results: dict, bot_paths: dict[str, str]):
    snapshots = _snapshot_bots(run_dir, bot_paths)
    old_snapshot = shlex.quote(snapshots[results["old_bot_id"]]["snapshot_path"])
    new_snapshot = shlex.quote(snapshots[results["new_bot_id"]]["snapshot_path"])
    opponent_snapshots = [
        shlex.quote(info["snapshot_path"])
        for bot_id, info in snapshots.items()
        if bot_id not in (results["old_bot_id"], results["new_bot_id"])
    ]
    replay_parts = [
        "python3 sandbox/benchmark.py",
        "--old", old_snapshot,
        "--new", new_snapshot,
        "--iterations", str(args.iterations),
        "--hands", str(args.hands),
        "--seed", str(args.seed),
        "--no-save-run",
    ]
    if opponent_snapshots:
        replay_parts.extend(["--opponents", *opponent_snapshots])
    manifest = {
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "metric_definitions": metric_definitions(),
        "bot_sources": snapshots,
        "replay_command": " ".join(replay_parts),
        "settings": {
            "old": args.old,
            "new": args.new,
            "iterations": args.iterations,
            "hands": args.hands,
            "seed_start": args.seed,
            "seed_end": args.seed + args.iterations - 1,
            "setups": args.setup,
            "opponents": args.opponents,
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (run_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")


def metric_definitions():
    return {
        "direct_head_to_head": "Old and new play at the same table over identical seeds.",
        "paired_field_eval": "Old and new separately face the same opponent setup over identical seeds.",
        "mean_improvement": "Average per-seed candidate chip delta difference: new_delta - old_delta.",
        "improvement_win_rate": "Fraction of seeds where new_delta is greater than old_delta.",
        "top_half_rate": "Fraction of matches where candidate finished in the top half of the table.",
        "bust_rate": "Fraction of matches where candidate final stack was zero.",
        "avg_place": "Average finishing position; lower is better.",
        "worst_delta": "Worst candidate chip delta in a single match; measures downside.",
        "regressed_setup": "A setup whose mean_improvement is below the configured threshold.",
    }


def _print_metric_definitions():
    print("\nMetric Definitions")
    print("-" * 80)
    for name, description in metric_definitions().items():
        print(f"{name}: {description}")


def _print_setup_result(setup):
    paired = setup["paired"]
    old = setup["old"]
    new = setup["new"]
    print(f"\n{setup['name']}")
    if "opponents" in setup:
        print("opponents: " + ", ".join(setup["opponents"]))
    print("-" * 104)
    print(
        "Bot".ljust(18)
        + "Total".rjust(10)
        + "Avg".rjust(10)
        + "Place".rjust(8)
        + "TopHalf".rjust(10)
        + "Bust".rjust(8)
        + "Best".rjust(10)
        + "Worst".rjust(10)
        + "WinRate".rjust(10)
    )
    for label, row in [(setup["old_bot_id"], old), (setup["new_bot_id"], new)]:
        print(
            label.ljust(18)
            + f"{row['total_delta']:>+10.0f}"
            + f"{row['avg_delta']:>+10.1f}"
            + f"{row['avg_place']:>8.2f}"
            + f"{row['top_half_rate']:>10.2f}"
            + f"{row['bust_rate']:>8.2f}"
            + f"{row['best_delta']:>+10.0f}"
            + f"{row['worst_delta']:>+10.0f}"
            + f"{row['win_rate']:>10.2f}"
        )
    print(
        "paired: "
        f"mean_improvement={paired['mean_improvement']:+.1f}, "
        f"median={paired['median_improvement']:+.1f}, "
        f"win_rate={paired['improvement_win_rate']:.2f}, "
        f"worst={paired['worst_improvement']:+.0f}"
    )


def _print_report(results: dict):
    print(
        f"Benchmark: {results['old_bot_id']} -> {results['new_bot_id']}  "
        f"matches/setup={results['iterations']}  hands={results['hands']}  "
        f"seeds={results['seed_start']}..{results['seed_start'] + results['iterations'] - 1}"
    )
    _print_metric_definitions()
    _print_setup_result(results["direct"])
    for setup in results["field_setups"]:
        _print_setup_result(setup)

    gate = results["acceptance"]
    status = "PASS" if gate["passed"] else "FAIL"
    print(f"\nAcceptance: {status}")
    print(
        f"direct_ok={gate['direct_head_to_head_ok']}  "
        f"improved_setups={gate['improved_setup_count']}  "
        f"regressed_setups={gate['regressed_setup_count']}  "
        f"bust_regressions={gate['bust_regression_count']}"
    )
    if gate["regressed_setups"]:
        print("regressed: " + ", ".join(gate["regressed_setups"]))
    if gate["bust_regression_setups"]:
        print("bust regressions: " + ", ".join(gate["bust_regression_setups"]))


def _setup_specs(args):
    if args.opponents:
        return {"custom_field": args.opponents}
    if args.setup:
        specs = {}
        for setup_name in args.setup:
            if setup_name in DEFAULT_FIELD_SETUPS:
                specs[setup_name] = DEFAULT_FIELD_SETUPS[setup_name]
            elif setup_name in SETUPS:
                specs[setup_name] = SETUPS[setup_name]
            else:
                raise SystemExit(f"Unknown setup: {setup_name}")
        return specs
    return DEFAULT_FIELD_SETUPS


def main():
    parser = argparse.ArgumentParser(description="Benchmark a new bot version against an old version")
    parser.add_argument("--old", default=DEFAULT_OLD, help="Old bot token/path, e.g. equity_guard:v1")
    parser.add_argument("--new", default=DEFAULT_NEW, help="New bot token/path, e.g. equity_guard:v2")
    parser.add_argument("--opponents", nargs="+", help="Custom opponent bot tokens/paths for one field setup.")
    parser.add_argument("--setup", action="append", help="Named setup to run. Defaults to passive/reference/aggressive/mixed.")
    parser.add_argument("--iterations", type=int, default=10, help="Number of paired seeds per setup.")
    parser.add_argument("--hands", type=int, default=100, help="Hands per match.")
    parser.add_argument("--seed", type=int, default=1, help="First seed. Iteration N uses seed + N.")
    parser.add_argument("--min-mean-improvement", type=float, default=0.0)
    parser.add_argument("--max-bust-rate-increase", type=float, default=0.10)
    parser.add_argument("--max-bad-setups", type=int, default=0)
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--no-save-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.iterations < 1:
        raise SystemExit("--iterations must be at least 1")
    if args.hands < 1:
        raise SystemExit("--hands must be at least 1")

    old, new = _distinct_candidate_specs(
        _candidate_spec(args.old, "old"),
        _candidate_spec(args.new, "new"),
    )
    setup_tokens = _setup_specs(args)
    field_setups = []
    all_bot_paths = {old[0]: old[1], new[0]: new[1]}

    direct = _run_direct_contest(old, new, args.iterations, args.hands, args.seed)
    for offset, (name, opponent_tokens) in enumerate(setup_tokens.items(), start=1):
        opponents = _resolve_opponents(opponent_tokens)
        for bot_id, path in opponents:
            all_bot_paths.setdefault(bot_id, path)
        field_setups.append(
            _run_field_setup(
                name,
                old,
                new,
                opponents,
                args.iterations,
                args.hands,
                args.seed + offset * 100000,
            )
        )

    results = {
        "old_bot_id": old[0],
        "new_bot_id": new[0],
        "iterations": args.iterations,
        "hands": args.hands,
        "seed_start": args.seed,
        "metric_definitions": metric_definitions(),
        "direct": direct,
        "field_setups": field_setups,
    }
    results["acceptance"] = _acceptance(
        results,
        args.min_mean_improvement,
        args.max_bust_rate_increase,
        args.max_bad_setups,
    )

    run_dir = None
    if not args.no_save_run:
        run_dir = _create_run_dir(args)
        _write_artifacts(run_dir, args, results, all_bot_paths)
        results["run_dir"] = str(run_dir)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_report(results)
        if run_dir:
            print(f"\nSaved benchmark: {run_dir}")
            print(f"Artifacts: {run_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()
