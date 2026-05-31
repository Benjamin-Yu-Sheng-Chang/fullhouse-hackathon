# Abstract CFR Bot

This folder contains a submission-friendly CFR-style bot:

- `bot.py` loads `data/cfr_strategy.npz` at import time and answers via `decide(game_state)`.
- `trainer/train.py` trains a compact abstract regret table and exports the runtime `.npz`.
- `trainer/abstraction.py` contains the richer shared abstraction code used by training.

The trainer is deliberately abstract. It samples Fullhouse-shaped states, buckets hands/boards/position/pressure/history, estimates equity by Monte Carlo, updates regrets, and stores average action probabilities. It is not exact multiway no-limit CFR, but it gives you a portable blueprint table that can be trained much harder elsewhere.

## Smoke Test

From the repo root:

```bash
make cfr-smoke
```

That creates and validates the actual upload-shaped archive:

```text
bots/cfr/data/cfr_strategy.npz
bots/cfr/dist/cfr_bot.zip
```

The zip contains only:

```text
bot.py
data/cfr_strategy.npz
```

## Bigger Training Run

On the other laptop, start with something like:

```bash
pixi run python bots/cfr/trainer/train.py \
  --iterations 200000 \
  --samples 8 \
  --equity-samples 48 \
  --output bots/cfr/data/cfr_strategy.npz \
  --log-interval 5000
```

The useful knobs are:

- `--iterations`: more abstract states visited.
- `--samples`: more sampled states per iteration.
- `--equity-samples`: less noisy equity estimates, slower per state.
- `--output`: where the bot will load the trained table from.

After training:

```bash
rm -rf bots/cfr/dist
mkdir -p bots/cfr/dist
pixi run python -c "import zipfile; z=zipfile.ZipFile('bots/cfr/dist/cfr_bot.zip', 'w', zipfile.ZIP_DEFLATED); z.write('bots/cfr/bot.py', 'bot.py'); z.write('bots/cfr/data/cfr_strategy.npz', 'data/cfr_strategy.npz'); z.close()"
pixi run python sandbox/validator.py bots/cfr/dist/cfr_bot.zip
```

For upload, use `bots/cfr/dist/cfr_bot.zip`.

## Training Curve Experiment

To train cumulative checkpoints and benchmark the actual zip artifacts every
10k iterations:

```bash
pixi run python bots/cfr/trainer/curve_experiment.py \
  --max-iterations 200000 \
  --step-iterations 10000 \
  --samples 8 \
  --equity-samples 48 \
  --benchmark-iterations 5 \
  --hands 400 \
  --seed 7000 \
  --output-dir bots/cfr/experiments/curve_200k
```

Smoke test:

```bash
make cfr-curve-smoke
```

The experiment writes:

```text
summary.json
curve.png
curve.csv
per_seed_results.jsonl
checkpoints/cfr_*.npz
zips/cfr_*.zip
```

Each benchmark uses the generated zip, not the source folder.
