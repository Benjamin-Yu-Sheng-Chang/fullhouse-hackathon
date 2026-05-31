# LLM Workflow Scripts

Use `scripts/llm_workflows.sh` to run repeatable LLM bot iteration patterns.
Every LLM run is immutable and stored under `hl_runs/`.

Each run folder starts with a compact UTC timestamp plus a short hex id, so folders sort in run order and are easy to reference:

```text
hl_runs/260530_141522_885d3ca4_hl_smoke
```

Use either the folder name or the short hex id when talking about runs. The full mapping is also appended to:

```text
hl_runs/index.jsonl
```

Each LLM run also records `command.txt`, `argv.json`, and `run.log` so you can
see the exact command and progress messages that produced it.
Verbose per-round JSON lives under `artifacts/selection/` and
`artifacts/curve/`, keeping the run root focused on the files you usually read.

## Workflows

- `smoke`
  - Small end-to-end test: DeepSeek call, bot extraction, validation, tiny staged selection.
  - Use after changing `.env`, prompts, TLS handling, or `sandbox/llm_iterate.py`.
  - Logs progress such as DeepSeek generation, validation, elimination/full benchmark setup, and gate metrics.

- `branch-value <hl_run>`
  - Starts from an existing run's `latest` candidate.
  - Asks the LLM to improve value betting and passive/reference performance.
  - Good when a bot survives well but misses chip-earning spots.

- `branch-anti-aggro <hl_run>`
  - Starts from an existing run's `latest` candidate.
  - Asks the LLM to reduce losses versus high-aggression tables.
  - Good when benchmark regressions show up in aggressive setups.

- `branch-survival <hl_run>`
  - Starts from an existing run's `latest` candidate.
  - Optimizes for lower bust rate and better worst-match downside.
  - Good before promoting a high-upside but swingy bot.

- `branch-position <hl_run>`
  - Starts from an existing run's `latest` candidate.
  - Improves late-position opens, steals, and c-bets.
  - Good when passive/reference tables are beatable but the bot is too quiet.

- `promote-test <hl_run>`
  - Runs a stronger benchmark for candidate versus its recorded baseline.
  - Use this before copying any candidate into canonical `bots/`.

- `compare-run <hl_run>`
  - Puts the baseline, candidate, and selected known baselines in one bake-off.
  - Useful for quick qualitative ranking after a benchmark.

## Examples

```bash
bash scripts/llm_workflows.sh smoke

bash scripts/llm_workflows.sh branch-value hl_runs/hl_deepseek_smoke_test

bash scripts/llm_workflows.sh branch-anti-aggro hl_runs/hl_deepseek_smoke_test

bash scripts/llm_workflows.sh promote-test hl_runs/hl_deepseek_smoke_test
```

## Tuning

Override budgets with environment variables:

```bash
ITER_BRANCH=8 HANDS_BRANCH=100 bash scripts/llm_workflows.sh branch-value hl_runs/some_run

ELIM_ITER_BRANCH=4 ELIM_HANDS_BRANCH=80 FULL_ITER_BRANCH=12 FULL_HANDS_BRANCH=120 bash scripts/llm_workflows.sh branch-survival hl_runs/some_run

ITER_STRONG=30 HANDS_STRONG=150 bash scripts/llm_workflows.sh promote-test hl_runs/some_run
```

`sandbox/llm_iterate.py` defaults to staged selection:

```bash
python3 sandbox/llm_iterate.py \
  --base short_stack_survivor \
  --new-version v2 \
  --goal "Improve value spots without increasing bust rate." \
  --selection-profile staged
```

The staged selector first runs a cheap elimination benchmark, then a fuller
diverse-baseline benchmark, then a finalist check unless `--skip-finalist-check`
is set.

Enable the diagnostic 20-group training curve with:

```bash
CURVE_BENCHMARK=1 CURVE_HANDS=400 bash scripts/llm_workflows.sh branch-value hl_runs/some_run
```

This writes `curve_benchmark/` artifacts inside the LLM run, including
`training_curve.csv`, `training_summary.csv`, `training_groups.json`,
`validation_curve.csv`, `validation_summary.csv`, `validation_groups.json`, and
two primary PNG plots when `matplotlib` is installed:

- `curve_scoreboard.png`
- `curve_risk_validation.png`

Detailed per-metric PNGs are only written with `--curve-detailed-plots`, or
`CURVE_DETAILED_PLOTS=1` when using `scripts/llm_workflows.sh`. The curve is
diagnostic: it is saved and fed back to DeepSeek on failed candidates, but
staged selection still decides acceptance.
Training groups exclude `shark` and `ref_bot_2` by default.

## Bot Strategy Comments

The LLM driver now requires generated bots to include a top-of-file
`STRATEGY OVERVIEW` comment block. Validation fails if the comment is missing.
This makes later improvement prompts easier because the next LLM run can see
the bot's intended plan, thresholds, and likely weaknesses directly in code.
The prompt also tells the model not to copy another bot's strategy, thresholds,
structure, or comments wholesale; common poker concepts are allowed only as part
of a distinct heuristic mix.
