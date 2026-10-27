---
id: "k-003"
title: "Git Stash Automation, Index Preservation, and Unstash Conflict Safeguards"
date: 2026-10-21
tags:
  - "git"
  - "autostash"
  - "dirty-tree"
  - "concurrency"
severity: "high"
affected_components:
  - "src/git_sync/core.py"
  - "src/git_sync/cli.py"
symptoms:
  - "error: Your local changes to the following files would be overwritten by merge"
  - "error: conflicts in index. Try without --index"
  - "Loss of staged vs unstaged separation after autostash restore"
  - "Silent loss of untracked files if stashed without -u"
---

# [k-003] Git Stash Automation, Index Preservation, and Unstash Conflict Safeguards

## Problem / Symptom

When executing bulk or automated pulls across repositories with uncommitted developer work:
1. `git pull --ff-only` refuses to merge when local working tree changes overlap with incoming remote commits, throwing:
```text
error: Your local changes to the following files would be overwritten by merge: ...
Please commit your changes or stash them before you merge.
```
2. Running standard `git stash` without `-u` / `--include-untracked` leaves untracked files behind, which can collide with incoming files or be orphaned.
3. Restoring stashes with standard `git stash pop` merges staged changes into the working directory unstaged, losing the distinction between staged and unstaged work.
4. Using `git stash pop --index` can fail with `error: conflicts in index. Try without --index` even when the working directory files could be cleanly applied without merge conflict.
5. If `git stash pop` encounters a genuine merge conflict with pulled updates, Git keeps the stash entry in `stash@{0}`. If automated tooling assumes pop failure implies data loss or attempts destructive resets, developer work in progress is permanently destroyed.

---

## Approved Solution

1. **Scoped Autostash:** Before invoking `pull`, evaluate working tree cleanliness via `check_repo_dirty`. If dirty and autostash is enabled, run `git stash push -u -m "<tag>"`. This safely preserves staged, unstaged, and untracked changes in an isolated stash entry.
2. **Two-Tiered Unstash with Index Fallback:**
   - First attempt `git stash pop --index` to preserve both staged and unstaged state.
   - If `pop --index` fails without file merge conflicts (e.g. index patch rejected), immediately retry with standard `git stash pop`.
3. **Unstash Conflict Isolation Without Data Loss:**
   - If unstash fails due to genuine merge conflicts (detected via porcelain unmerged status codes like `UU`, `AA`, etc.), Git preserves the stash in `stash@{0}`.
   - The operation reports failure (`success = False`), flags `conflict = True`, lists conflicting files, and provides the preserved `stash_ref` so the developer or agent can resolve conflicts without losing stashed work.
4. **Pull Failure Recovery:** If the pull operation itself fails (e.g. network error, non-ff divergence), the stashed changes MUST be immediately restored onto the original HEAD so the repository is never left in an unexpectedly clean or wiped state.

---

## What NEVER to Do

- ❌ **Anti-Pattern:** Run `git stash` without `-u` during automated sync workflows.
  - *Why:* Untracked developer files will not be preserved and may be overwritten or cause pull collisions.
- ❌ **Anti-Pattern:** Drop or clear stashes (`git stash drop`, `git stash clear`) after a failed pop.
  - *Why:* Git preserves the unmerged stash in the stash list upon conflict; dropping it causes irreversible data loss.
- ❌ **Anti-Pattern:** Leave stashed changes unrestored when `git pull` fails.
  - *Why:* The developer's working directory changes disappear into the stash list without notice.
