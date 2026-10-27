---
id: "k-002"
title: "Worktree In-Tree False Dirty and Active Branch Fetch Refusal"
date: 2026-10-20
tags:
  - "git"
  - "worktrees"
  - "submodules"
  - "concurrency"
severity: "medium"
affected_components:
  - "src/git_sync/core.py"
  - "src/git_sync/cli.py"
symptoms:
  - "fatal: refusing to fetch into branch '<branch>' checked out at '<path>'"
  - "Untracked worktree directory marked as dirty in primary repo"
  - "Secondary worktrees omitted or submodules duplicated during audit"
---

# [k-002] Worktree In-Tree False Dirty and Active Branch Fetch Refusal

## Problem / Symptom

When operating across Git workspaces that contain secondary linked worktrees or submodules:
1. Linked worktrees have `.git` as a plain file pointing to `$GIT_DIR/worktrees/<name>`, unlike standard repositories which have a `.git/` directory. Traversing directories checking only `".git" in dirnames` fails to discover linked worktrees.
2. If a secondary worktree is initialized within the primary repository tree (e.g. `repo/worktrees/feat`), running `git status --porcelain` in the primary repository reports `?? worktrees/` as an untracked file, leading to a false `is_dirty = True` flag.
3. If an auxiliary tracking branch is checked out in an active secondary worktree, attempting an atomic fast-forward fetch from the primary repository via `git fetch . origin/<branch>:<branch>` fails with exit code 128:
```text
fatal: refusing to fetch into branch 'refs/heads/<branch>' checked out at '<path>'
```
4. Git submodules have `.git` as a plain file pointing to `$GIT_DIR/modules/<name>`. Treating all `.git` files as top-level repositories duplicates status audits and pull operations.

---

## Approved Solution

1. **Topology Discrimination:** When discovering repositories in `os.walk`, check both `dirnames` (`.git` directory for primary repos) and `filenames` (`.git` file). Inspect `.git` file content:
   - If `gitdir:` contains `/worktrees/`, treat as a linked worktree.
   - If `gitdir:` contains `/modules/`, treat as a submodule and exclude from top-level discovery to avoid duplicate audits.
2. **False Dirty Suppression:** In `inspect_repo`, run `git worktree list --porcelain`. For any worktree situated within `repo_path`, filter out untracked lines in `git status --porcelain` that match the worktree directory path.
3. **Active Worktree Branch Fetch Exemption:** In `pull_repo` and `inspect_repo`, cross-reference branches checked out across all worktrees. When a tracking branch is checked out in another worktree, do NOT attempt `git fetch . origin/<branch>:<branch>`; instead allow that worktree to pull updates in its own directory.
4. **Remote Inheritance:** If a linked worktree does not return a remote URL directly, fall back to querying the primary worktree path identified via `git worktree list --porcelain`.

---

## What NEVER to Do

- ❌ **Anti-Pattern:** Attempt `git fetch . origin/<branch>:<branch>` indiscriminately on all tracking branches without checking active worktrees.
  - *Why:* Git strictly forbids updating a ref checked out in any linked worktree; doing so aborts the fetch process with exit code 128.
- ❌ **Anti-Pattern:** Treat submodules as independent top-level repositories during `discover_repos`.
  - *Why:* Causes redundant inspections, detached HEAD alerts, and uncoordinated pulls. Submodule status must be inspected via `git submodule status` from the parent repository.

---

## Verification

```bash
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_inspect_worktree_no_false_dirty_nested
python3 -m unittest tests.test_git_sync.TestGitSyncCore.test_pull_repo_skips_active_worktree_branch
```
