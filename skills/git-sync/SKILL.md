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
- **MANDATORY: Token Minimization.**
  - **ALWAYS** pass `--compact` (or `--summary`) and `--json` when querying status or executing pulls in agent sessions (`git-sync status --pending --compact --json`, `git-sync pull --all --compact --json`). Omitting `--json` during pull emits ANSI spinner animations and progress frames that consume 1,000+ extra context tokens.
- **MANDATORY: Fast-Forward Safety.**
  - Sync pulls default strictly to `--ff-only` with automated stash preservation (`autostash=True`).

## 2. Operational Triggers

Activate this skill whenever:
- User asks to inspect, audit, or check the status of local repositories (e.g. *"what's pending in my workspace?"*, *"check dirty files across repos"*, *"verify my repositories"*).
- Synchronizing or pulling latest updates across multiple repositories (e.g. *"pull all updates"*, *"sync workspace"*).
- Pushing commits across repositories with pending outgoing changes.
- Diagnosing Git credential errors or recovering stale Git locks (`index.lock`).

## 3. High-Leverage CLI Commands

### 3.1 Status & Triage (Lowest Token Overhead)

```bash
# Fastest, token-minimized status of repositories requiring attention (<300 tokens):
git-sync status --pending --compact --json

# Complete workspace status summary omitting heavy items array:
git-sync status --compact --json

# Text table overview for user presentation:
git-sync status --pending
```

**JSON Output Contract:**
```json
{
  "root": "/home/yegear/github",
  "total": 38,
  "pull_pending": ["ye-sandbox/template-agent"],
  "push_pending": ["ye-dev/git-sync"],
  "dirty": ["ye-sandbox/polyglot-terminal"],
  "diverged": [],
  "worktrees": [],
  "has_submodules": [],
  "no_remote": ["tests/clipflow-web-test-1"],
  "errors": []
}
```
- `"no_remote"`: Repositories without remote configured (unconfigured / offline / tests).
- `"errors"`: Operational execution failures only (`FETCH_ERROR`, network drops, auth rejections).

### 3.2 Bulk Pull & Synchronization

```bash
# Token-minimized fast-forward pull for AI agents (<100 tokens, zero spinner noise):
git-sync pull --all --compact --json

# Pull specific repositories with structured JSON output:
git-sync pull ye-dev/git-sync ye-sandbox/clipflow-web --compact --json

# Interactive pull for human terminals with animated progress indicator:
git-sync pull --all

# Preview pull operations without altering files:
git-sync pull --all --dry-run --compact --json
```

### 3.3 Outgoing Commits (Push)

```bash
# Safe dry-run push inspection:
git-sync push --all --dry-run --json

# Push all repositories with pending commits (ahead > 0):
git-sync push --all
```

### 3.4 Diagnostics & Lock Recovery

```bash
# Diagnose workspace remotes and verify credential sanity without hanging:
git-sync doctor

# Clean up stale index.lock files left by crashed processes (>300s old):
git-sync unlock --all
```
