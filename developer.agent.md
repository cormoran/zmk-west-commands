# Developer agent guide

This repository is a west extension that adds `zmk-build` and `zmk-test` commands for ZMK users. Use this guide to quickly gather the full context before making changes.

## Files to read first
- `README.md`: User-facing description, usage examples, and development setup.
- `scripts/zmk_build.py` and `scripts/zmk_test.py`: Main command implementations.
- `scripts/west-commands.yml`: Declares the west extensions.
- `test.py` and `tests/`: Fixtures and expectations for the west commands (build/test samples, `build.yaml` examples).
- `west.yml` and `scripts/west-test*.yml`: Workspace layouts for dependency checkout; helpful when touching manifests.
- `requirements.txt`: Optional interactive dependency (`questionary`).

## Working notes for the coding agent
- Treat `dependencies/` as vendor checkouts (zephyr, zmk, etc.); read for reference only and avoid editing unless the task explicitly requires it.
- Prefer ripgrep/glob to locate code or fixtures, then read whole files to understand context rather than snippets.
- Keep user-facing messaging concise—README should stay friendly for end users.

## How to validate changes
- From the repo root, run `python -m unittest` (Linux only). This exercises `west zmk-build` and `west zmk-test` via the fixtures; it can take a few minutes and requires `west` plus the dependencies fetched via the provided manifests.
- If you only adjust documentation, still ensure commands load (import errors) by running the tests when feasible.
