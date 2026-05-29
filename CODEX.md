# Fullhouse Hackathon Codebase Notes

## Project Purpose

This repo is the local engine and tooling for Fullhouse Hackathon, a Python No-Limit Texas Hold'em bot competition. Participants submit a bot with one `decide(game_state: dict) -> dict` function, then use this repo to validate bots, run local matches, and inspect reference strategies.

## Architecture

- `engine/`: Core poker/tournament logic. `engine/game.py` owns No-Limit Texas Hold'em hand state, legal action validation, betting flow, side pots, showdown resolution, chip invariants, and state serialization. `engine/tournament.py` contains Swiss pairing, standings, and finalist helpers.
- `sandbox/`: Bot execution and validation. `sandbox/runner.py` loads a submitted bot and speaks newline-delimited JSON over stdin/stdout. `sandbox/match.py` orchestrates multi-hand matches and can run bots locally or in Docker. `sandbox/validator.py` performs static and runtime submission checks.
- `bots/`: Starter and reference bots used for examples, validation, demo matches, and local bake-offs.
- `demo.py`: Flask single-page local demo that runs real matches/tournaments and streams leaderboard/replay updates in memory.
- `tests/`: Engine-focused tests, currently in `tests/test_engine.py`.
- `db/`: Supabase/Postgres schema for production portal concepts such as users, bots, tournaments, matches, hands, winners, and leaderboard.
- `data/`: Experiment notebooks and dataset-loading helpers. Bots may ship optional read-only `data/` payloads in submissions, but gameplay-time file I/O is forbidden.

## Core Flow

`sandbox/match.py` prepares each bot path (`bot.py`, bot directory, or `bot.zip`), starts one `BotProcess` per bot, warms each bot once, and then creates a `PokerEngine` for each hand. For every action request, it sends the serialized game state to `sandbox/runner.py`, receives the bot's action dict, appends match action history, and applies the action through `engine.game.PokerEngine`. When the hand completes, final stacks feed into the next hand and match results include chip deltas, bot errors, and hand histories.

`PokerEngine` is the rules authority: it posts blinds, deals deterministic or random decks, tracks active/folded/all-in states, validates actions, advances streets, computes side pots, resolves showdowns with `eval7`, emits replay events, and checks chip conservation.

## Bot Contract

Bots expose:

```python
def decide(game_state: dict) -> dict:
    return {"action": "call"}
```

Valid actions are:

- `{"action": "fold"}`
- `{"action": "check"}` when `can_check` is true
- `{"action": "call"}`
- `{"action": "raise", "amount": 1200}` where `amount` is the total bet, not raise-by
- `{"action": "all_in"}`

Invalid actions default to fold or are normalized by the engine. Raises below the legal minimum are snapped to the minimum legal total. Bots get a 2 second per-action budget; crashes, exceptions, and timeouts auto-fold for that action.

Sandbox rules matter: no network calls, no subprocesses, no file writes during gameplay, no threading/async tricks, and no reflection escape attempts. Optional submission data may live in `data/`, up to the documented limits, and should be loaded at module import time via `BOT_DATA_DIR`.

## Important Commands

```bash
make install
python3 demo.py
python3 -m pytest tests/ -q
make validate BOT=bots/template/bot.py
python3 sandbox/match.py bots/template/bot.py bots/shark/bot.py --hands 400
```

Useful Docker sandbox helpers live in `sandbox.sh`:

```bash
./sandbox.sh build
./sandbox.sh test bots/template/bot.py
./sandbox.sh match bots/template/bot.py bots/shark/bot.py
./sandbox.sh security-check
```

## Development Cautions

- `CONTRIBUTING.md` documents `engine/game.py`, `sandbox/runner.py`, and `db/schema.sql` as frozen before the event. Treat edits there as high-risk unless explicitly requested.
- The README/Docker path documents Python 3.10 with `eval7==0.1.7`, because that version has build constraints around Python 3.11. The current `pixi.toml` uses Python 3.11 and `eval7==0.1.10`; note this environment mismatch rather than changing it casually.
- The current worktree already has modifications to `pixi.toml` and `pixi.lock`. Do not overwrite or revert those changes unless the user explicitly asks.
- `requirements.txt` is a participant/sandbox reference, not a reliable one-shot install file; the README and `Makefile` use a special two-step install for `eval7`.
- Keep bot protocol changes synchronized across `README.md`, `bots/template/bot.py`, `sandbox/validator.py`, `sandbox/runner.py`, and `sandbox/match.py`.

## Testing Expectations

- Engine or poker-rule changes should add/update coverage in `tests/test_engine.py` and run `python3 -m pytest tests/ -q`.
- Sandbox, validator, or bot contract changes should validate a reference bot with `make validate BOT=bots/template/bot.py`.
- Match orchestration changes should run a short local match before relying on a full 400-hand run.
- Demo UI changes should be checked through `python3 demo.py` at `http://localhost:5001` or an alternate `DEMO_PORT`.
- Documentation-only changes usually need inspection only; avoid running unnecessary commands that might mutate unrelated generated files.
