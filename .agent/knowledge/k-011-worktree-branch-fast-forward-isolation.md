---
id: "k-011"
title: "Secondary Worktree Active Branch Collision and Fast-Forward Isolation"
date: 2026-10-28
tags:
  - "git"
  - "worktrees"
  - "concurrency"
  - "fast-forward"
severity: "medium"
affected_components:
  - "src/git_sync/engine/git.py"
  - "src/git_sync/cli.py"
  - "tests/test_git_sync.py"
symptoms:
  - "fatal: refusing to fetch into checked out branch '<branch>' of worktree '<path>'"
  - "Git exit code 128 during auxiliary branch fast-forward in multi-worktree families"
---

# [k-011] Secondary Worktree Active Branch Collision and Fast-Forward Isolation

## Problem / Symptom

When a Git repository family has linked worktrees (e.g. `main_repo` checking out `main` and linked worktree `wt_feature` checking out `topic-a`):
1. In bulk pull operations with `--sync-branches`, updating auxiliary tracking branches via `git fetch . origin/<branch>:<branch>` fails with exit code 128 if `<branch>` is checked out in any secondary worktree:
   ```text
   fatal: refusing to fetch into checked out branch 'refs/heads/topic-a' of worktree '/path/to/wt_feature'
   ```
2. Without explicit isolation metadata, callers cannot distinguish whether an auxiliary branch failed due to a divergence error or was safely bypassed because it is active in another worktree target.
3. Progress indicators in bulk runs lacked per-target worktree distinction, obscuring which worktrees were being updated.

---

## Approved Solution

1. **Pre-fetch Worktree Branch Inspection:**
   Query repository worktree topology via `get_worktree_info` / `BulkCache` to retrieve `checked_out_branches` mapping branch names to absolute worktree directories.

2. **Branch Collision Isolation:**
   When fast-forwarding auxiliary branches in `pull_repo` (both live and `--dry-run`), check if the target branch is checked out in another worktree (`checked_out_branches[b] != repo_abs`). If active:
   - Skip direct `fetch . origin/b:b` on this repository.
   - Record in `worktree_isolated[b] = checked_out_branches[b]`.
   - Prevent treating the isolation as a failure or pull error.

3. **Per-Target Worktree Progress and Status Reporting:**
   - In `ProgressIndicator`, append `[worktree]` label to secondary worktree targets (`prog_label = f"{rel} [worktree]"`).
   - In CLI summary output, annotate isolated auxiliary branches:
     `Branch auxiliar '<branch>': isolada (ativa em worktree alvo) [<rel_path>]`.
   - Tag worktree pull/push outputs with `[worktree]`.
   - Expose `worktree_isolated`, `is_worktree`, and `worktree_main_path` in structured JSON outputs.
