#!/usr/bin/env bash
set -euo pipefail

# LLM poker-bot workflow helper.
#
# All workflows create immutable experiments under hl_runs/.
# The canonical bots/ folder is only read as input; LLM candidates are written
# under hl_runs/<run>/bots/candidates/... and never overwrite old runs.
#
# Usage:
#   bash scripts/llm_workflows.sh list
#   bash scripts/llm_workflows.sh smoke
#   bash scripts/llm_workflows.sh branch-value hl_runs/hl_deepseek_smoke_test
#   bash scripts/llm_workflows.sh branch-anti-aggro hl_runs/hl_deepseek_smoke_test
#   bash scripts/llm_workflows.sh branch-survival hl_runs/hl_deepseek_smoke_test
#   bash scripts/llm_workflows.sh promote-test hl_runs/hl_deepseek_smoke_test
#   bash scripts/llm_workflows.sh compare-run hl_runs/hl_deepseek_smoke_test

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BASE_DEFAULT="${BASE:-short_stack_survivor:v1}"
METRICS_DEFAULT="${METRICS:-runs/bench_generated_10x100}"
ITER_SMOKE="${ITER_SMOKE:-1}"
HANDS_SMOKE="${HANDS_SMOKE:-20}"
ITER_BRANCH="${ITER_BRANCH:-4}"
HANDS_BRANCH="${HANDS_BRANCH:-80}"
ITER_STRONG="${ITER_STRONG:-12}"
HANDS_STRONG="${HANDS_STRONG:-120}"

usage() {
  cat <<'EOF'
LLM Bot Workflow Commands

  list
    Show available workflows and what each is for.

  smoke
    Minimal end-to-end API/validation/benchmark check. Use this after changing
    .env, certificate handling, prompts, or the LLM driver.

  branch-value <hl_run>
    Create a child candidate from an existing run's latest bot. Goal: improve
    value betting and passive/reference-table performance.

  branch-anti-aggro <hl_run>
    Create a child candidate from an existing run's latest bot. Goal: survive
    aggressive tables and reduce exploitability.

  branch-survival <hl_run>
    Create a child candidate from an existing run's latest bot. Goal: reduce
    bust rate and worst-match downside, even if upside drops slightly.

  branch-position <hl_run>
    Create a child candidate from an existing run's latest bot. Goal: improve
    late-position opens, steals, and c-bets without punting multiway.

  promote-test <hl_run>
    Run a stronger benchmark on a run's latest candidate against its baseline.
    This is the "is this actually worth promoting?" check.

  compare-run <hl_run>
    Compare a run's baseline and latest candidate directly in a small bake-off.

Environment overrides:
  BASE=short_stack_survivor:v1
  METRICS=runs/bench_generated_10x100
  ITER_SMOKE=1 HANDS_SMOKE=20
  ITER_BRANCH=4 HANDS_BRANCH=80
  ITER_STRONG=12 HANDS_STRONG=120
EOF
}

require_run() {
  local run="${1:-}"
  if [[ -z "$run" ]]; then
    echo "Missing hl_run argument." >&2
    usage
    exit 2
  fi
  if [[ ! -d "$run" ]]; then
    echo "Run folder not found: $run" >&2
    exit 2
  fi
}

candidate_latest() {
  local run="$1"
  local latest
  latest="$(find "$run/bots/candidates" -path '*/latest/bot.py' -print 2>/dev/null | sort | tail -1 || true)"
  if [[ -z "$latest" ]]; then
    echo "No latest candidate found under $run/bots/candidates" >&2
    exit 2
  fi
  dirname "$latest"
}

base_dir() {
  local run="$1"
  local base
  base="$(python3 - "$run" <<'PY'
import json, sys
from pathlib import Path
manifest = json.loads((Path(sys.argv[1]) / "manifest.json").read_text())
print(manifest["base"]["run_path"])
PY
)"
  if [[ ! -d "$base" ]]; then
    echo "Base dir from manifest not found: $base" >&2
    exit 2
  fi
  echo "$base"
}

run_name_suffix() {
  date -u +"%Y%m%dT%H%M%SZ"
}

llm_iterate() {
  python3 sandbox/llm_iterate.py "$@"
}

cmd="${1:-list}"
case "$cmd" in
  list|-h|--help|help)
    usage
    ;;

  smoke)
    llm_iterate \
      --base "$BASE_DEFAULT" \
      --new-version v2 \
      --goal "Smoke test: make a small safe improvement while preserving validation and low bust rate." \
      --metrics-run "$METRICS_DEFAULT" \
      --benchmark-iterations "$ITER_SMOKE" \
      --hands "$HANDS_SMOKE" \
      --seed 6100 \
      --max-bad-setups 4 \
      --run-name "hl_smoke_$(run_name_suffix)"
    ;;

  branch-value)
    run="${2:-}"
    require_run "$run"
    base="$(candidate_latest "$run")"
    llm_iterate \
      --base "$base" \
      --new-type short_stack_survivor \
      --new-version "v_value_$(run_name_suffix)" \
      --goal "Improve value betting and passive/reference-table performance. Keep short-stack survival unchanged and do not increase bust rate." \
      --metrics-run "$run/benchmark_round_01.json" \
      --benchmark-iterations "$ITER_BRANCH" \
      --hands "$HANDS_BRANCH" \
      --seed 6200 \
      --max-bad-setups 4 \
      --run-name "hl_branch_value_$(run_name_suffix)"
    ;;

  branch-anti-aggro)
    run="${2:-}"
    require_run "$run"
    base="$(candidate_latest "$run")"
    llm_iterate \
      --base "$base" \
      --new-type short_stack_survivor \
      --new-version "v_anti_aggro_$(run_name_suffix)" \
      --goal "Improve aggressive-table performance. Tighten against repeated raises, reduce marginal calls, and preserve zero or near-zero bust regression." \
      --metrics-run "$run/benchmark_round_01.json" \
      --benchmark-iterations "$ITER_BRANCH" \
      --hands "$HANDS_BRANCH" \
      --seed 6300 \
      --max-bad-setups 4 \
      --run-name "hl_branch_anti_aggro_$(run_name_suffix)"
    ;;

  branch-survival)
    run="${2:-}"
    require_run "$run"
    base="$(candidate_latest "$run")"
    llm_iterate \
      --base "$base" \
      --new-type short_stack_survivor \
      --new-version "v_survival_$(run_name_suffix)" \
      --goal "Reduce bust rate and worst-match downside. Prefer robust top-half finishes over high-variance chip spikes." \
      --metrics-run "$run/benchmark_round_01.json" \
      --benchmark-iterations "$ITER_BRANCH" \
      --hands "$HANDS_BRANCH" \
      --seed 6400 \
      --max-bad-setups 4 \
      --run-name "hl_branch_survival_$(run_name_suffix)"
    ;;

  branch-position)
    run="${2:-}"
    require_run "$run"
    base="$(candidate_latest "$run")"
    llm_iterate \
      --base "$base" \
      --new-type short_stack_survivor \
      --new-version "v_position_$(run_name_suffix)" \
      --goal "Improve late-position opening, stealing, and c-betting. Avoid wider calls out of position and avoid multiway spew." \
      --metrics-run "$run/benchmark_round_01.json" \
      --benchmark-iterations "$ITER_BRANCH" \
      --hands "$HANDS_BRANCH" \
      --seed 6500 \
      --max-bad-setups 4 \
      --run-name "hl_branch_position_$(run_name_suffix)"
    ;;

  promote-test)
    run="${2:-}"
    require_run "$run"
    old="$(base_dir "$run")"
    new="$(candidate_latest "$run")"
    python3 sandbox/benchmark.py \
      --old "$old" \
      --new "$new" \
      --iterations "$ITER_STRONG" \
      --hands "$HANDS_STRONG" \
      --seed 7000 \
      --run-name "promote_test_$(basename "$run")_$(run_name_suffix)"
    ;;

  compare-run)
    run="${2:-}"
    require_run "$run"
    old="$(base_dir "$run")"
    new="$(candidate_latest "$run")"
    python3 sandbox/compare.py \
      --bots "$old" "$new" shark mathematician ref_bot_2 position_bully:v2 equity_guard:v2 \
      --iterations "$ITER_BRANCH" \
      --hands "$HANDS_BRANCH" \
      --seed 7100 \
      --run-name "compare_$(basename "$run")_$(run_name_suffix)"
    ;;

  *)
    echo "Unknown workflow: $cmd" >&2
    usage
    exit 2
    ;;
esac
