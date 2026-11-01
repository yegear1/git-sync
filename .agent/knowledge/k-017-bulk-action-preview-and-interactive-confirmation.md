---
id: "k-017"
title: "Bulk Action Preview Mode and Late-Bound Interactive Confirmation Input"
date: 2026-10-31
tags:
  - "cli"
  - "interactive"
  - "preview"
  - "dry-run"
severity: "medium"
affected_components:
  - "src/git_sync/ui/progress.py"
  - "src/git_sync/cli.py"
  - "tests/test_progress_telemetry_dryrun.py"
symptoms:
  - "Interactive confirmation prompts hang indefinitely during automated unit tests when input_func binds to default parameter input at definition time."
  - "Non-interactive batch operations mutate multiple repositories without safety previews or opportunity to abort."
---

# [k-017] Bulk Action Preview Mode and Late-Bound Interactive Confirmation Input

## Problem / Symptom
When defining interactive confirmation helpers with parameter defaults like `def prompt_bulk_confirmation(..., input_func: Any = input)`, the Python runtime evaluates and binds the default value at function definition time (module import). Later mocking via `unittest.mock.patch("builtins.input", ...)` in test harnesses fails to replace the bound parameter default, causing tests to wait on actual terminal stdin (`sys.stdin.readline()`), which hangs indefinitely in non-interactive CI/agent runs.

Additionally, bulk mutating commands (`pull --all`, `push --all`, `worktree prune`) require safety summaries to preview dirty working trees (which will be autostashed), protected branches, and commit counts before executing multi-repo mutations.

---

## Approved Solution
1. **Late-Bound Prompt Resolution:**
   Default `input_func: Optional[Any] = None` in `prompt_bulk_confirmation` and resolve dynamically at call time:
   ```python
   prompt_fn = input_func if input_func is not None else input
   ans = prompt_fn(prompt_text).strip().lower()
   ```
2. **Structured BulkActionItem & Safety Summary:**
   Encapsulate planned operations in `BulkActionItem` and format visual preview tables (`format_bulk_action_preview`) displaying affected branches, commit divergence, dirty tree warnings, and protected branch indicators.
3. **Safety Invariants:**
   - On denial (`n`, `no`, `nao`, EOF, SIGINT), return `False` and abort cleanly without touching any repository.
   - Accept affirmative responses (`y`, `yes`, `s`, `sim`) or `-y`/`--yes` bypass.
   - In `--json` mode without `--yes`, reject interactive prompt execution with exit code 1 to prevent hanging headless scripts.

---

## What NEVER to Do
- ❌ **Anti-Pattern:** Using early-bound function defaults `input_func = input` in CLI utilities intended to be unit-tested.
  - *Why:* Prevents standard `patch("builtins.input")` monkeypatching from intercepting input calls, causing tests or headless jobs to block on stdin.
- ❌ **Anti-Pattern:** Executing multi-repo batch mutations without structured preview options in destructive or state-changing operations.
  - *Why:* Users cannot audit which repositories have uncommitted changes or target protected branches before mutations occur.

---

## Verification
```bash
python3 -m unittest tests/test_progress_telemetry_dryrun.py
python3 -m unittest discover -s tests
```
