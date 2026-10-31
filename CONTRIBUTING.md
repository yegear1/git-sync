# Contributing to git-sync

Thank you for your interest in contributing to **git-sync**!

`git-sync` is a high-speed, concurrent multi-repository status auditor and synchronization CLI designed for both developer terminals and autonomous AI agents.

---

## Core Engineering Invariants

Before submitting a Pull Request, please ensure your contribution adheres to our core design principles:

1. **Zero External Runtime Dependencies:**
   `git-sync` uses **only** the Python standard library (`argparse`, `concurrent.futures`, `subprocess`, `json`, `pathlib`, `dataclasses`, etc.). Third-party runtime dependencies (e.g., `requests`, `rich`, `click`) are strictly prohibited to ensure instant startup, zero install friction, and maximum portability across environments.

2. **Terminal Prompt Suppression (`GIT_TERMINAL_PROMPT=0`):**
   All Git subprocess calls must run with interactive prompts suppressed to prevent headless agent sessions and background tasks from hanging indefinitely.

3. **Fast-Forward Safety by Default:**
   All bulk synchronization commands enforce `--ff-only`. Auxiliary branch synchronization must not perform destructive resets or introduce unintended merge commits.

4. **Strict Typing and Clean Compilation:**
   All Python source code must compile cleanly under Python 3.10+ (`python3 -m py_compile`) and adhere to strict typing standards.

---

## Development Setup

Clone the repository and set up your local development environment:

```bash
git clone https://github.com/yegear1/git-sync.git
cd git-sync

# Run the CLI in development mode using PYTHONPATH
PYTHONPATH=src python3 -m git_sync.cli --help
PYTHONPATH=src python3 -m git_sync.cli status --no-fetch
```

---

## Validation & Test Suite

All contributions must pass the canonical verification suite before opening a PR:

```bash
# 1. Run all unit and integration tests (must exit code 0)
python3 -m unittest discover -s tests

# 2. Bytecode compilation and syntax checks
python3 -m py_compile src/git_sync/*.py tests/*.py scripts/*.py

# 3. Packaging and distribution hygiene check
python3 scripts/build.py

# 4. Git diff and whitespace hygiene check
git diff --check
```

---

## Commit Guidelines

We enforce [Conventional Commits](https://www.conventionalcommits.org/) in English:

- `feat(scope): add new capability`
- `fix(scope): fix bug or edge case with regression test`
- `refactor(scope): refactor code without changing behavior`
- `test(scope): add or improve test coverage`
- `docs(scope): documentation or ADR updates`
- `chore(scope): build, packaging, or tooling updates`

---

## Submitting Pull Requests

1. Fork the repository and create your feature branch from `main`.
2. Ensure new features or bug fixes include dedicated unit tests in `tests/`.
3. Verify all validation commands pass with exit code 0.
4. Open a Pull Request against the `main` branch with a clear summary of changes and motivations.
