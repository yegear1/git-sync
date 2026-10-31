---
id: "k-013"
title: "Submodule Drift Detection, Recursive Updates, and Autostash Isolation"
date: 2026-10-31
tags:
  - "git"
  - "submodules"
  - "drift"
  - "autostash"
severity: "medium"
affected_components:
  - "src/git_sync/engine/git.py"
  - "src/git_sync/cli.py"
symptoms:
  - "Submodules remaining out of sync (+ SHA) after git pull"
  - "Failed to restore stash: No stash entries found when only submodule pointer was modified"
  - "Uncoordinated recursive submodule checkout causing detached HEAD warnings or interactive hangs"
---

# [k-013] Submodule Drift Detection, Recursive Updates, and Autostash Isolation

## Problem / Symptom

When managing repositories containing Git submodules:
1. `git pull` in the superproject updates repository tracking refs and checked-in tree entries, but does NOT automatically update checked-out submodules. Submodules drift into a detached or modified commit state (`+` prefix in `git submodule status`).
2. If the ONLY change in the superproject working copy is an out-of-sync submodule pointer (` M vendor/sub`), `git status --porcelain` reports dirty, but `git stash push -u` refuses to stash submodules, outputting `No local changes to save` with exit code 0. Naive autostash handlers that assume exit code 0 implies a created stash fail during `git stash pop` with `No stash entries found.`.
3. Updating submodules without `GIT_TERMINAL_PROMPT=0` can hang background agent processes indefinitely if a submodule remote requires SSH key passphrase or HTTP credentials.

---

## Approved Solution

1. **Deterministic Drift Classification:**
   - Pre-check `.gitmodules` on disk to avoid subshell overhead on standard repositories.
   - Run `git submodule status --recursive` with `GIT_TERMINAL_PROMPT=0`.
   - Parse status prefixes: `' '` (UP_TO_DATE), `'+'` (MODIFIED / DRIFT), `'-'` (UNINITIALIZED), `'U'` (CONFLICT).
   - If `detailed` inspection is requested, audit `branch --show-current` within initialized submodule directories to flag detached HEAD states.
2. **Autostash `No local changes to save` Detection:**
   - In `stash_changes`, inspect combined stdout and stderr. If `No local changes to save` is returned, return `(True, tag, "NO_CHANGES")`.
   - In `pull_repo`, verify `stash_err != "NO_CHANGES"` before flagging `stashed = True`, preventing phantom stash restorations.
   - In `restore_stash`, handle `No stash entries found` gracefully as a successful no-op.
3. **Recursive Submodule Synchronization:**
   - Provide `update_submodules(repo_path, recursive=True, init=True, timeout=..., dry_run=False)`.
   - In `dry_run` mode, preview pending submodule updates without executing checkouts.
   - Surface actionable remediations (`action_required: update_submodules`, `remediation: git -C '<path>' submodule update --init --recursive`) in agent JSON summaries.

---

## What NEVER to Do

- ❌ **Anti-Pattern:** Assume `git stash push` creates a stash entry whenever `git status --porcelain` is non-empty.
  - *Why:* Git refuses to stash dirty submodules with `git stash push -u`; attempting `git stash pop` afterwards fails with `No stash entries found`.
- ❌ **Anti-Pattern:** Run `git submodule update` without `GIT_TERMINAL_PROMPT=0`.
  - *Why:* Missing credentials on 3rd-party submodule remotes cause CLI processes to block indefinitely in background runners.

---

## Verification

```bash
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_update_submodules_drift_and_dry_run
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_pull_repo_with_submodules_recursive
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_cli_pull_and_status_submodules
```
