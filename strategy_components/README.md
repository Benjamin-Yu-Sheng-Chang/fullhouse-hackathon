# Strategy Components

This package is build-time only. Generated bots must not import it at runtime.

Use:

```bash
python3 sandbox/build_bot.py --strategy adaptive_hybrid --version v1
```

The builder writes a complete standalone bot to:

```text
bots/<strategy>/<version>/bot.py
```

The source of truth for generated strategies is `registry.py`; the emitted bot
runtime is defined in `templates.py`.
