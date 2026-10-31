---
id: "k-014"
title: "Worktree Stale Pruning, Dirty Linked Worktree Safeguards, and Orphaned Admin Recovery"
date: 2026-10-31
tags:
  - "git"
  - "worktrees"
  - "prune"
  - "orphaned-metadata"
severity: "medium"
affected_components:
  - "src/git_sync/engine/git.py"
  - "src/git_sync/cli.py"
symptoms:
  - "Deleted worktree directories leaving stale pointers in .git/worktrees/<name>"
  - "Git worktree list --porcelain marking missing worktrees as prunable gitdir file points to non-existent location"
  - "Accidental deletion of active uncommitted work in dirty linked worktrees"
  - "Corrupt admin directories lacking gitdir references skipped by standard git worktree list"
---

# [k-014] Worktree Stale Pruning, Dirty Linked Worktree Safeguards, and Orphaned Admin Recovery

## Problem / Symptom

When managing multi-worktree repository families:
1. Deleting a linked worktree directory manually (e.g. `rm -rf <path>`) leaves dangling administrative metadata in `.git/worktrees/<name>/`. Git marks these as `prunable gitdir file points to non-existent location`.
2. Running `git worktree prune` without `--expire now` defaults to retaining prunable worktree entries older than 3 months, preventing immediate cleanup unless explicitly expired.
3. If an automated tool cleans worktrees blindly, it risks pruning metadata while a secondary linked worktree has uncommitted modifications, severing the link and leaving the developer with detached changes or lost work.
4. If an administrative directory in `.git/worktrees/<name>` is corrupted (e.g. missing `gitdir` file or empty directory), standard `git worktree list --porcelain` does not report it, but Git internal commands may encounter stale directory clutter.

---

## Approved Solution

1. **Explicit Expiration in Prune Commands:**
   - Execute `git worktree prune -v --expire now` (supporting `--dry-run` via `-n` / `--dry-run`).
   - Parse pruned items from combined stderr and stdout (`Removing worktrees/<name>: <reason>`).
2. **Dirty Linked Worktree Warning Invariant:**
   - `# INVARIANT: Preserves active dirty linked worktrees and warns without deleting working files.`
   - Before pruning, inspect all linked worktrees in the family. For each worktree that exists on disk and is not the main worktree, execute `check_repo_dirty`.
   - If dirty files are detected, append structured warning entries and include them in `warnings` and `dirty_worktrees` outputs without terminating the pruning of truly stale entries.
3. **Orphaned Admin Directory Recovery:**
   - In `get_worktree_info` and `prune_repo_worktrees`, audit `get_repo_git_dir(primary_wt) / "worktrees"`.
   - Detect subdirectories that either lack a `gitdir` file or point to a target path that does not exist on disk.
   - In live mode, remove corrupt orphaned admin directories safely via `shutil.rmtree` after Git's native prune runs. In dry-run mode, report them without modification.
4. **Agent Status Diagnostics & Triage Remediation:**
   - Expose `stale_worktrees` in `inspect_repo` and status CLI outputs (`worktrees_stale(N)`).
   - In `--json --compact` triage mode, output `"stale_worktrees": [...]` and include remediation:
     `{"action_required": "prune_worktrees", "remediation": "git-sync worktree prune '<rel_path>'"}`.

---

## What NEVER to Do

- ❌ **Anti-Pattern:** Run `git worktree prune` without `--expire now` when immediate cleanup is requested, as Git will preserve missing worktrees for up to 90 days.
- ❌ **Anti-Pattern:** Attempt to prune without checking for uncommitted files in linked worktrees.
- ❌ **Anti-Pattern:** Pass `--force` to `git worktree prune` (Git's native `worktree prune` does not accept a `--force` flag).

---

## Verification

```bash
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_prune_repo_worktrees_cleans_missing_worktree_metadata
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_prune_repo_worktrees_warns_on_dirty_linked_worktree
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_cli_worktree_commands_and_status_diagnostics
```
