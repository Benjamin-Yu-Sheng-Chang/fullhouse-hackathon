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
import datetime as dt
import json
import os
import re
import shutil
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


DEFAULT_GOAL = (
    "Create a stronger heuristic poker bot version. Improve paired-field benchmark "
    "performance while avoiding higher bust rate. Keep the code readable, standalone, "
    "fast under the 2-second action limit, and validator-safe."
)


def _run(cmd: list[str], cwd: Path = ROOT, timeout: int | None = None) -> dict:
    started = time.time()
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


def _create_run_dir(name: str | None, base_id: str, new_token: str, hl_runs_dir: Path) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = name or f"{timestamp}_llm_{base_id}_to_{new_token}"
    candidate = hl_runs_dir / _safe_name(base)
    suffix = 2
    while candidate.exists():
        candidate = hl_runs_dir / f"{_safe_name(base)}_{suffix}"
        suffix += 1
    (candidate / "attempts").mkdir(parents=True)
    (candidate / "bots" / "baselines").mkdir(parents=True)
    (candidate / "bots" / "candidates").mkdir(parents=True)
    return candidate


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


def _deepseek_chat(messages: list[dict], api_key: str, model: str, endpoint: str, temperature: float) -> str:
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DeepSeek HTTP {exc.code}: {body}") from exc
    return data["choices"][0]["message"]["content"]


def _system_prompt() -> str:
    return (
        "You are improving a no-limit Texas Hold'em bot for a local hackathon engine. "
        "Return exactly one complete standalone Python bot.py. Do not include prose. "
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
    metrics: str,
) -> str:
    return f"""Goal:
{goal}

Base bot token: {base_token}
Candidate token to create: {new_token}

Strategy component notes:
{strategy_docs[:16000]}

Recent benchmark metrics:
{metrics or "No additional metrics provided."}

Current bot.py:
```python
{base_source}
```

Hard requirements:
- Return a complete standalone bot.py only.
- Preserve the Fullhouse return format: fold/check/call/raise/all_in.
- Do not call external APIs or import forbidden runtime modules.
- Optimize for benchmark.py acceptance: direct head-to-head positive, paired field improvement, no bust-rate regression.
- Prefer small, interpretable heuristic changes over giant rewrites.
"""


def _repair_prompt(error_report: str, candidate_source: str) -> str:
    return f"""The candidate failed local validation or compilation.

Error report:
{error_report}

Candidate source:
```python
{candidate_source}
```

Return a corrected complete standalone bot.py only. Do not add prose.
"""


def _metrics_prompt(metrics: str, candidate_source: str) -> str:
    return f"""The candidate passed validation but benchmark metrics were not good enough.

Benchmark metrics:
{metrics}

Candidate source:
```python
{candidate_source}
```

Revise the bot to improve benchmark acceptance. Focus on the regressed setups and bust-rate issues.
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
    passed = compile_result["returncode"] == 0 and validator_result and validator_result["returncode"] == 0
    if validator_json is not None:
        passed = passed and bool(validator_json.get("passed"))
    return {
        "passed": passed,
        "compile": compile_result,
        "validator": validator_result,
        "validator_json": validator_json,
    }


def _benchmark(base_path: str, new_path: str, args: argparse.Namespace, run_dir: Path) -> dict:
    cmd = [
        sys.executable,
        "sandbox/benchmark.py",
        "--old",
        base_path,
        "--new",
        new_path,
        "--iterations",
        str(args.benchmark_iterations),
        "--hands",
        str(args.hands),
        "--seed",
        str(args.seed),
        "--run-name",
        f"{run_dir.name}_benchmark",
        "--no-save-run",
        "--json",
    ]
    if args.max_bad_setups is not None:
        cmd.extend(["--max-bad-setups", str(args.max_bad_setups)])
    result = _run(cmd, timeout=args.benchmark_timeout)
    parsed = None
    if result["stdout"].strip():
        try:
            parsed = json.loads(result["stdout"])
        except json.JSONDecodeError:
            parsed = None
    return {"command": result, "json": parsed}


def _acceptance_passed(benchmark: dict) -> bool:
    parsed = benchmark.get("json")
    if not isinstance(parsed, dict):
        return False
    return bool(parsed.get("acceptance", {}).get("passed"))


def main():
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
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--max-bad-setups", type=int, default=0)
    parser.add_argument("--benchmark-timeout", type=int, default=600)
    parser.add_argument("--run-name")
    parser.add_argument("--hl-runs-dir", default=str(HL_RUNS_DIR), help="Immutable LLM run workspace root.")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--endpoint", default="https://api.deepseek.com/chat/completions")
    parser.add_argument("--temperature", type=float, default=0.4)
    parser.add_argument("--force", action="store_true", help="Reserved for compatibility; hl_runs never overwrites old runs.")
    parser.add_argument("--dry-run", action="store_true", help="Write prompt/context but do not call DeepSeek or modify bot files.")
    args = parser.parse_args()

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
    baseline_snapshots = _copy_all_baselines(run_dir)
    base_run_path = _ensure_base_snapshot(run_dir, baseline_snapshots, base_id, base_path)

    base_source_path = _source_file_for_bot(base_run_path)
    base_source = base_source_path.read_text()
    strategy_docs = _read_optional(ROOT / "bots" / "STRATEGY_COMPONENTS.md")
    metrics = _load_recent_metrics(args.metrics_run)
    initial_prompt = _initial_prompt(args.goal, args.base, new_token, base_source, strategy_docs, metrics)

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
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (run_dir / "initial_prompt.txt").write_text(initial_prompt)
    shutil.copyfile(base_source_path, run_dir / "base.pybak")

    if args.dry_run:
        print(f"Dry run written: {run_dir}")
        print(f"Prompt: {run_dir / 'initial_prompt.txt'}")
        return

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required unless --dry-run is used")

    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": initial_prompt},
    ]
    final_benchmark = None
    final_validation = None
    latest_candidate_dir = _latest_candidate_dir(run_dir, new_type, new_version)
    latest_candidate_path = latest_candidate_dir / "bot.py"
    accepted = False

    for round_index in range(1, args.improvement_rounds + 1):
        source = None
        for attempt in range(1, args.llm_attempts + 1):
            prompt_for_log = messages[-1]["content"]
            response = _deepseek_chat(messages, api_key, args.model, args.endpoint, args.temperature)
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
                break

            error_report = json.dumps(validation, indent=2)[:20000]
            messages = [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _repair_prompt(error_report, source)},
            ]
        else:
            raise SystemExit(f"Candidate failed validation after {args.llm_attempts} attempts. See {run_dir}")

        final_benchmark = _benchmark(base_run_path, str(latest_candidate_dir), args, run_dir)
        (run_dir / f"benchmark_round_{round_index:02d}.json").write_text(json.dumps(final_benchmark, indent=2) + "\n")
        accepted = _acceptance_passed(final_benchmark)
        if accepted or round_index == args.improvement_rounds:
            break

        metrics_text = json.dumps(final_benchmark.get("json") or final_benchmark, indent=2)[:30000]
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _metrics_prompt(metrics_text, source or latest_candidate_path.read_text())},
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
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
