# Contributing

Thanks for your interest! This is a personal project shared as-is. Issues and pull requests are
welcome and are looked at on a best-effort basis.

## Ground rules

- **Never commit real data.** That means real backups, databases, message text, names, phone numbers
  or screenshots, whether in code, tests, issues or pull requests. All tests use the synthetic builder
  in `tests/fixture.py`. Extend it when you need a new scenario.
- **Never weaken the safety model.**
  - The archive is append-only (enforced by triggers in `store.py`).
  - Backups are read-only.
  - "Safe to clear" requires media to be archived, hash-verified and uploaded.
  - Message contents never go to logs.
- **Changing the archive format** (the schema in `store.py`) needs a version bump and a migration,
  so existing archives keep working.

## Setup

```sh
uv sync --all-groups
uv run --group crosscheck pytest
```

Keep pull requests focused and include tests. If your change affects how WhatsApp data is read,
please mention which iOS and WhatsApp versions you tested with.
