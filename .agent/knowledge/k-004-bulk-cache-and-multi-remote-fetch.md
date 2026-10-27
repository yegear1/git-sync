---
id: "k-004"
title: "Bulk Cache Optimizations, Worktree Family Memoization, and Multi-Remote Coordination"
date: 2026-10-21
tags:
  - "git"
  - "performance"
  - "caching"
  - "worktrees"
  - "multi-remote"
severity: "medium"
affected_components:
  - "src/git_sync/core.py"
  - "src/git_sync/cli.py"
symptoms:
  - "Excessive subshell spawns during bulk repository audits"
  - "Redundant git worktree list calls for linked sibling worktrees"
  - "Slow status audits due to git submodule status calls on repos without submodules"
  - "Stale upstream tracking branches when repositories configure multiple remotes beyond origin"
---

# [k-004] Bulk Cache Optimizations, Worktree Family Memoization, and Multi-Remote Coordination

## Problem / Symptom

1. **Submodule Audit Overhead:** Calling `git submodule status` per repository spawns an extra subshell (~20-50ms each) even when 95% of workspace repositories do not define any Git submodules.
2. **Worktree Family Redundancy:** When a repository has linked worktrees (e.g. secondary branches checked out in sibling folders), inspecting each worktree directory independently invokes `git worktree list --porcelain`, multiplying subshell overhead across the same worktree family.
3. **Multi-Remote Fetch Blindspots:** Running standard `git fetch` without arguments defaults only to the primary remote (`origin`), leaving auxiliary remotes (`upstream`, `backup`, `fork`) unrefreshed and tracking branches out of sync.
4. **Auxiliary Remote Errors:** Invoking `git fetch --all` fails with exit code 128 if an auxiliary remote is temporarily unreachable or misconfigured (e.g. an offline mirror), which prematurely halts audits if unhandled.

---

## Approved Solution

1. **Fast Submodule Pre-check:** Verify the existence of `.gitmodules` on disk before invoking `git submodule status`. If absent, return `[]` immediately with zero subshell overhead.
2. **Worktree Family Memoization in `BulkCache`:** When `get_worktree_info` executes `git worktree list --porcelain` for any member of a worktree family, auto-populate the cache for all sibling worktrees discovered in that topology.
3. **Multi-Remote Coordination with Fallback:**
   - Detect all configured remotes via `get_repo_remotes`.
   - If multiple remotes exist, execute `git fetch --all` to refresh all remotes simultaneously.
   - If `git fetch --all` encounters an error, attempt a fallback fetch on the target primary remote (`origin`) so healthy primary branches can still be synchronized while reporting auxiliary warnings.
4. **Targeted Remote Operations:** Allow users and automated agents to specify explicit target remotes via `--remote <name>` across `status`, `pull`, and `push`.
5. **Thread-Safe Scoped Caching:** Guard in-memory cache dictionaries (`_worktrees`, `_remotes`, `_submodules`) with a reentrant or standard mutex (`threading.Lock()`), providing safe concurrent access across thread pool workers with explicit invalidation upon repository mutations (`pull_repo`, `push_repo`).
