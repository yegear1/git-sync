---
name: git-sync
description: High-speed bulk Git repository inspection, fast-forward pull, autostash, and status triage across developer workspaces (~/github). Trigger whenever checking status, pulling, pushing, auditing, or resolving Git repositories across the workspace instead of executing ad-hoc bash git loops.
---

# Git Sync CLI (`git-sync`)

Canonical operational directives for AI agents inspecting, auditing, synchronizing, and resolving multiple Git repositories across developer workspaces (`~/github` or configured root).

## 1. System Invariants & Anti-Patterns

- **FORBIDDEN ACTION: Ad-hoc Bash Loops.**
  - **NEVER** run commands like `for d in */; do (cd $d && git status); done` or `find . -name .git -exec ...`.
  - Ad-hoc loops consume 20,000+ context tokens, execute sequentially, and risk hanging permanently on interactive credentials.
- **FORBIDDEN ACTION: Interactive Git Hanging.**
  - **NEVER** run background Git operations without `GIT_TERMINAL_PROMPT=0`.
- **MANDATORY: Token Minimization (Agent-First Defaults).**
  - `git-sync` defaults globally to `--json` and `--compact` (`json=True`, `compact=True`). Invoking `git-sync status`, `git-sync pull --all`, `git-sync push --all`, or `git-sync doctor` automatically emits token-minimized JSON payloads with zero ANSI spinner noise.
  - Use `--full` (or `--no-compact`) only when you specifically need the full per-repository `items` array in `status` or full unmodified payloads.
  - Use `--human` (or `--no-json`, `--table`, `--text`) only when rendering formatted tables or progress bars for human terminal inspection.
- **MANDATORY: Fast-Forward Safety.**
  - Sync pulls default strictly to `--ff-only` with automated stash preservation (`autostash=True`).

## 2. Operational Triggers

Activate this skill whenever:
- User asks to inspect, audit, or check the status of local repositories (e.g. *"what's pending in my workspace?"*, *"check dirty files across repos"*, *"verify my repositories"*).
- Synchronizing or pulling latest updates across multiple repositories (e.g. *"pull all updates"*, *"sync workspace"*).
- Pushing commits across repositories with pending outgoing changes.
- Diagnosing Git credential errors or recovering stale Git locks (`index.lock`).

## 3. High-Leverage CLI Commands

### 3.1 Status & Triage (Lowest Token Overhead by Default)

```bash
# Fastest, token-minimized status of repositories requiring attention (<300 tokens, compact JSON by default):
git-sync status --pending

# Complete workspace status summary omitting heavy items array (compact JSON by default):
git-sync status

# Detailed status JSON including full per-repository items array:
git-sync status --full

# Human-readable text table overview for terminal presentation:
git-sync status --pending --human
```

**JSON Output Contract (Default Compact Mode):**
```json
{
  "root": "/home/user/github",
  "total": 38,
  "pull_pending": ["org/template-agent"],
  "push_pending": ["org/git-sync"],
  "dirty": ["org/terminal-ui"],
  "diverged": [],
  "worktrees": [],
  "has_submodules": [],
  "no_remote": ["tests/sample-repo-test-1"],
  "errors": []
}
```
- `"push_pending"`: Repositories with `PUSH_PENDING` or `AUX_PUSH_PENDING` (unpushed commits on auxiliary branches).
- `"no_remote"`: Repositories without remote configured (unconfigured / offline / tests).
- `"errors"`: Operational execution failures only (`FETCH_ERROR`, network drops, auth rejections).
- The ten keys above are always present. In compact mode, secondary keys are **omitted when empty**: `stale_worktrees`, `submodule_drift`, `shallow_submodules`, `has_lfs`, `lfs_pointers`, `lfs_warnings`, `sparse_checkout`, `sparse_cone_warnings`, `no_upstream`, `offline`, `multi_remote`, `worktree_health`, `remediations`. Absent key = healthy/empty. `--full` (`--no-compact`) emits every key plus the `items` array.

### 3.2 Bulk Pull & Synchronization

```bash
# Token-minimized fast-forward pull for AI agents (<100 tokens, compact JSON by default, zero spinner noise):
git-sync pull --all

# Pull specific repositories with structured JSON output (default):
git-sync pull org/git-sync org/repo-b

# Interactive pull for human terminals with animated progress indicator:
git-sync pull --all --human

# Preview pull operations without altering files (JSON by default):
git-sync pull --all --dry-run
```

### 3.3 Outgoing Commits (Push)

```bash
# Safe dry-run push inspection (JSON by default):
git-sync push --all --dry-run

# Push all repositories with pending commits (ahead > 0):
git-sync push --all
```

### 3.4 Diagnostics & Lock Recovery

```bash
# Diagnose workspace remotes and verify credential sanity without hanging (JSON by default, add --human for table):
git-sync doctor

# Clean up stale index.lock files left by crashed processes (>300s old):
git-sync unlock --all
```
