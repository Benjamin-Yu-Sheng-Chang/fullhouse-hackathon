# Strategy Components

Composable bots are built from reusable strategy topics. The generated output
must still be a standalone `bot.py`, so shared code lives in
`strategy_components/` for development and is embedded into generated bot
versions by `sandbox/build_bot.py`.

## Component Topics

- `hand`
  - Preflop score from rank, suitedness, pairs, gaps, Broadway/ace bonuses.
  - Postflop equity using bounded Monte Carlo rollouts with `eval7`.
  - Made-hand and draw flags: pair/two-pair-ish, flush draw, straight draw.

- `position`
  - Position factor from acting seat within the current table.
  - Labels: `early`, `middle`, `late`.
  - Live opponent count and multiway/head-up pressure.

- `pot_price`
  - Call price as `amount_owed / (pot + amount_owed)`.
  - Bet size category from owed-to-pot ratio.
  - Equity margin required to continue.

- `stack`
  - Stack in big blinds and stack-to-pot ratio.
  - Modes: `short`, `normal`, `deep`.
  - Optional short-stack shove/fold override.

- `opponent`
  - Table raise/fold/call rates from `match_action_log`.
  - Labels: `passive`, `balanced`, `aggressive`, `foldy`.
  - Used to steal more, trap more, or tighten up.

- `initiative`
  - Street aggression count.
  - Whether current bet is unopened/limped preflop.
  - Whether the bot has a good c-bet or probe-bet opportunity.

- `board`
  - Wet/dry board proxy from flush and straight draw density.
  - Paired board and danger flags.
  - Used to size value/protection bets and avoid weak floats.

- `policy`
  - Final action composer.
  - Applies short-stack policy first, then preflop policy, then postflop policy.
  - Converts component signals into `fold`, `check`, `call`, or `raise`.

## Generation Flow

Every composed bot follows this internal flow:

```python
features = extract_features(state)
adjustments = profile_table(state)
if short_stack_policy_applies(features):
    return short_stack_action(features)
if state["street"] == "preflop":
    return preflop_policy(features, adjustments)
return postflop_policy(features, adjustments)
```

## Current Generated Families

- `equity_position/v1`
  - Equity guard core plus position-aware preflop opens and postflop c-bets.

- `anti_aggro/v1`
  - Tight preflop range, trap/value orientation, and caution versus high table
    raise rates.

- `short_stack_survivor/v1`
  - Conservative normal play with an explicit short-stack jam/fold override.

- `adaptive_hybrid/v1`
  - Hand equity, position pressure, opponent profile adjustment, and stack mode.

## Evaluation Rule of Thumb

For a new generated version, prefer accepting it only when:

- It beats the previous version in direct head-to-head.
- It improves paired-seed average delta against mixed fields.
- It does not increase bust rate by more than `0.10`.
- It has no major setup regression in passive, reference, aggressive, or mixed
  benchmark setups.
