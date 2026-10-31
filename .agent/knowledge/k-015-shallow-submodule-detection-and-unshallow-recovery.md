---
id: "k-015"
title: "Shallow Submodule Deepening, Unshallow Fetch Recovery, and Local Depth Protocols"
date: 2026-10-31
tags:
  - "git"
  - "submodules"
  - "shallow"
  - "unshallow"
severity: "medium"
affected_components:
  - "src/git_sync/engine/git.py"
  - "src/git_sync/cli.py"
symptoms:
  - "Git fatal error 'fatal: --unshallow on a complete repository does not make sense' when unshallowing non-shallow repos"
  - "Truncated commit history in shallow submodules causing missing commits or broken submodule references"
  - "Git warning '--depth is ignored in local clones; use file:// instead' during local shallow submodule clones"
---

# [k-015] Shallow Submodule Deepening, Unshallow Fetch Recovery, and Local Depth Protocols

## Problem / Symptom

When working with shallow repository clones and truncated submodule trees (`--depth 1`):
1. **Submodule Depth Truncation:** Submodules cloned shallowly lack intermediate history. When parent repository commits advance or diverge, submodule pointer commits may not exist locally, failing checkouts or status verifications.
2. **Fatal Error on Non-Shallow Unshallow:** If `git fetch --unshallow` is executed on a repository or submodule that is already complete (not shallow), Git exits with code 128:
   `fatal: --unshallow on a complete repository does not make sense`.
3. **Subprocess Overhead:** Running `git rev-parse --is-shallow-repository` across dozens of submodules in large multi-repo workspaces incurs excessive subprocess spawning overhead.
4. **Local Submodule Clone Depth Gotcha:** Git ignores `--depth` on raw filesystem directory paths (`git submodule add --depth 1 /path/to/repo`) with `warning: --depth is ignored in local clones; use file:// instead.`.

---

## Approved Solution

1. **Instant Direct Inode Shallow Inspection:**
   - `# INVARIANT: Directly inspecting git_dir/shallow eliminates subprocess overhead during bulk audits.`
   - In `is_shallow_repository(repo_path)`: inspect `(get_repo_git_dir(repo_path) / "shallow").is_file()`.
   - For linked worktrees, traverse the common gitdir via `(git_dir / "commondir")` to check the parent's `shallow` file.
   - For submodules, `get_repo_git_dir(sub_dir)` resolves to `.git/modules/<path>`, directly locating `.git/modules/<path>/shallow`.
2. **Idempotent Guided Unshallow (`unshallow_submodule`):**
   - `# INVARIANT: If submodule is not shallow, skip --unshallow to prevent Git fatal error: 'fatal: --unshallow on a complete repository does not make sense'.`
   - Before invoking `fetch --unshallow`, verify `is_shallow_repository(sub_dir)`. If false, return cleanly with `was_shallow: False` and `unshallowed: False`.
3. **Resilient Submodule Deepening During Sync:**
   - In `update_submodules(..., unshallow=True)`: unshallow currently shallow submodules before checkout, run `git submodule update`, and re-check for newly initialized shallow submodules.
   - In `pull_repo(..., unshallow=True)`: if the primary repository itself is shallow, fetch `--unshallow` from remote; if submodules are present, pass `unshallow=True` to `update_submodules`.
4. **Agent Status Diagnostics & Triage Remediation:**
   - In `inspect_repo`, return `"is_shallow": bool` and per-submodule `"is_shallow": bool`.
   - In `--json --compact` triage mode, output `"shallow_submodules": [...]` and remediation:
     `{"action_required": "unshallow_submodules", "remediation": "git-sync pull '<rel_path>' --submodules --unshallow"}`.
   - In table view, format shallow submodules with `submodules(N, M shallow)` and `[shallow]`.

---

## What NEVER to Do

- ❌ **Anti-Pattern:** Blindly invoke `git fetch --unshallow` without verifying shallow state via `is_shallow_repository`, causing Git exit code 128 failure on complete repositories.
- ❌ **Anti-Pattern:** Spawn `git rev-parse --is-shallow-repository` in a loop across hundreds of repositories or submodules when on-disk inspection of `git_dir / "shallow"` is exact and instantaneous.
- ❌ **Anti-Pattern:** Use raw local filesystem paths without `file://` when testing or configuring shallow submodule fixtures, triggering `--depth is ignored in local clones`.

---

## Verification

```bash
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_shallow_submodule_detection_and_unshallow_recovery
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_pull_repo_with_unshallow_primary_and_submodules
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_cli_pull_and_status_unshallow
```
