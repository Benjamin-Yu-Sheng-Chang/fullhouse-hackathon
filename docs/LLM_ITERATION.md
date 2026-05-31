# LLM Bot Iteration Guide

This document is included in DeepSeek prompts. It explains the local bot API,
validation rules, benchmark flow, and common mistakes to avoid.

## Bot API

Generated bots must be a single standalone `bot.py` file that defines:

```python
def decide(state: dict) -> dict:
    ...
```

`decide` must return one of:

```python
{"action": "fold"}
{"action": "check"}
{"action": "call"}
{"action": "raise", "amount": 1200}
{"action": "all_in"}
```

For raises, `amount` is the total bet amount for the street, not raise-by.

## State Schema

Use these exact state keys:

- `state["street"]`: `"preflop"`, `"flop"`, `"turn"`, or `"river"`.
- `state["pot"]`: total chips in the pot.
- `state["community_cards"]`: board cards, empty preflop.
- `state["current_bet"]`: highest bet this street.
- `state["min_raise_to"]`: minimum legal total raise amount.
- `state["amount_owed"]`: chips needed to call.
- `state["can_check"]`: true when `amount_owed == 0`.
- `state["your_cards"]`: your two hole cards.
- `state["your_stack"]`: your remaining chips.
- `state["your_bet_this_street"]`: chips already committed this street.
- `state["players"]`: public player states.
- `state["action_log"]`: current hand actions.
- `state.get("match_action_log", [])`: optional full-match action history.

Never use missing aliases such as `state["hand"]`, `state["your_hand"]`, or
`state["board"]`. The correct keys are `your_cards` and `community_cards`.

Cards are strings like `"As"` or `"Td"`. Ranks are `2 3 4 5 6 7 8 9 T J Q K A`.
Suits are `s h d c`.

## Safety And Validation

The validator checks that the bot compiles, defines `decide`, returns legal
actions, includes a top-of-file `STRATEGY OVERVIEW`, and does not crash on
preflop, postflop, large-bet, and short-stack states.

Do not import or use network, subprocess, threading, asyncio, pickle, importlib,
eval, exec, file writes, or reflection tricks. The bot should respond within
2 seconds per action.

## LLM Iteration Flow

`sandbox/llm_iterate.py` creates an immutable run under `hl_runs/`, copies all
baseline bots, writes prompts/responses, validates candidate code, then runs
selection benchmarks.

Each run records reproducibility and progress files:

- `command.txt`: replay-style shell command that launched the run.
- `argv.json`: exact Python argv list.
- `run.log`: timestamped progress log, including child validator/benchmark
  commands and return codes.
- `manifest.json`: settings, frozen baseline snapshots, and command metadata.
- `artifacts/selection/`: per-round staged selection JSON.
- `artifacts/curve/`: per-round curve benchmark JSON.

The default staged selector has:

- Validation gate: compile, validator, and `STRATEGY OVERVIEW`.
- Elimination gate: cheap paired benchmark against a small mixed table.
- Full gate: paired benchmark against a larger baseline field.
- Finalist gate: optional benchmark against strong bots and lineage candidates.

If a candidate fails selection and more improvement rounds remain, the next
DeepSeek prompt includes failed metrics so the strategy can be revised.

## Curve Benchmark

When `--curve-benchmark` is enabled, the run also writes diagnostic artifacts
under `curve_benchmark/`.

Training curve:

- Evaluates base, recent lineage candidates, and current candidate.
- Each candidate plays 20 deterministic 6-player tables by default.
- Each table is candidate plus 5 sampled baseline opponents.

Validation curve:

- Each lineage candidate plays heads-up against the other lineage candidates.
- This helps detect overfitting to fixed baseline opponents.

Curve artifacts include CSV, JSON, and two primary PNG files:

- `curve_scoreboard.png`: training and validation chip-delta vs original base.
- `curve_risk_validation.png`: top-half, win, and bust-rate trends.

Detailed per-metric PNGs are optional debugging output. Curve metrics are
diagnostic and prompt feedback only; staged selection still decides acceptance.

## Strategy Guidance

Prefer readable heuristic changes over giant rewrites. A good candidate should
have a distinct strategic idea, not a wholesale copy of another bot. Common
poker concepts such as pot odds, equity, position, stack pressure, board
texture, and opponent profiling are allowed when expressed as a materially
different heuristic mix.

When improving a simple baseline, preserve its useful identity and repair its
obvious leak:

- `aggressor`: keep pressure and initiative, but replace random raising with
  selective aggression, position-aware steals, value raises, and emergency fold
  discipline.
- `mathematician`: keep probability and pot-price reasoning, but add calibrated
  value betting, safer multiway calls, and short-stack protection.
- `ref_bot_2` / `potodds`: keep pot-odds-first discipline, but add basic hand
  strength, board texture, and value betting.
- `shark`: keep tight/exploitative play, but reduce missed value and avoid
  brittle position assumptions.
- `template`: add fundamentals only: tight hand selection, pot odds, value
  bets, and survival.

Useful defensive coding patterns:

- Use `state.get("match_action_log", [])` for optional history.
- Use `max(1, state["pot"])` before ratios.
- Cap raises by `state["your_stack"] + state["your_bet_this_street"]`.
- Check `state["can_check"]` before returning `check`.
- Avoid assuming fields like position, dealer, blinds, or opponent count exist.
