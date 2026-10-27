# git-sync

Fast, concurrent multi-repository status auditor and synchronization CLI tool for local Git and GitHub workspaces.

Designed for both developers in the terminal and **AI agents** (such as Antigravity) to minimize context token consumption and latency.

---

## Key Features

- **Concurrent Execution:** Scans dozens of repositories in parallel via Python `ThreadPoolExecutor` (scans 30+ repos in ~2 seconds).
- **Zero External Dependencies:** Built entirely with standard library Python 3.12+ (no `pip install` required).
- **Hangs Prevention:** Suppresses interactive prompts via `GIT_TERMINAL_PROMPT=0` and enforces strict timeouts.
- **Safe Synchronization:** Pulls default to `--ff-only` and atomically updates auxiliary tracking branches without worktree switching.
- **Token-Dense for AI Agents:**
  - `--json`: Outputs structured, compact JSON with `pull_pending`, `push_pending`, `dirty`, `errors`.
  - `--pending`: Filters down to only repositories requiring attention.
  - `--no-fetch`: Instant local status inspection (<0.5s).

---

## Global Installation

Run the safe, idempotent installer command:

```bash
# Automated installation into ~/.local/bin/git-sync
git-sync install

# Or run the bootstrap script from the repository
./scripts/install.sh
```

To enable shell autocompletion (Bash & Zsh):

```bash
# Bash completion:
git-sync completion bash --install
# or evaluate directly:
eval "$(git-sync completion bash)"

# Zsh completion:
git-sync completion zsh --install
```

---

## Usage

```bash
# Full status with parallel remote fetch
git-sync status

# Instant local status check (no network)
git-sync status --no-fetch

# Show only repositories with pending pull/push or dirty files
git-sync status --pending

# JSON format for AI agents
git-sync status --pending --json

# Pull all repositories with upstream commits
git-sync pull --all

# Pull specific repositories
git-sync pull taskctl template-agent

# Push commits across all repositories with unpushed commits (dry-run first)
git-sync push --dry-run --all

# Push commits for all pending repositories
git-sync push --all

# Push commits for specific repositories
git-sync push taskctl template-agent

# List modified / untracked files across all repositories
git-sync dirty
```

---

## Testing & CI/CD

Run canonical unit tests and compile checks:

```bash
# Run all unit tests
python3 -m unittest discover -s tests

# Check syntax and bytecode compilation
python3 -m py_compile src/git_sync/*.py tests/*.py scripts/*.py
```

Automated continuous integration is configured in `.github/workflows/ci.yml`, running tests across Python 3.10, 3.11, 3.12, and 3.13, validating git hygiene, CLI execution, installer scripts, and Twine package validation.

---

## Release Packaging & Distribution Hygiene

Build and audit clean source distribution (`sdist`) and binary wheel (`whl`) artifacts with zero external runtime dependencies:

```bash
# Build and audit distribution packages
python3 scripts/build.py

# Audit existing packages in dist/
python3 scripts/build.py --check-dist

# Clean transient build artifacts
python3 scripts/build.py --clean

# Install wheel package locally
pip install dist/git_sync-*.whl
```
