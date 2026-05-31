"""Compare Fullhouse bots across seeded local match setups.

Examples:
  python3 sandbox/compare.py --setup all --iterations 5 --hands 100
  python3 sandbox/compare.py --bots shark equity_guard position_bully --iterations 10
  python3 sandbox/compare.py --bots equity_guard:v1 equity_guard:v2 --iterations 10
  python3 sandbox/compare.py --bots equity_guard:* --iterations 10
  python3 sandbox/compare.py --setup references --setup new --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shlex
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sandbox.match import run_match


BOT_ALIASES = {
    "math": "mathematician",
    "potodds": "ref_bot_2",
}

SETUPS = {
    "references": ["template", "aggressor", "mathematician", "shark", "ref_bot_2"],
    "new": ["equity_guard", "position_bully", "trap_steal"],
    "generated": ["equity_position", "anti_aggro", "short_stack_survivor", "adaptive_hybrid"],
    "equity_versions": ["equity_guard:*"],
    "all": [
        "template",
        "aggressor",
        "mathematician",
        "shark",
        "ref_bot_2",
        "equity_guard",
        "position_bully",
        "trap_steal",
        "equity_position",
        "anti_aggro",
        "short_stack_survivor",
        "adaptive_hybrid",
    ],
    "anti_aggressor": ["aggressor", "shark", "equity_guard", "trap_steal"],
    "passive_table": ["template", "mathematician", "ref_bot_2", "position_bully", "equity_guard", "equity_position"],
}


def _version_sort_key(version: str):
    parts = re.split(r"(\d+)", version)
    key = []
    for part in parts:
        if part.isdigit():
            key.append((1, int(part)))
        elif part:
            key.append((0, part))
    return key


def _discover_versioned_bots():
    discovered = {}
    for bot_py in sorted((ROOT / "bots").glob("*/*/bot.py")):
        bot_type = bot_py.parents[1].name
        version = bot_py.parent.name
        discovered.setdefault(bot_type, {})[version] = str(bot_py.parent)
    return discovered


def _latest_version(versions: dict[str, str]) -> str:
    return sorted(versions, key=_version_sort_key)[-1]


def _bot_id_from_path(path_text: str, fallback_index: int) -> str:
    path = Path(path_text)
    if path.suffix in (".py", ".pybak", ".zip"):
        bot_id = path.stem
        if bot_id == "bot":
            bot_id = path.parent.name
    else:
        bot_id = path.name
    return bot_id or f"bot_{fallback_index}"


def _split_version_token(token: str):
    if ":" in token:
        return token.split(":", 1)
    if "@" in token:
        return token.split("@", 1)
    return token, None


def _expand_bot_token(token: str, versioned_bots: dict[str, dict[str, str]], index: int):
    if token in BOT_ALIASES:
        token = BOT_ALIASES[token]

    bot_type, requested_version = _split_version_token(token)
    if bot_type in versioned_bots:
        versions = versioned_bots[bot_type]
        if requested_version in (None, "", "latest"):
            version = _latest_version(versions)
            return [(f"{bot_type}_{version}", versions[version])]
        if requested_version == "*":
            return [(f"{bot_type}_{version}", versions[version]) for version in sorted(versions, key=_version_sort_key)]
        if requested_version in versions:
            return [(f"{bot_type}_{requested_version}", versions[requested_version])]
        available = ", ".join(sorted(versions, key=_version_sort_key))
        raise FileNotFoundError(f"Unknown version for {bot_type}: {requested_version}. Available: {available}")

    path = Path(token)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"Unknown bot, versioned bot, or missing path: {token}")
    return [(_bot_id_from_path(str(path), index), str(path))]


def _resolve_bot_specs(tokens: list[str]) -> dict[str, str]:
    versioned_bots = _discover_versioned_bots()
    paths = {}
    for index, token in enumerate(tokens):
        for base_id, path_text in _expand_bot_token(token, versioned_bots, index):
            bot_id = base_id
            suffix = 2
            while bot_id in paths:
                bot_id = f"{base_id}_{suffix}"
                suffix += 1
            paths[bot_id] = path_text
    return paths


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "run"


def _source_file_for_bot(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_dir():
        return path / "bot.py"
    return path


def _infer_version(path_text: str):
    path = Path(path_text)
    if path.is_dir():
        bot_py = path / "bot.py"
    else:
        bot_py = path

    try:
        relative = bot_py.resolve().relative_to((ROOT / "bots").resolve())
    except ValueError:
        return {"type": None, "version": None}

    parts = relative.parts
    if len(parts) == 2 and parts[1] == "bot.py":
        return {"type": parts[0], "version": "legacy"}
    if len(parts) == 3 and parts[2] == "bot.py":
        return {"type": parts[0], "version": parts[1]}
    return {"type": None, "version": None}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_run_dir(setup_specs, args):
    requested = []
    for name, bot_tokens in setup_specs:
        requested.append(name if name != "custom" else "custom")
    label = _safe_name("_".join(requested))
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_name = args.run_name or f"{timestamp}_{label}_seed{args.seed}_h{args.hands}_i{args.iterations}"
    run_dir = Path(args.runs_dir)
    if not run_dir.is_absolute():
        run_dir = ROOT / run_dir

    candidate = run_dir / _safe_name(base_name)
    suffix = 2
    while candidate.exists():
        candidate = run_dir / f"{_safe_name(base_name)}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    (candidate / "bots").mkdir()
    return candidate


def _snapshot_sources(run_dir: Path, reports: list[dict]) -> dict[str, dict]:
    snapshots = {}
    for report in reports:
        for bot_id, path_text in report["bot_paths"].items():
            if bot_id in snapshots:
                continue
            source = _source_file_for_bot(path_text)
            if not source.is_file():
                raise FileNotFoundError(f"Could not snapshot bot source for {bot_id}: {source}")

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


def _write_run_artifacts(run_dir: Path, args, setup_specs, reports: list[dict]) -> None:
    snapshots = _snapshot_sources(run_dir, reports)
    snapshot_args = " ".join(shlex.quote(str(Path(info["snapshot_path"]))) for info in snapshots.values())
    replay_command = (
        f"python3 sandbox/compare.py --bots {snapshot_args} "
        f"--iterations {args.iterations} --hands {args.hands} --seed {args.seed} --no-save-run"
    )

    manifest = {
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "iterations": args.iterations,
        "hands": args.hands,
        "seed_start": args.seed,
        "seed_end": args.seed + args.iterations - 1,
        "json_output_requested": args.json,
        "requested_setups": [
            {"name": name, "bot_tokens": bot_tokens}
            for name, bot_tokens in setup_specs
        ],
        "bot_sources": snapshots,
        "replay_command": replay_command,
    }

    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (run_dir / "results.json").write_text(json.dumps(reports, indent=2) + "\n")


def _empty_stats():
    return {
        "matches": 0,
        "total_delta": 0,
        "best_delta": None,
        "worst_delta": None,
        "wins": 0,
        "top_half": 0,
        "busts": 0,
        "errors": 0,
        "placements": [],
    }


def _run_setup(name: str, bot_tokens: list[str], iterations: int, hands: int, seed: int):
    bot_paths = _resolve_bot_specs(bot_tokens)
    stats = {bot_id: _empty_stats() for bot_id in bot_paths}
    matches = []

    for index in range(iterations):
        match_seed = seed + index
        result = run_match(
            f"compare_{name}_{match_seed}",
            bot_paths,
            n_hands=hands,
            seed=match_seed,
        )
        deltas = result["chip_delta"]
        ordered = sorted(result["bot_ids"], key=lambda bot_id: -deltas[bot_id])
        top_half_cutoff = max(1, len(ordered) // 2)

        for place, bot_id in enumerate(ordered, start=1):
            bot_stats = stats[bot_id]
            delta = deltas[bot_id]
            bot_stats["matches"] += 1
            bot_stats["total_delta"] += delta
            bot_stats["placements"].append(place)
            bot_stats["best_delta"] = delta if bot_stats["best_delta"] is None else max(bot_stats["best_delta"], delta)
            bot_stats["worst_delta"] = delta if bot_stats["worst_delta"] is None else min(bot_stats["worst_delta"], delta)
            if place == 1:
                bot_stats["wins"] += 1
            if place <= top_half_cutoff:
                bot_stats["top_half"] += 1
            if result["final_stacks"][bot_id] <= 0:
                bot_stats["busts"] += 1
            if result["bot_errors"].get(bot_id):
                bot_stats["errors"] += len(result["bot_errors"][bot_id])

        matches.append(
            {
                "seed": match_seed,
                "hands_played": result["n_hands"],
                "duration_s": result["duration_s"],
                "chip_delta": deltas,
                "final_stacks": result["final_stacks"],
            }
        )

    summary = []
    for bot_id, bot_stats in stats.items():
        matches_played = max(1, bot_stats["matches"])
        summary.append(
            {
                "bot_id": bot_id,
                "matches": bot_stats["matches"],
                "total_delta": bot_stats["total_delta"],
                "avg_delta": bot_stats["total_delta"] / matches_played,
                "avg_place": sum(bot_stats["placements"]) / matches_played,
                "wins": bot_stats["wins"],
                "top_half": bot_stats["top_half"],
                "busts": bot_stats["busts"],
                "best_delta": bot_stats["best_delta"],
                "worst_delta": bot_stats["worst_delta"],
                "errors": bot_stats["errors"],
            }
        )

    summary.sort(key=lambda row: (-row["total_delta"], row["avg_place"], -row["wins"], row["bot_id"]))
    return {
        "setup": name,
        "bots": list(bot_paths),
        "bot_paths": bot_paths,
        "iterations": iterations,
        "hands_per_match": hands,
        "seed_start": seed,
        "summary": summary,
        "matches": matches,
    }


def _print_report(report):
    print(f"\nSetup: {report['setup']}")
    print(
        f"matches={report['iterations']}  hands={report['hands_per_match']}  "
        f"seeds={report['seed_start']}..{report['seed_start'] + report['iterations'] - 1}"
    )
    print("-" * 104)
    print(
        "Bot".ljust(18)
        + "Total".rjust(10)
        + "Avg".rjust(10)
        + "Place".rjust(8)
        + "Wins".rjust(7)
        + "TopHalf".rjust(9)
        + "Busts".rjust(8)
        + "Best".rjust(10)
        + "Worst".rjust(10)
        + "Errors".rjust(8)
    )
    print("-" * 104)
    for row in report["summary"]:
        print(
            row["bot_id"].ljust(18)
            + f"{row['total_delta']:>+10.0f}"
            + f"{row['avg_delta']:>+10.1f}"
            + f"{row['avg_place']:>8.2f}"
            + f"{row['wins']:>7}"
            + f"{row['top_half']:>9}"
            + f"{row['busts']:>8}"
            + f"{row['best_delta']:>+10.0f}"
            + f"{row['worst_delta']:>+10.0f}"
            + f"{row['errors']:>8}"
        )


def main():
    parser = argparse.ArgumentParser(description="Compare Fullhouse bots across seeded match setups")
    parser.add_argument("--setup", action="append", choices=sorted(SETUPS), help="Named setup to run. Can be repeated.")
    parser.add_argument("--bots", nargs="+", help="Custom bot names or paths. Overrides --setup when provided.")
    parser.add_argument("--iterations", type=int, default=5, help="Number of seeded matches per setup.")
    parser.add_argument("--hands", type=int, default=100, help="Hands per match.")
    parser.add_argument("--seed", type=int, default=1, help="First seed. Iteration N uses seed + N.")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR), help="Directory where reproducible run artifacts are saved.")
    parser.add_argument("--run-name", default=None, help="Optional name for this saved run folder.")
    parser.add_argument("--no-save-run", action="store_true", help="Do not write a reproducibility snapshot under runs/.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of tables.")
    args = parser.parse_args()

    if args.iterations < 1:
        raise SystemExit("--iterations must be at least 1")
    if args.hands < 1:
        raise SystemExit("--hands must be at least 1")

    if args.bots:
        setup_specs = [("custom", args.bots)]
    else:
        setup_names = args.setup or ["all"]
        setup_specs = [(name, SETUPS[name]) for name in setup_names]

    reports = [
        _run_setup(name, bot_tokens, args.iterations, args.hands, args.seed)
        for name, bot_tokens in setup_specs
    ]

    run_dir = None
    if not args.no_save_run:
        run_dir = _create_run_dir(setup_specs, args)
        _write_run_artifacts(run_dir, args, setup_specs, reports)

    if args.json:
        print(json.dumps({"run_dir": str(run_dir) if run_dir else None, "reports": reports}, indent=2))
    else:
        for report in reports:
            _print_report(report)
        if run_dir:
            print(f"\nSaved run: {run_dir}")
            print(f"Artifacts: {run_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()
