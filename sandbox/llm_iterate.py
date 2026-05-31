"""Offline DeepSeek-assisted bot iteration loop.

This script is for development only. It may call the DeepSeek API while
generating candidate bot code, but generated bots must remain standalone and
must not call any network/API at gameplay time.

Example:
  DEEPSEEK_API_KEY=... python3 sandbox/llm_iterate.py \
    --base short_stack_survivor:v1 \
    --new-version v2 \
    --goal "Blend in a little equity_guard_v2 aggression without increasing bust rate."
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import ssl
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HL_RUNS_DIR = ROOT / "hl_runs"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sandbox.compare import _infer_version, _resolve_bot_specs, _safe_name, _source_file_for_bot
from sandbox.match import run_match


DEFAULT_GOAL = (
    "Create a stronger heuristic poker bot version. Improve paired-field benchmark "
    "performance while avoiding higher bust rate. Keep the code readable, standalone, "
    "fast under the 2-second action limit, and validator-safe."
)

ELIMINATION_BASELINES = ["mathematician", "ref_bot_2", "aggressor", "shark"]
FULL_SELECTION_BASELINES = [
    "mathematician",
    "ref_bot_2",
    "aggressor",
    "shark",
    "equity_guard",
    "position_bully",
    "trap_steal",
    "short_stack_survivor",
]
MAX_TABLE_BOTS = 9
CURVE_EXCLUDED_TRAINING_PREFIXES = ("shark", "ref_bot_2")
FINALIST_BASELINES = [
    "equity_guard",
    "position_bully",
    "trap_steal",
    "short_stack_survivor",
    "adaptive_hybrid",
]
CATASTROPHIC_MEAN_IMPROVEMENT = -1000.0
LOG_PATH: Path | None = None


def _log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    if LOG_PATH is not None:
        with LOG_PATH.open("a") as handle:
            handle.write(line + "\n")


def _shell_command(argv: list[str]) -> str:
    parts = list(argv)
    if parts and parts[0].endswith(".py"):
        parts = [Path(sys.executable).name, *parts]
    return " ".join(shlex.quote(part) for part in parts)


def _initialize_run_log(run_dir: Path, argv: list[str]) -> None:
    global LOG_PATH
    LOG_PATH = run_dir / "run.log"
    command = _shell_command(argv)
    (run_dir / "command.txt").write_text(command + "\n")
    (run_dir / "argv.json").write_text(json.dumps(argv, indent=2) + "\n")
    LOG_PATH.write_text(
        "\n".join(
            [
                f"# LLM iterate run log",
                f"created_at_utc={dt.datetime.now(dt.timezone.utc).isoformat()}",
                f"cwd={ROOT}",
                f"command={command}",
                "",
            ]
        )
    )


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _run(cmd: list[str], cwd: Path = ROOT, timeout: int | None = None) -> dict:
    started = time.time()
    _log("Running command: " + _shell_command(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_s": round(time.time() - started, 3),
    }
    _log(f"Command finished rc={proc.returncode} duration_s={result['duration_s']}")
    if proc.stderr.strip():
        _log("Command stderr: " + proc.stderr.strip()[:2000])
    return result


def _resolve_one(token: str) -> tuple[str, str]:
    resolved = _resolve_bot_specs([token])
    if len(resolved) != 1:
        raise SystemExit(f"Expected one bot from {token!r}, got {list(resolved)}")
    return next(iter(resolved.items()))


def _next_version(bot_type: str, run_root: Path | None = None) -> str:
    root = ROOT / "bots" / bot_type
    existing = []
    if root.exists():
        for child in root.iterdir():
            match = re.fullmatch(r"v(\d+)", child.name)
            if child.is_dir() and match:
                existing.append(int(match.group(1)))
    if run_root is not None:
        candidate_root = run_root / "bots" / "candidates" / bot_type
        if candidate_root.exists():
            for child in candidate_root.iterdir():
                match = re.fullmatch(r"v(\d+)", child.name)
                if child.is_dir() and match:
                    existing.append(int(match.group(1)))
    return f"v{max(existing, default=0) + 1}"


def _new_run_hex(base_id: str, new_token: str, goal: str, timestamp: str) -> str:
    nonce = os.urandom(8).hex()
    material = f"{timestamp}|{base_id}|{new_token}|{goal}|{nonce}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def _create_run_dir(name: str | None, base_id: str, new_token: str, hl_runs_dir: Path) -> Path:
    now = dt.datetime.now(dt.timezone.utc)
    timestamp = now.strftime("%y%m%d_%H%M%S")
    run_hex = _new_run_hex(base_id, new_token, name or "", now.isoformat())
    label = name or f"llm_{base_id}_to_{new_token}"
    base = f"{timestamp}_{run_hex}_{label}"
    candidate = hl_runs_dir / _safe_name(base)
    suffix = 2
    while candidate.exists():
        candidate = hl_runs_dir / f"{_safe_name(base)}_{suffix}"
        suffix += 1
    (candidate / "attempts").mkdir(parents=True)
    (candidate / "artifacts" / "selection").mkdir(parents=True)
    (candidate / "artifacts" / "curve").mkdir(parents=True)
    (candidate / "bots" / "baselines").mkdir(parents=True)
    (candidate / "bots" / "candidates").mkdir(parents=True)
    (candidate / "RUN_ID").write_text(run_hex + "\n")
    return candidate


def _append_run_index(run_dir: Path, manifest: dict) -> None:
    run_id = (run_dir / "RUN_ID").read_text().strip()
    entry = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "created_at_utc": manifest["created_at_utc"],
        "base": manifest["base"]["token"],
        "candidate": manifest["candidate"]["token"],
        "run_name": manifest["settings"].get("run_name"),
    }
    index_path = run_dir.parent / "index.jsonl"
    with index_path.open("a") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _bot_id_from_bot_py(bot_py: Path) -> str:
    relative = bot_py.resolve().relative_to((ROOT / "bots").resolve())
    parts = relative.parts
    if len(parts) == 2 and parts[1] == "bot.py":
        return parts[0]
    if len(parts) == 3 and parts[2] == "bot.py":
        return f"{parts[0]}_{parts[1]}"
    return _safe_name("_".join(parts[:-1]))


def _copy_all_baselines(run_dir: Path) -> dict[str, dict]:
    snapshots = {}
    seen = set()
    bot_files = sorted((ROOT / "bots").glob("*/bot.py")) + sorted((ROOT / "bots").glob("*/*/bot.py"))
    for bot_py in bot_files:
        resolved = bot_py.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        bot_id = _bot_id_from_bot_py(bot_py)
        target_dir = run_dir / "bots" / "baselines" / bot_id
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bot_py, target_dir / "bot.py")
        shutil.copyfile(bot_py, target_dir / "bot.pybak")
        snapshots[bot_id] = {
            "source_path": str(bot_py),
            "bot_dir": str(target_dir),
            "bot_py": str(target_dir / "bot.py"),
            "pybak": str(target_dir / "bot.pybak"),
        }
    return snapshots


def _ensure_base_snapshot(run_dir: Path, snapshots: dict[str, dict], bot_id: str, source_path: str) -> str:
    if bot_id in snapshots:
        return snapshots[bot_id]["bot_dir"]
    source = _source_file_for_bot(source_path)
    target_dir = run_dir / "bots" / "baselines" / _safe_name(bot_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target_dir / "bot.py")
    shutil.copyfile(source, target_dir / "bot.pybak")
    snapshots[bot_id] = {
        "source_path": str(source),
        "bot_dir": str(target_dir),
        "bot_py": str(target_dir / "bot.py"),
        "pybak": str(target_dir / "bot.pybak"),
    }
    return str(target_dir)


def _read_optional(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _mean(values: list[float]) -> float:
    return sum(values) / max(1, len(values))


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _rank_for_match(result: dict, bot_id: str) -> int:
    ordered = sorted(result["bot_ids"], key=lambda bid: -result["chip_delta"][bid])
    return ordered.index(bot_id) + 1


def _top_half_for_match(result: dict, bot_id: str) -> bool:
    return _rank_for_match(result, bot_id) <= max(1, len(result["bot_ids"]) // 2)


def _load_recent_metrics(paths: list[str]) -> str:
    chunks = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        if path.is_dir():
            path = path / "results.json"
        if path.exists():
            chunks.append(f"### Metrics from {path}\n{path.read_text()[:30000]}")
    return "\n\n".join(chunks)


def _extract_python_code(text: str) -> str:
    fenced = re.findall(r"```(?:python)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced[-1].strip() + "\n"
    return text.strip() + "\n"


def _ssl_context(insecure_ssl: bool):
    if insecure_ssl:
        return ssl._create_unverified_context()
    try:
        import certifi
    except Exception:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _deepseek_chat(
    messages: list[dict],
    api_key: str,
    model: str,
    endpoint: str,
    temperature: float,
    insecure_ssl: bool = False,
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120, context=_ssl_context(insecure_ssl)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek HTTP {exc.code}: {body}") from exc
    return data["choices"][0]["message"]["content"]


def _system_prompt() -> str:
    return (
        "You are improving a no-limit Texas Hold'em bot for a local hackathon engine. "
        "Return exactly one complete standalone Python bot.py. Do not include prose. "
        "At the top of the file, include a short comment block headed 'STRATEGY OVERVIEW' "
        "that explains the bot's core plan, key thresholds, and expected weaknesses. "
        "Do not cheat by copying another bot's strategy, thresholds, structure, or comments "
        "wholesale; use the base bot only as improvement context and express a materially "
        "distinct heuristic mix. "
        "The bot must define decide(state). It may import stdlib random/math and allowed "
        "libraries such as eval7/numpy, but must not import network, subprocess, threading, "
        "asyncio, pickle, importlib, or other forbidden modules. It must not read/write files "
        "during gameplay. It must respond quickly under 2 seconds per action."
    )


def _initial_prompt(
    goal: str,
    base_token: str,
    new_token: str,
    base_source: str,
    strategy_docs: str,
    iteration_docs: str,
    metrics: str,
) -> str:
    return f"""Goal:
{goal}

Base bot token: {base_token}
Candidate token to create: {new_token}

Strategy component notes:
{strategy_docs[:16000]}

Fullhouse bot API and LLM iteration guide:
{iteration_docs[:20000]}

Recent benchmark metrics:
{metrics or "No additional metrics provided."}

Current bot.py:
```python
{base_source}
```

Hard requirements:
- Return a complete standalone bot.py only.
- Include a top-of-file comment block headed "STRATEGY OVERVIEW" with the plan, key thresholds, and likely weaknesses.
- Do not cheat: do not copy another bot's strategy, thresholds, structure, or comments wholesale.
- Use the current bot as improvement context only; the candidate must describe its distinct strategic idea in STRATEGY OVERVIEW.
- Common poker concepts such as pot odds, equity, position, stack pressure, and opponent profiling are allowed when expressed as a materially different heuristic mix.
- Preserve the Fullhouse return format: fold/check/call/raise/all_in.
- Do not call external APIs or import forbidden runtime modules.
- Optimize for benchmark.py acceptance: direct head-to-head positive, paired field improvement, no bust-rate regression.
- Prefer small, interpretable heuristic changes over giant rewrites.
"""


def _repair_prompt(error_report: str, candidate_source: str, iteration_docs: str) -> str:
    return f"""The candidate failed local validation or compilation.

Fullhouse bot API and LLM iteration guide:
{iteration_docs[:20000]}

Error report:
{error_report}

Candidate source:
```python
{candidate_source}
```

Return a corrected complete standalone bot.py only. Do not add prose. Preserve the anti-copying requirement: do not copy another bot wholesale.
"""


def _metrics_prompt(metrics: str, candidate_source: str, iteration_docs: str) -> str:
    return f"""The candidate passed validation but benchmark metrics were not good enough.

Fullhouse bot API and LLM iteration guide:
{iteration_docs[:20000]}

Benchmark metrics:
{metrics}

Candidate source:
```python
{candidate_source}
```

Revise the bot to improve benchmark acceptance. Focus on the regressed setups and bust-rate issues.
Do not copy another bot wholesale; keep the strategy overview honest about the candidate's distinct heuristic mix.
Return a complete standalone bot.py only. Do not add prose.
"""


def _write_attempt(run_dir: Path, attempt: int, prompt: str, response: str, source: str) -> Path:
    attempt_dir = run_dir / "attempts" / f"attempt_{attempt:02d}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "prompt.txt").write_text(prompt)
    (attempt_dir / "response.txt").write_text(response)
    (attempt_dir / "candidate.py").write_text(source)
    return attempt_dir


def _candidate_attempt_dir(run_dir: Path, new_type: str, new_version: str, round_index: int, attempt: int) -> Path:
    bot_dir = run_dir / "bots" / "candidates" / new_type / new_version / f"round_{round_index:02d}_attempt_{attempt:02d}"
    bot_dir.mkdir(parents=True, exist_ok=True)
    return bot_dir


def _latest_candidate_dir(run_dir: Path, new_type: str, new_version: str) -> Path:
    bot_dir = run_dir / "bots" / "candidates" / new_type / new_version / "latest"
    bot_dir.mkdir(parents=True, exist_ok=True)
    return bot_dir


def _validate_candidate(candidate_path: Path) -> dict:
    compile_result = _run([sys.executable, "-m", "py_compile", str(candidate_path)])
    validator_result = None
    validator_json = None
    if compile_result["returncode"] == 0:
        validator_result = _run([sys.executable, "sandbox/validator.py", str(candidate_path.parent), "--json"], timeout=60)
        if validator_result["stdout"].strip():
            try:
                validator_json = json.loads(validator_result["stdout"])
            except json.JSONDecodeError:
                validator_json = None
    source = candidate_path.read_text() if candidate_path.exists() else ""
    overview_ok = "STRATEGY OVERVIEW" in source[:2500]
    overview_error = None if overview_ok else "Missing top-of-file STRATEGY OVERVIEW comment block."
    passed = compile_result["returncode"] == 0 and validator_result and validator_result["returncode"] == 0 and overview_ok
    if validator_json is not None:
        passed = passed and bool(validator_json.get("passed"))
    return {
        "passed": passed,
        "compile": compile_result,
        "validator": validator_result,
        "validator_json": validator_json,
        "strategy_overview_ok": overview_ok,
        "strategy_overview_error": overview_error,
    }


def _run_benchmark(
    base_path: str,
    new_path: str,
    iterations: int,
    hands: int,
    seed: int,
    run_name: str,
    timeout: int,
    max_bad_setups: int | None,
    opponents: list[str] | None = None,
) -> dict:
    cmd = [
        sys.executable,
        "sandbox/benchmark.py",
        "--old",
        base_path,
        "--new",
        new_path,
        "--iterations",
        str(iterations),
        "--hands",
        str(hands),
        "--seed",
        str(seed),
        "--run-name",
        run_name,
        "--no-save-run",
        "--json",
    ]
    if opponents:
        cmd.extend(["--opponents", *opponents])
    if max_bad_setups is not None:
        cmd.extend(["--max-bad-setups", str(max_bad_setups)])
    result = _run(cmd, timeout=timeout)
    parsed = None
    if result["stdout"].strip():
        try:
            parsed = json.loads(result["stdout"])
        except json.JSONDecodeError:
            parsed = None
    return {"command": result, "json": parsed}


def _benchmark(base_path: str, new_path: str, args: argparse.Namespace, run_dir: Path) -> dict:
    return _run_benchmark(
        base_path=base_path,
        new_path=new_path,
        iterations=args.benchmark_iterations,
        hands=args.hands,
        seed=args.seed,
        run_name=f"{run_dir.name}_benchmark",
        timeout=args.benchmark_timeout,
        max_bad_setups=args.max_bad_setups,
    )


def _resolve_baseline_opponents(tokens: list[str], snapshots: dict[str, dict], base_id: str) -> tuple[list[str], list[dict]]:
    opponents = []
    skipped = []
    seen = set()
    for token in tokens:
        try:
            bot_id, path = _resolve_one(token)
        except SystemExit as exc:
            skipped.append({"token": token, "reason": str(exc)})
            continue
        if bot_id == base_id:
            skipped.append({"token": token, "bot_id": bot_id, "reason": "base bot is already benchmark old"})
            continue
        snapshot = snapshots.get(bot_id)
        if snapshot:
            path = snapshot["bot_dir"]
        else:
            skipped.append({"token": token, "bot_id": bot_id, "reason": "snapshot unavailable; using live resolved path"})
        if path in seen:
            continue
        seen.add(path)
        opponents.append(path)
    return opponents, skipped


def _opponent_names(opponents: list[str]) -> str:
    names = []
    for path_text in opponents:
        path = Path(path_text)
        names.append(path.parent.name if path.name == "bot.py" else path.name)
    return ", ".join(names or ["<none>"])


def _cap_field_opponents(opponents: list[str], skipped: list[dict], gate: str) -> list[str]:
    max_opponents = MAX_TABLE_BOTS - 1
    if len(opponents) <= max_opponents:
        return opponents
    for path_text in opponents[max_opponents:]:
        skipped.append({
            "token": path_text,
            "reason": f"{gate} field capped at {max_opponents} opponents because match engine supports at most {MAX_TABLE_BOTS} total bots",
        })
    return opponents[:max_opponents]


def _find_lineage_candidates(base_path: str, current_candidate_dir: Path) -> list[str]:
    lineage = []
    try:
        path = Path(base_path).resolve()
    except OSError:
        return lineage
    for parent in path.parents:
        if parent.name == "hl_runs":
            break
        if parent.parent.name == "hl_runs":
            candidates_root = parent / "bots" / "candidates"
            if candidates_root.exists():
                for bot_py in sorted(candidates_root.glob("*/*/latest/bot.py")):
                    candidate_dir = bot_py.parent.resolve()
                    if candidate_dir != current_candidate_dir.resolve():
                        lineage.append(str(candidate_dir))
            break
    return lineage


def _selection_gate_result(name: str, benchmark: dict, max_bust_rate_increase: float) -> dict:
    parsed = benchmark.get("json")
    if not isinstance(parsed, dict):
        return {
            "name": name,
            "passed": False,
            "reason": "benchmark output was not valid JSON",
            "benchmark": benchmark,
        }

    reasons = []
    acceptance = parsed.get("acceptance", {})
    direct = parsed.get("direct", {}).get("paired", {})
    direct_mean = direct.get("mean_improvement", 0) or 0
    if direct_mean <= 0:
        reasons.append(f"direct head-to-head mean improvement was {direct_mean:+.1f}")

    field_setups = parsed.get("field_setups", [])
    mean_improvements = []
    worst_setup_delta = None
    for setup in field_setups:
        paired = setup.get("paired", {})
        mean_improvement = paired.get("mean_improvement", 0) or 0
        mean_improvements.append(mean_improvement)
        worst_setup_delta = mean_improvement if worst_setup_delta is None else min(worst_setup_delta, mean_improvement)
        if mean_improvement < CATASTROPHIC_MEAN_IMPROVEMENT:
            reasons.append(f"{setup.get('name')} catastrophic mean improvement {mean_improvement:+.1f}")
        bust_change = paired.get("bust_rate_change", 0) or 0
        if bust_change > max_bust_rate_increase:
            reasons.append(f"{setup.get('name')} bust-rate regression {bust_change:+.2f}")

    field_mean = sum(mean_improvements) / len(mean_improvements) if mean_improvements else 0
    if field_setups and field_mean <= 0:
        reasons.append(f"paired field mean improvement was {field_mean:+.1f}")

    if not acceptance.get("passed"):
        regressed = acceptance.get("regressed_setups") or []
        bust_regressed = acceptance.get("bust_regression_setups") or []
        if regressed:
            reasons.append("benchmark acceptance regressed setups: " + ", ".join(regressed))
        if bust_regressed:
            reasons.append("benchmark acceptance bust regressions: " + ", ".join(bust_regressed))

    return {
        "name": name,
        "passed": not reasons,
        "reason": "; ".join(reasons) if reasons else "passed",
        "direct_mean_improvement": direct_mean,
        "field_mean_improvement": field_mean,
        "worst_setup_delta": worst_setup_delta,
        "benchmark_acceptance": acceptance,
        "benchmark": benchmark,
    }


def _run_staged_selection(
    base_id: str,
    base_path: str,
    candidate_path: str,
    candidate_dir: Path,
    args: argparse.Namespace,
    run_dir: Path,
    snapshots: dict[str, dict],
    round_index: int,
) -> dict:
    result = {
        "accepted": False,
        "failed_gate": None,
        "baseline_count": 0,
        "elimination_metrics": None,
        "full_metrics": None,
        "finalist_metrics": None,
        "reason": "",
        "skipped_baselines": [],
    }

    elimination_opponents, skipped = _resolve_baseline_opponents(ELIMINATION_BASELINES, snapshots, base_id)
    result["skipped_baselines"].extend(skipped)
    result["baseline_count"] = 1 + len(elimination_opponents)
    _log("Elimination benchmark against bots: " + _opponent_names(elimination_opponents))
    elimination = _run_benchmark(
        base_path=base_path,
        new_path=candidate_path,
        iterations=args.elimination_iterations,
        hands=args.elimination_hands,
        seed=args.seed,
        run_name=f"{run_dir.name}_round{round_index:02d}_elimination",
        timeout=args.benchmark_timeout,
        max_bad_setups=0,
        opponents=elimination_opponents,
    )
    elimination_gate = _selection_gate_result("elimination", elimination, args.max_bust_rate_increase)
    result["elimination_metrics"] = elimination_gate
    (_selection_artifact_dir(run_dir) / f"selection_round_{round_index:02d}_gate_elimination.json").write_text(json.dumps(elimination_gate, indent=2) + "\n")
    _log("Elimination metrics: " + _benchmark_summary_text(elimination))
    if not elimination_gate["passed"]:
        result["failed_gate"] = "elimination"
        result["reason"] = elimination_gate["reason"]
        return result

    full_tokens = args.selection_baselines or FULL_SELECTION_BASELINES
    full_opponents, skipped = _resolve_baseline_opponents(full_tokens, snapshots, base_id)
    result["skipped_baselines"].extend(skipped)
    full_opponents = _cap_field_opponents(full_opponents, result["skipped_baselines"], "full selection")
    result["baseline_count"] = 1 + len(full_opponents)
    _log("Full selection benchmark against bots: " + _opponent_names(full_opponents))
    full = _run_benchmark(
        base_path=base_path,
        new_path=candidate_path,
        iterations=args.full_iterations,
        hands=args.full_hands,
        seed=args.seed + 1000,
        run_name=f"{run_dir.name}_round{round_index:02d}_full",
        timeout=args.benchmark_timeout,
        max_bad_setups=0,
        opponents=full_opponents,
    )
    full_gate = _selection_gate_result("full", full, args.max_bust_rate_increase)
    result["full_metrics"] = full_gate
    (_selection_artifact_dir(run_dir) / f"selection_round_{round_index:02d}_gate_full.json").write_text(json.dumps(full_gate, indent=2) + "\n")
    _log("Full selection metrics: " + _benchmark_summary_text(full))
    if not full_gate["passed"]:
        result["failed_gate"] = "full"
        result["reason"] = full_gate["reason"]
        return result

    if not args.skip_finalist_check:
        finalist_tokens = list(FINALIST_BASELINES)
        finalist_opponents, skipped = _resolve_baseline_opponents(finalist_tokens, snapshots, base_id)
        lineage_opponents = _find_lineage_candidates(base_path, candidate_dir)
        finalist_opponents.extend(path for path in lineage_opponents if path not in finalist_opponents)
        result["skipped_baselines"].extend(skipped)
        finalist_opponents = _cap_field_opponents(finalist_opponents, result["skipped_baselines"], "finalist")
        _log("Finalist benchmark against bots: " + _opponent_names(finalist_opponents))
        finalist = _run_benchmark(
            base_path=base_path,
            new_path=candidate_path,
            iterations=max(1, args.full_iterations),
            hands=args.full_hands,
            seed=args.seed + 2000,
            run_name=f"{run_dir.name}_round{round_index:02d}_finalist",
            timeout=args.benchmark_timeout,
            max_bad_setups=0,
            opponents=finalist_opponents,
        )
        finalist_gate = _selection_gate_result("finalist", finalist, args.max_bust_rate_increase)
        result["finalist_metrics"] = finalist_gate
        (_selection_artifact_dir(run_dir) / f"selection_round_{round_index:02d}_gate_finalist.json").write_text(json.dumps(finalist_gate, indent=2) + "\n")
        _log("Finalist metrics: " + _benchmark_summary_text(finalist))
        if not finalist_gate["passed"]:
            result["failed_gate"] = "finalist"
            result["reason"] = finalist_gate["reason"]
            return result

    result["accepted"] = True
    result["reason"] = "passed staged selection"
    return result


def _is_excluded_curve_training_bot(bot_id: str) -> bool:
    return any(bot_id == prefix or bot_id.startswith(prefix + "_") for prefix in CURVE_EXCLUDED_TRAINING_PREFIXES)


def _curve_opponent_pool(snapshots: dict[str, dict], base_id: str) -> tuple[list[dict], list[str]]:
    pool = []
    excluded = []
    for bot_id, snapshot in sorted(snapshots.items()):
        if bot_id == base_id:
            continue
        if _is_excluded_curve_training_bot(bot_id):
            excluded.append(bot_id)
            continue
        bot_dir = snapshot.get("bot_dir")
        if bot_dir and Path(bot_dir).exists():
            pool.append({"bot_id": bot_id, "path": bot_dir})
    return pool, excluded


def _curve_training_groups(snapshots: dict[str, dict], base_id: str, group_count: int, seed: int) -> list[dict]:
    pool, _excluded = _curve_opponent_pool(snapshots, base_id)
    if len(pool) < 5:
        raise RuntimeError(f"Curve benchmark needs at least 5 baseline opponents, found {len(pool)}")
    groups = []
    for group_index in range(group_count):
        shuffled = list(pool)
        random.Random(f"{seed}:{group_index}").shuffle(shuffled)
        opponents = shuffled[:5]
        groups.append(
            {
                "group_id": f"group_{group_index + 1:02d}",
                "opponents": opponents,
            }
        )
    return groups


def _curve_excluded_training_bots(snapshots: dict[str, dict], base_id: str) -> list[str]:
    _pool, excluded = _curve_opponent_pool(snapshots, base_id)
    return excluded


def _curve_candidate_label(path_text: str, fallback: str) -> str:
    path = Path(path_text)
    parts = path.parts
    if "hl_runs" in parts:
        index = parts.index("hl_runs")
        if index + 1 < len(parts):
            run_name = parts[index + 1]
            if "candidates" in parts:
                cidx = parts.index("candidates")
                tail = "_".join(parts[cidx + 1:cidx + 3])
                return _safe_name(f"{run_name}_{tail}")
            return _safe_name(run_name)
    return _safe_name(fallback)


def _curve_candidate_entries(
    base_id: str,
    base_path: str,
    current_candidate_dir: Path,
    recent_candidates: int,
) -> list[dict]:
    entries = [
        {
            "candidate_id": "base",
            "label": f"base_{_safe_name(base_id)}",
            "path": base_path,
            "order": 0,
        }
    ]
    seen = {str(Path(base_path).resolve())}
    lineage = _find_lineage_candidates(base_path, current_candidate_dir)
    recent_lineage = lineage[-recent_candidates:] if recent_candidates else []
    for path_text in recent_lineage:
        try:
            resolved = str(Path(path_text).resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        entries.append(
            {
                "candidate_id": f"prior_{len(entries):02d}",
                "label": _curve_candidate_label(path_text, f"prior_{len(entries):02d}"),
                "path": path_text,
                "order": len(entries),
            }
        )
    current_resolved = str(current_candidate_dir.resolve())
    if current_resolved not in seen:
        entries.append(
            {
                "candidate_id": "current",
                "label": "current_candidate",
                "path": str(current_candidate_dir),
                "order": len(entries),
            }
        )
    return entries


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _selection_artifact_dir(run_dir: Path) -> Path:
    path = run_dir / "artifacts" / "selection"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _curve_artifact_dir(run_dir: Path) -> Path:
    path = run_dir / "artifacts" / "curve"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _summarize_curve_rows(rows: list[dict], candidate_entries: list[dict]) -> list[dict]:
    by_candidate = {entry["candidate_id"]: [] for entry in candidate_entries}
    for row in rows:
        by_candidate.setdefault(row["candidate_id"], []).append(row)

    summaries = []
    for entry in candidate_entries:
        candidate_rows = by_candidate.get(entry["candidate_id"], [])
        deltas = [float(row["chip_delta"]) for row in candidate_rows]
        places = [float(row["place"]) for row in candidate_rows]
        top_half_values = [1.0 if row["top_half"] else 0.0 for row in candidate_rows]
        bust_values = [1.0 if row["busted"] else 0.0 for row in candidate_rows]
        group_wins = {}
        for row in candidate_rows:
            group_wins.setdefault(row["group_id"], []).append(1.0 if int(row["place"]) == 1 else 0.0)
        groups_won = sum(1 for values in group_wins.values() if _mean(values) > 0.5)
        summaries.append(
            {
                "candidate_id": entry["candidate_id"],
                "label": entry["label"],
                "order": entry["order"],
                "matches": len(candidate_rows),
                "avg_delta": round(_mean(deltas), 3),
                "median_delta": round(_median(deltas), 3),
                "worst_group_delta": round(min(deltas), 3) if deltas else 0,
                "best_group_delta": round(max(deltas), 3) if deltas else 0,
                "avg_place": round(_mean(places), 3),
                "top_half_rate": round(_mean(top_half_values), 3),
                "bust_rate": round(_mean(bust_values), 3),
                "groups_won": groups_won,
            }
        )
    return summaries


def _plot_metric_curves(curve_dir: Path, summaries: list[dict], plot_specs: list[tuple[str, str, str]]) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        _log(f"Curve PNG generation skipped; matplotlib unavailable: {exc}")
        return []

    labels = [row["label"] for row in summaries]
    xs = list(range(len(summaries)))
    paths = []
    for filename, metric, title in plot_specs:
        values = [row[metric] for row in summaries]
        plt.figure(figsize=(max(8, len(labels) * 1.3), 4.8))
        plt.plot(xs, values, marker="o", linewidth=2)
        plt.xticks(xs, labels, rotation=35, ha="right")
        plt.title(title)
        plt.xlabel("Candidate Lineage")
        plt.ylabel(title)
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        output = curve_dir / filename
        plt.savefig(output)
        plt.close()
        paths.append(str(output))
    return paths


def _plot_training_curve(curve_dir: Path, summaries: list[dict]) -> list[str]:
    return _plot_metric_curves(
        curve_dir,
        summaries,
        [
            ("training_curve_avg_delta.png", "avg_delta", "Training Average Chip Delta"),
            ("training_curve_top_half.png", "top_half_rate", "Training Top-Half Rate"),
            ("training_curve_bust_rate.png", "bust_rate", "Training Bust Rate"),
            ("training_curve_worst_delta.png", "worst_group_delta", "Training Worst Group Delta"),
        ],
    )


def _plot_validation_curve(curve_dir: Path, summaries: list[dict]) -> list[str]:
    return _plot_metric_curves(
        curve_dir,
        summaries,
        [
            ("validation_curve_avg_delta.png", "avg_delta", "Validation Average Chip Delta"),
            ("validation_curve_win_rate.png", "win_rate", "Validation Win Rate"),
            ("validation_curve_bust_rate.png", "bust_rate", "Validation Bust Rate"),
            ("validation_curve_worst_delta.png", "worst_delta", "Validation Worst Delta"),
        ],
    )


def _lineage_round_row(result: dict) -> dict | None:
    training = result.get("training_summary") or result.get("summary") or []
    validation = result.get("validation_summary") or []
    if len(training) < 2:
        return None
    train_base = training[0]
    train_current = training[-1]
    val_base = validation[0] if len(validation) >= 2 else {}
    val_current = validation[-1] if len(validation) >= 2 else {}
    round_index = int(result.get("round") or 0)
    return {
        "round": round_index,
        "round_label": f"round_{round_index:02d}",
        "candidate_label": train_current.get("label", "current_candidate"),
        "training_base_avg_delta": train_base.get("avg_delta", 0),
        "training_candidate_avg_delta": train_current.get("avg_delta", 0),
        "training_delta_vs_base": train_current.get("avg_delta", 0) - train_base.get("avg_delta", 0),
        "training_top_half_rate": train_current.get("top_half_rate", 0),
        "training_bust_rate": train_current.get("bust_rate", 0),
        "training_worst_group_delta": train_current.get("worst_group_delta", 0),
        "training_groups_won": train_current.get("groups_won", 0),
        "validation_base_avg_delta": val_base.get("avg_delta", 0),
        "validation_candidate_avg_delta": val_current.get("avg_delta", 0),
        "validation_delta_vs_base": val_current.get("avg_delta", 0) - val_base.get("avg_delta", 0),
        "validation_win_rate": val_current.get("win_rate", 0),
        "validation_bust_rate": val_current.get("bust_rate", 0),
        "validation_worst_delta": val_current.get("worst_delta", 0),
    }


def _load_curve_round_results(run_dir: Path, current_result: dict | None = None) -> list[dict]:
    by_round = {}
    round_paths = list((run_dir / "artifacts" / "curve").glob("curve_round_*.json"))
    round_paths.extend(run_dir.glob("curve_round_*.json"))
    for path in sorted(round_paths):
        try:
            result = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        round_index = int(result.get("round") or 0)
        if round_index:
            by_round[round_index] = result
    if current_result:
        round_index = int(current_result.get("round") or 0)
        if round_index:
            by_round[round_index] = current_result
    return [by_round[key] for key in sorted(by_round)]


def _plot_condensed_curves(curve_dir: Path, lineage_rows: list[dict]) -> list[str]:
    if not lineage_rows:
        return []
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        _log(f"Condensed curve PNG generation skipped; matplotlib unavailable: {exc}")
        return []

    labels = [row["round_label"] for row in lineage_rows]
    xs = list(range(len(lineage_rows)))
    paths = []

    plt.figure(figsize=(max(9, len(labels) * 1.0), 5.2))
    plt.axhline(0, color="black", linewidth=1, alpha=0.45)
    plt.plot(xs, [row["training_delta_vs_base"] for row in lineage_rows], marker="o", linewidth=2, label="training delta vs base")
    plt.plot(xs, [row["validation_delta_vs_base"] for row in lineage_rows], marker="o", linewidth=2, label="validation delta vs base")
    plt.xticks(xs, labels, rotation=35, ha="right")
    plt.title("Candidate Scoreboard")
    plt.xlabel("Improvement Round")
    plt.ylabel("Chip Delta vs Original Base")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    output = curve_dir / "curve_scoreboard.png"
    plt.savefig(output)
    plt.close()
    paths.append(str(output))

    plt.figure(figsize=(max(9, len(labels) * 1.0), 5.2))
    plt.plot(xs, [row["training_top_half_rate"] for row in lineage_rows], marker="o", linewidth=2, label="training top-half")
    plt.plot(xs, [row["training_bust_rate"] for row in lineage_rows], marker="o", linewidth=2, label="training bust")
    plt.plot(xs, [row["validation_win_rate"] for row in lineage_rows], marker="o", linewidth=2, label="validation win")
    plt.plot(xs, [row["validation_bust_rate"] for row in lineage_rows], marker="o", linewidth=2, label="validation bust")
    plt.ylim(-0.05, 1.05)
    plt.xticks(xs, labels, rotation=35, ha="right")
    plt.title("Risk And Validation")
    plt.xlabel("Improvement Round")
    plt.ylabel("Rate")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    output = curve_dir / "curve_risk_validation.png"
    plt.savefig(output)
    plt.close()
    paths.append(str(output))
    return paths


def _write_lineage_curve_artifacts(curve_dir: Path, current_result: dict | None = None) -> dict:
    run_dir = curve_dir.parent
    results = _load_curve_round_results(run_dir, current_result)
    rows = []
    for result in results:
        row = _lineage_round_row(result)
        if row:
            rows.append(row)

    fields = [
        "round",
        "round_label",
        "candidate_label",
        "training_base_avg_delta",
        "training_candidate_avg_delta",
        "training_delta_vs_base",
        "training_top_half_rate",
        "training_bust_rate",
        "training_worst_group_delta",
        "training_groups_won",
        "validation_base_avg_delta",
        "validation_candidate_avg_delta",
        "validation_delta_vs_base",
        "validation_win_rate",
        "validation_bust_rate",
        "validation_worst_delta",
    ]
    lineage_csv = curve_dir / "lineage_summary.csv"
    _write_csv(lineage_csv, rows, fields)
    main_plots = _plot_condensed_curves(curve_dir, rows)
    return {
        "lineage_summary_csv": str(lineage_csv),
        "lineage_rounds": len(rows),
        "main_plots": main_plots,
        "lineage_summary": rows,
    }


def _run_validation_curve(
    run_dir: Path,
    candidates: list[dict],
    hands: int,
    iterations: int,
    seed: int,
    round_index: int,
) -> tuple[list[dict], list[dict]]:
    rows = []
    if len(candidates) < 2:
        return rows, [
            {
                "candidate_id": candidate["candidate_id"],
                "label": candidate["label"],
                "order": candidate["order"],
                "matches": 0,
                "avg_delta": 0,
                "median_delta": 0,
                "worst_delta": 0,
                "best_delta": 0,
                "avg_place": 0,
                "win_rate": 0,
                "bust_rate": 0,
                "opponents_beaten": 0,
            }
            for candidate in candidates
        ]

    for candidate_index, candidate in enumerate(candidates):
        for opponent_index, opponent in enumerate(candidates):
            if candidate_index == opponent_index:
                continue
            for iteration in range(iterations):
                match_seed = seed + 50_000_000 + candidate_index * 1_000_000 + opponent_index * 1_000 + iteration
                candidate_bot_id = f"candidate_{candidate_index:02d}"
                opponent_bot_id = f"opponent_{opponent_index:02d}"
                match = run_match(
                    f"valcurve_{run_dir.name}_r{round_index:02d}_c{candidate_index:02d}_o{opponent_index:02d}_i{iteration:02d}",
                    {
                        candidate_bot_id: candidate["path"],
                        opponent_bot_id: opponent["path"],
                    },
                    n_hands=hands,
                    seed=match_seed,
                )
                place = _rank_for_match(match, candidate_bot_id)
                rows.append(
                    {
                        "round": round_index,
                        "candidate_id": candidate["candidate_id"],
                        "candidate_label": candidate["label"],
                        "candidate_order": candidate["order"],
                        "opponent_id": opponent["candidate_id"],
                        "opponent_label": opponent["label"],
                        "iteration": iteration,
                        "seed": match_seed,
                        "hands": match["n_hands"],
                        "chip_delta": match["chip_delta"][candidate_bot_id],
                        "place": place,
                        "win": place == 1,
                        "busted": match["final_stacks"][candidate_bot_id] <= 0,
                        "final_stack": match["final_stacks"][candidate_bot_id],
                        "duration_s": match["duration_s"],
                    }
                )

    summaries = []
    for candidate in candidates:
        candidate_rows = [row for row in rows if row["candidate_id"] == candidate["candidate_id"]]
        deltas = [float(row["chip_delta"]) for row in candidate_rows]
        places = [float(row["place"]) for row in candidate_rows]
        wins = [1.0 if row["win"] else 0.0 for row in candidate_rows]
        busts = [1.0 if row["busted"] else 0.0 for row in candidate_rows]
        by_opponent = {}
        for row in candidate_rows:
            by_opponent.setdefault(row["opponent_id"], []).append(float(row["chip_delta"]))
        opponents_beaten = sum(1 for values in by_opponent.values() if _mean(values) > 0)
        summaries.append(
            {
                "candidate_id": candidate["candidate_id"],
                "label": candidate["label"],
                "order": candidate["order"],
                "matches": len(candidate_rows),
                "avg_delta": round(_mean(deltas), 3),
                "median_delta": round(_median(deltas), 3),
                "worst_delta": round(min(deltas), 3) if deltas else 0,
                "best_delta": round(max(deltas), 3) if deltas else 0,
                "avg_place": round(_mean(places), 3),
                "win_rate": round(_mean(wins), 3),
                "bust_rate": round(_mean(busts), 3),
                "opponents_beaten": opponents_beaten,
            }
        )
    return rows, summaries


def _curve_summary_text(curve_result: dict | None) -> str:
    if not curve_result:
        return "curve benchmark not run"
    summaries = curve_result.get("training_summary") or curve_result.get("summary", [])
    if not summaries:
        return "curve benchmark produced no summary rows"
    base = summaries[0]
    current = summaries[-1]
    delta = current.get("avg_delta", 0) - base.get("avg_delta", 0)
    bust_change = current.get("bust_rate", 0) - base.get("bust_rate", 0)
    validation = curve_result.get("validation_summary") or []
    validation_bits = ""
    if validation:
        val_base = validation[0]
        val_current = validation[-1]
        validation_bits = (
            f"; validation_current_avg_delta={val_current.get('avg_delta'):+.1f}; "
            f"validation_delta_vs_base={val_current.get('avg_delta', 0) - val_base.get('avg_delta', 0):+.1f}; "
            f"validation_win_rate={val_current.get('win_rate'):.2f}"
        )
    return (
        f"curve current_avg_delta={current.get('avg_delta'):+.1f}; "
        f"base_avg_delta={base.get('avg_delta'):+.1f}; "
        f"delta_vs_base={delta:+.1f}; "
        f"current_worst_group={current.get('worst_group_delta'):+.1f}; "
        f"top_half={current.get('top_half_rate'):.2f}; "
        f"bust_change={bust_change:+.2f}; "
        f"groups_won={current.get('groups_won')}"
        f"{validation_bits}"
    )


def _run_curve_benchmark(
    base_id: str,
    base_path: str,
    current_candidate_dir: Path,
    args: argparse.Namespace,
    run_dir: Path,
    snapshots: dict[str, dict],
    round_index: int,
) -> dict:
    curve_dir = run_dir / "curve_benchmark"
    curve_dir.mkdir(parents=True, exist_ok=True)
    groups = _curve_training_groups(snapshots, base_id, args.curve_groups, args.curve_seed)
    candidates = _curve_candidate_entries(base_id, base_path, current_candidate_dir, args.curve_recent_candidates)
    rows = []

    groups_record = {
        "round": round_index,
        "group_count": args.curve_groups,
        "hands": args.curve_hands,
        "iterations": args.curve_iterations,
        "seed": args.curve_seed,
        "excluded_training_opponents": _curve_excluded_training_bots(snapshots, base_id),
        "candidates": candidates,
        "groups": groups,
    }
    (curve_dir / "training_groups.json").write_text(json.dumps(groups_record, indent=2) + "\n")

    for candidate_index, candidate in enumerate(candidates):
        _log(f"Curve benchmark candidate {candidate_index + 1}/{len(candidates)}: {candidate['label']}")
        for group_index, group in enumerate(groups):
            for iteration in range(args.curve_iterations):
                seed = args.curve_seed + candidate_index * 1_000_000 + group_index * 1_000 + iteration
                candidate_bot_id = f"candidate_{candidate_index:02d}"
                table = {candidate_bot_id: candidate["path"]}
                for opponent in group["opponents"]:
                    bot_id = opponent["bot_id"]
                    if bot_id in table:
                        bot_id = f"{bot_id}_opponent"
                    table[bot_id] = opponent["path"]
                match = run_match(
                    f"curve_{run_dir.name}_r{round_index:02d}_c{candidate_index:02d}_g{group_index:02d}_i{iteration:02d}",
                    table,
                    n_hands=args.curve_hands,
                    seed=seed,
                )
                place = _rank_for_match(match, candidate_bot_id)
                rows.append(
                    {
                        "round": round_index,
                        "candidate_id": candidate["candidate_id"],
                        "candidate_label": candidate["label"],
                        "candidate_order": candidate["order"],
                        "group_id": group["group_id"],
                        "iteration": iteration,
                        "seed": seed,
                        "hands": match["n_hands"],
                        "chip_delta": match["chip_delta"][candidate_bot_id],
                        "place": place,
                        "top_half": _top_half_for_match(match, candidate_bot_id),
                        "busted": match["final_stacks"][candidate_bot_id] <= 0,
                        "final_stack": match["final_stacks"][candidate_bot_id],
                        "opponent_ids": "|".join(opponent["bot_id"] for opponent in group["opponents"]),
                        "duration_s": match["duration_s"],
                    }
                )

    curve_fields = [
        "round",
        "candidate_id",
        "candidate_label",
        "candidate_order",
        "group_id",
        "iteration",
        "seed",
        "hands",
        "chip_delta",
        "place",
        "top_half",
        "busted",
        "final_stack",
        "opponent_ids",
        "duration_s",
    ]
    summary_fields = [
        "candidate_id",
        "label",
        "order",
        "matches",
        "avg_delta",
        "median_delta",
        "worst_group_delta",
        "best_group_delta",
        "avg_place",
        "top_half_rate",
        "bust_rate",
        "groups_won",
    ]
    validation_fields = [
        "round",
        "candidate_id",
        "candidate_label",
        "candidate_order",
        "opponent_id",
        "opponent_label",
        "iteration",
        "seed",
        "hands",
        "chip_delta",
        "place",
        "win",
        "busted",
        "final_stack",
        "duration_s",
    ]
    validation_summary_fields = [
        "candidate_id",
        "label",
        "order",
        "matches",
        "avg_delta",
        "median_delta",
        "worst_delta",
        "best_delta",
        "avg_place",
        "win_rate",
        "bust_rate",
        "opponents_beaten",
    ]
    summaries = _summarize_curve_rows(rows, candidates)
    _log("Running validation curve across recent candidate lineage")
    validation_rows, validation_summaries = _run_validation_curve(
        run_dir=run_dir,
        candidates=candidates,
        hands=args.curve_hands,
        iterations=args.curve_iterations,
        seed=args.curve_seed,
        round_index=round_index,
    )
    _write_csv(curve_dir / "training_curve.csv", rows, curve_fields)
    _write_csv(curve_dir / "training_summary.csv", summaries, summary_fields)
    _write_csv(curve_dir / "validation_curve.csv", validation_rows, validation_fields)
    _write_csv(curve_dir / "validation_summary.csv", validation_summaries, validation_summary_fields)
    validation_record = {
        "round": round_index,
        "hands": args.curve_hands,
        "iterations": args.curve_iterations,
        "seed": args.curve_seed,
        "candidates": candidates,
        "description": "Each candidate plays heads-up against every other candidate in the lineage.",
    }
    (curve_dir / "validation_groups.json").write_text(json.dumps(validation_record, indent=2) + "\n")
    result = {
        "round": round_index,
        "curve_dir": str(curve_dir),
        "training_curve_csv": str(curve_dir / "training_curve.csv"),
        "training_summary_csv": str(curve_dir / "training_summary.csv"),
        "training_groups_json": str(curve_dir / "training_groups.json"),
        "validation_curve_csv": str(curve_dir / "validation_curve.csv"),
        "validation_summary_csv": str(curve_dir / "validation_summary.csv"),
        "validation_groups_json": str(curve_dir / "validation_groups.json"),
        "training_plots": [],
        "validation_plots": [],
        "detailed_plots": [],
        "main_plots": [],
        "plots": [],
        "summary": summaries,
        "training_summary": summaries,
        "validation_summary": validation_summaries,
    }
    lineage_artifacts = _write_lineage_curve_artifacts(curve_dir, result)
    result.update(lineage_artifacts)
    if getattr(args, "curve_detailed_plots", False):
        training_plot_paths = _plot_training_curve(curve_dir, summaries)
        validation_plot_paths = _plot_validation_curve(curve_dir, validation_summaries)
        result["training_plots"] = training_plot_paths
        result["validation_plots"] = validation_plot_paths
        result["detailed_plots"] = training_plot_paths + validation_plot_paths
    result["plots"] = result.get("main_plots", []) + result.get("detailed_plots", [])
    (curve_dir / "curve_summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def _acceptance_passed(benchmark: dict) -> bool:
    parsed = benchmark.get("json")
    if not isinstance(parsed, dict):
        return False
    return bool(parsed.get("acceptance", {}).get("passed"))


def _benchmark_summary_text(benchmark: dict) -> str:
    parsed = benchmark.get("json")
    if not isinstance(parsed, dict):
        return "benchmark output was not valid JSON"
    acceptance = parsed.get("acceptance", {})
    chunks = [
        f"accepted={acceptance.get('passed')}",
        f"direct_ok={acceptance.get('direct_head_to_head_ok')}",
        f"improved_setups={acceptance.get('improved_setup_count')}",
        f"regressed_setups={acceptance.get('regressed_setup_count')}",
        f"bust_regressions={acceptance.get('bust_regression_count')}",
    ]
    direct = parsed.get("direct", {}).get("paired", {})
    if direct:
        chunks.append(f"direct_mean_improvement={direct.get('mean_improvement'):+.1f}")
    setup_bits = []
    for setup in parsed.get("field_setups", []):
        paired = setup.get("paired", {})
        setup_bits.append(f"{setup.get('name')}={paired.get('mean_improvement', 0):+.1f}")
    if setup_bits:
        chunks.append("field_mean_improvements[" + ", ".join(setup_bits) + "]")
    return "; ".join(chunks)


def main():
    _load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="Use DeepSeek offline to iterate a standalone poker bot.")
    parser.add_argument("--base", required=True, help="Baseline bot token/path, e.g. short_stack_survivor:v1")
    parser.add_argument("--new-type", help="Candidate bot family. Defaults to inferred baseline type.")
    parser.add_argument("--new-version", help="Candidate version. Defaults to next vN for the family.")
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--metrics-run", action="append", default=[], help="Prior run dir or results.json to include as feedback.")
    parser.add_argument("--llm-attempts", type=int, default=3, help="Repair attempts for compile/validator failures.")
    parser.add_argument("--improvement-rounds", type=int, default=1, help="LLM benchmark-feedback rounds.")
    parser.add_argument("--benchmark-iterations", type=int, default=4)
    parser.add_argument("--hands", type=int, default=80)
    parser.add_argument("--selection-profile", choices=["staged", "legacy"], default="staged")
    parser.add_argument("--elimination-iterations", type=int, default=3)
    parser.add_argument("--elimination-hands", type=int, default=60)
    parser.add_argument("--full-iterations", type=int, default=10)
    parser.add_argument("--full-hands", type=int, default=100)
    parser.add_argument("--selection-baselines", nargs="+", help="Override full-selection baseline tokens.")
    parser.add_argument("--skip-finalist-check", action="store_true")
    parser.add_argument("--curve-benchmark", action="store_true", help="Run diagnostic 20-group training curve after selection.")
    parser.add_argument("--curve-groups", type=int, default=20)
    parser.add_argument("--curve-hands", type=int, default=400)
    parser.add_argument("--curve-iterations", type=int, default=1)
    parser.add_argument("--curve-seed", type=int, default=9000)
    parser.add_argument("--curve-recent-candidates", type=int, default=8)
    parser.add_argument("--curve-detailed-plots", action="store_true", help="Also write the older per-metric curve PNGs.")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--max-bad-setups", type=int, default=0)
    parser.add_argument("--max-bust-rate-increase", type=float, default=0.10)
    parser.add_argument("--benchmark-timeout", type=int, default=600)
    parser.add_argument("--run-name")
    parser.add_argument("--hl-runs-dir", default=str(HL_RUNS_DIR), help="Immutable LLM run workspace root.")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--endpoint", default="https://api.deepseek.com/chat/completions")
    parser.add_argument("--temperature", type=float, default=0.4)
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for local API experiments.")
    parser.add_argument("--force", action="store_true", help="Reserved for compatibility; hl_runs never overwrites old runs.")
    parser.add_argument("--dry-run", action="store_true", help="Write prompt/context but do not call DeepSeek or modify bot files.")
    args = parser.parse_args()
    if args.curve_groups < 1:
        raise SystemExit("--curve-groups must be at least 1")
    if args.curve_hands < 1:
        raise SystemExit("--curve-hands must be at least 1")
    if args.curve_iterations < 1:
        raise SystemExit("--curve-iterations must be at least 1")
    if args.curve_recent_candidates < 0:
        raise SystemExit("--curve-recent-candidates must be non-negative")

    _log("Resolving baseline and candidate target")
    base_id, base_path = _resolve_one(args.base)
    inferred = _infer_version(base_path)
    new_type = args.new_type or inferred.get("type")
    if not new_type:
        raise SystemExit("--new-type is required when baseline type cannot be inferred")
    hl_runs_dir = Path(args.hl_runs_dir)
    if not hl_runs_dir.is_absolute():
        hl_runs_dir = ROOT / hl_runs_dir
    new_version = args.new_version or _next_version(new_type, hl_runs_dir)
    new_token = f"{new_type}:{new_version}"
    run_dir = _create_run_dir(args.run_name, base_id, new_token.replace(":", "_"), hl_runs_dir)
    _initialize_run_log(run_dir, sys.argv)
    run_id = (run_dir / "RUN_ID").read_text().strip()
    _log(f"Created run {run_id}: {run_dir}")
    _log("Copying all baseline bots into immutable run workspace")
    baseline_snapshots = _copy_all_baselines(run_dir)
    base_run_path = _ensure_base_snapshot(run_dir, baseline_snapshots, base_id, base_path)
    _log(f"Baseline for benchmark: {base_id} -> {base_run_path}")

    base_source_path = _source_file_for_bot(base_run_path)
    base_source = base_source_path.read_text()
    strategy_docs = _read_optional(ROOT / "bots" / "STRATEGY_COMPONENTS.md")
    iteration_docs = _read_optional(ROOT / "docs" / "LLM_ITERATION.md")
    metrics = _load_recent_metrics(args.metrics_run)
    initial_prompt = _initial_prompt(args.goal, args.base, new_token, base_source, strategy_docs, iteration_docs, metrics)

    manifest = {
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base": {
            "token": args.base,
            "bot_id": base_id,
            "source_path": base_path,
            "run_path": base_run_path,
            "source": str(base_source_path),
        },
        "candidate": {"token": new_token, "type": new_type, "version": new_version},
        "baseline_snapshots": baseline_snapshots,
        "settings": vars(args),
        "command": {
            "argv": sys.argv,
            "shell": _shell_command(sys.argv),
            "cwd": str(ROOT),
            "log": str(run_dir / "run.log"),
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    _append_run_index(run_dir, manifest)
    (run_dir / "initial_prompt.txt").write_text(initial_prompt)
    shutil.copyfile(base_source_path, run_dir / "base.pybak")

    if args.dry_run:
        _log(f"Dry run written: {run_dir}")
        _log(f"Prompt: {run_dir / 'initial_prompt.txt'}")
        return

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required unless --dry-run is used")

    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": initial_prompt},
    ]
    final_benchmark = None
    final_selection = None
    final_curve = None
    final_validation = None
    latest_candidate_dir = _latest_candidate_dir(run_dir, new_type, new_version)
    latest_candidate_path = latest_candidate_dir / "bot.py"
    accepted = False

    for round_index in range(1, args.improvement_rounds + 1):
        _log(f"Starting improvement round {round_index}/{args.improvement_rounds}")
        source = None
        for attempt in range(1, args.llm_attempts + 1):
            _log(f"DeepSeek is generating candidate: round {round_index}, attempt {attempt}")
            prompt_for_log = messages[-1]["content"]
            response = _deepseek_chat(messages, api_key, args.model, args.endpoint, args.temperature, args.insecure_ssl)
            _log("DeepSeek response received; extracting bot.py")
            source = _extract_python_code(response)
            attempt_dir = _write_attempt(run_dir, (round_index - 1) * args.llm_attempts + attempt, prompt_for_log, response, source)

            candidate_dir = _candidate_attempt_dir(run_dir, new_type, new_version, round_index, attempt)
            candidate_path = candidate_dir / "bot.py"
            candidate_path.write_text(source)
            (candidate_dir / "bot.pybak").write_text(source)
            latest_candidate_path.write_text(source)
            (latest_candidate_dir / "bot.pybak").write_text(source)
            validation = _validate_candidate(candidate_path)
            (attempt_dir / "validation.json").write_text(json.dumps(validation, indent=2) + "\n")
            (candidate_dir / "validation.json").write_text(json.dumps(validation, indent=2) + "\n")
            final_validation = validation
            if validation["passed"]:
                _log(f"Validation passed for {candidate_dir}")
                break

            _log("Validation failed; sending compiler/validator feedback to DeepSeek")
            error_report = json.dumps(validation, indent=2)[:20000]
            messages = [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _repair_prompt(error_report, source, iteration_docs)},
            ]
        else:
            raise SystemExit(f"Candidate failed validation after {args.llm_attempts} attempts. See {run_dir}")

        if args.selection_profile == "legacy":
            _log(f"Benchmarking baseline vs candidate: {base_id} vs {latest_candidate_dir}")
            final_benchmark = _benchmark(base_run_path, str(latest_candidate_dir), args, run_dir)
            (run_dir / f"benchmark_round_{round_index:02d}.json").write_text(json.dumps(final_benchmark, indent=2) + "\n")
            accepted = _acceptance_passed(final_benchmark)
            final_selection = {
                "accepted": accepted,
                "failed_gate": None if accepted else "legacy_benchmark",
                "baseline_count": None,
                "elimination_metrics": None,
                "full_metrics": None,
                "finalist_metrics": None,
                "reason": "legacy benchmark accepted" if accepted else "legacy benchmark failed acceptance",
                "legacy_benchmark": final_benchmark,
            }
            _log("Benchmark metrics: " + _benchmark_summary_text(final_benchmark))
        else:
            _log(f"Running staged selection for baseline vs candidate: {base_id} vs {latest_candidate_dir}")
            final_selection = _run_staged_selection(
                base_id=base_id,
                base_path=base_run_path,
                candidate_path=str(latest_candidate_dir),
                candidate_dir=latest_candidate_dir,
                args=args,
                run_dir=run_dir,
                snapshots=baseline_snapshots,
                round_index=round_index,
            )
            accepted = bool(final_selection["accepted"])
            final_benchmark = (
                (final_selection.get("finalist_metrics") or {}).get("benchmark")
                or (final_selection.get("full_metrics") or {}).get("benchmark")
                or (final_selection.get("elimination_metrics") or {}).get("benchmark")
            )
            (_selection_artifact_dir(run_dir) / f"selection_round_{round_index:02d}.json").write_text(json.dumps(final_selection, indent=2) + "\n")
        if args.curve_benchmark:
            _log(
                "Running 20-group training curve: "
                f"groups={args.curve_groups}, iterations={args.curve_iterations}, hands={args.curve_hands}"
            )
            final_curve = _run_curve_benchmark(
                base_id=base_id,
                base_path=base_run_path,
                current_candidate_dir=latest_candidate_dir,
                args=args,
                run_dir=run_dir,
                snapshots=baseline_snapshots,
                round_index=round_index,
            )
            (_curve_artifact_dir(run_dir) / f"curve_round_{round_index:02d}.json").write_text(json.dumps(final_curve, indent=2) + "\n")
            _log("Curve metrics: " + _curve_summary_text(final_curve))
        if accepted or round_index == args.improvement_rounds:
            break

        _log("Candidate was not accepted; asking DeepSeek for a metric-aware revision")
        metrics_payload = {
            "selection": final_selection,
            "benchmark": final_benchmark,
            "curve_summary_text": _curve_summary_text(final_curve),
            "curve_summary": (final_curve or {}).get("summary"),
        }
        metrics_text = json.dumps(metrics_payload, indent=2)[:30000]
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _metrics_prompt(metrics_text, source or latest_candidate_path.read_text(), iteration_docs)},
        ]

    shutil.copyfile(latest_candidate_path, run_dir / "candidate.pybak")
    summary = {
        "accepted": accepted,
        "candidate_path": str(latest_candidate_path),
        "candidate_run_dir": str(latest_candidate_dir),
        "base_run_dir": str(base_run_path),
        "run_dir": str(run_dir),
        "validation_passed": bool(final_validation and final_validation["passed"]),
        "benchmark_acceptance": (final_benchmark.get("json") or {}).get("acceptance") if final_benchmark else None,
        "selection": final_selection,
        "curve_benchmark": final_curve,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    _log(f"Final candidate: {latest_candidate_path}")
    _log(f"Run summary: {run_dir / 'summary.json'}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
