---
id: "k-008"
title: "Unconfigured Repository Inspection, Branch Preservation, and Error Triage Semantics"
date: 2026-10-21
tags:
  - "git"
  - "no-remote"
  - "branch-preservation"
  - "dirty-status"
  - "json-contract"
  - "cli-formatting"
severity: "medium"
affected_components:
  - "src/git_sync/core.py"
  - "src/git_sync/cli.py"
  - "tests/test_git_sync.py"
symptoms:
  - "Repositories without configured remotes exhibiting blank BRANCH column in CLI status"
  - "Local uncommitted modifications (dirty status) going undetected in repositories lacking remotes"
  - "Visual oxymoron in CLI status table where NO_REMOTE or NO_UPSTREAM showed 'sincronizado' in details"
  - "JSON summaries grouping unconfigured/local-only repos into 'errors', causing false positive monitoring alerts"
---

# [k-008] Unconfigured Repository Inspection, Branch Preservation, and Error Triage Semantics

## Problem / Symptom

1. **Incomplete Local Inspection on Premature Remote Exit:**
   In `src/git_sync/core.py`, when a repository had no remote (`not remotes`), `inspect_repo` returned immediately before querying the current branch (`git branch --show-current`) and before checking working tree status (`check_repo_dirty`).
   - Consequently, the `BRANCH` column remained empty (`""`).
   - More critically, local uncommitted or untracked changes were completely ignored, creating a silent data loss risk where developers or agents believed a workspace had no dirty changes.

2. **Visual Oxymoron in CLI Status Table:**
   When `details` was empty (no ahead, behind, or dirty files), the default fallback appended `"sincronizado"`. This caused `❌ SEM REMOTE ... sincronizado` to be printed for offline, unlinked, or test repositories.

3. **Semantic Ambiguity in JSON Summaries (`errors` vs `unconfigured`):**
   The top-level summary JSON lumped `NO_REMOTE` repos into `"errors": [...]` alongside genuine fetch failures (`FETCH_ERROR`). Because local scratch repositories and offline projects are standard Git workflows rather than execution defects, this triggered false alarms in monitoring tools and agent scripts checking `summary["errors"]`.

---

## Approved Solution

1. **Pre-Remote Local Status Evaluation:**
   In `inspect_repo` (`src/git_sync/core.py`), evaluate current branch and working tree dirty status **before** validating remotes:
   ```python
   # INVARIANT: Inspect local branch and working tree dirty status before remote checks to ensure complete local visibility even for unconfigured/NO_REMOTE repositories.
   p_branch = run_git(repo_path, ["branch", "--show-current"], timeout=5)
   info["branch"] = p_branch.stdout.strip() or "HEAD (detached)"

   is_dirty, dirty_files = check_repo_dirty(repo_path, worktrees=worktrees, timeout=5, cache=cache)
   if is_dirty:
       info["is_dirty"] = True
       info["dirty_files"] = dirty_files
   ```
   This guarantees that even when `NO_REMOTE` is returned, `branch` and `is_dirty` are fully populated.

2. **Accurate Table Status Formatting:**
   In `src/git_sync/cli.py`, replace the naive fallback with status-aware details:
   - For `NO_REMOTE`: append `"sem remote configurado"`.
   - For `NO_UPSTREAM`: append `"sem upstream configurado"`.
   - For `FETCH_ERROR`: append `"erro no fetch"` if no error message was captured.
   - For `UP_TO_DATE`: append `"sincronizado"` only when the status is legitimately synchronized.

3. **Segregated JSON Summary Keys:**
   Segregate configuration state from runtime execution errors in `summary`:
   ```python
   "no_remote": [r["rel_path"] for r in results if r["status"] == "NO_REMOTE"],
   "errors": [r["rel_path"] for r in results if r["status"] == "FETCH_ERROR"],
   ```
   This preserves the integrity of `"errors"` for true network, auth, or lock failures.

---

## Invariants & Rules

- **NEVER** return from repository inspection before checking local branch and dirty status.
- **NEVER** label unconfigured or remote-less repositories as `"sincronizado"`.
- **RESERVE** `"errors"` in JSON summaries strictly for operational execution failures (`FETCH_ERROR`, timeouts, authentication rejections).
