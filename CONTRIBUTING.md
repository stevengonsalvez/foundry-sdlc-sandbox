# Contributing to parcelkit

parcelkit is a synthetic demo library. Contributions welcome for demo and PoC purposes.

## Development setup

```bash
pip install -e ".[dev]"
```

## Running tests

```bash
pytest
```

## Code style

- Plain Python, stdlib only (no external deps beyond pytest for tests).
- Docstrings for every public function.
- Known gaps are documented in the module-level docstring with a `KNOWN GAP N:` label.

## Submitting changes

1. Fork the repo.
2. Create a branch: `git checkout -b fix/my-fix`.
3. Commit with a short conventional commit message: `fix: handle lowercase postcode input`.
4. Open a pull request. CI runs `pytest` automatically.
