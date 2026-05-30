# LLM Workflow Scripts

Use `scripts/llm_workflows.sh` to run repeatable LLM bot iteration patterns.
Every LLM run is immutable and stored under `hl_runs/`.

Each run folder starts with a short hash, for example:

```text
hl_runs/885d3ca4f5_hl_hash_log_dry
```

Use that hash when talking about runs. The full mapping is also appended to:

```text
hl_runs/index.jsonl
```

## Workflows

- `smoke`
  - Small end-to-end test: DeepSeek call, bot extraction, validation, tiny benchmark.
  - Use after changing `.env`, prompts, TLS handling, or `sandbox/llm_iterate.py`.
  - Logs progress such as DeepSeek generation, validation, benchmark setup, and benchmark metrics.

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

ITER_STRONG=30 HANDS_STRONG=150 bash scripts/llm_workflows.sh promote-test hl_runs/some_run
```

## Bot Strategy Comments

The LLM driver now requires generated bots to include a top-of-file
`STRATEGY OVERVIEW` comment block. Validation fails if the comment is missing.
This makes later improvement prompts easier because the next LLM run can see
the bot's intended plan, thresholds, and likely weaknesses directly in code.
