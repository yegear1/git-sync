# Knowledge Card: k-016-protected-branch-defense-and-push-preflight

- **Title:** Direct Push Pre-Flight Telemetry and Protected Branch Force-Push Defense
- **Domain:** Git / Safety Safeguards / Remote Branch Protection
- **Date:** 2026-10-31
- **Applies to:** `git-sync` (`src/git_sync/engine/git.py`, `src/git_sync/cli.py`)

---

## 1. Problem Description

Batch Git push across multiple repositories carries significant catastrophic risk:
1. An accidental force-push (`git push --force`) could permanently overwrite upstream commits on trunk branches (`main`, `master`, `release/*`, `production`), causing unrecoverable data loss in developer workspaces.
2. Direct pushes to protected branches in strict pull request/trunk-based environments need a deterministic policy block before network execution.
3. Blindly executing `git push` without proactive local pre-flight checks risks encountering network roundtrip latency or unhandled git push rejections (e.g. divergence, detached HEAD, missing remotes, uncommitted working tree changes).

---

## 2. Solution & Invariants

1. **Unconditional Force-Push Defense:** Force pushing (`force=True` or `--force`) to any branch matching protected patterns (`DEFAULT_PROTECTED_BRANCHES = ["main", "master", "release/*", "production", "prod"]` or custom patterns) is strictly prohibited at the client pre-flight stage:
   ```python
   if is_protected and force:
       return {
           "passed": False,
           "diagnostic": {
               "category": "PROTECTED_BRANCH_FORCE_PUSH",
               "message": f"Force-push to protected branch '{target_branch}' is strictly prohibited to prevent accidental remote override.",
           }
       }
   ```
2. **Safe Feature Force-Pushing:** When force push is authorized on non-protected feature branches, `git-sync` enforces `--force-with-lease` rather than raw `--force` to guarantee that another developer's unseen remote commits are never clobbered.
3. **Configurable Direct Push Safeguard:** When `block_protected=True` is enabled in configuration or CLI (`--block-protected`), direct pushes to protected branches are halted with `PROTECTED_BRANCH_DIRECT_PUSH` diagnostics unless explicitly overridden by `--allow-protected`.
4. **Pre-Flight Telemetry & Divergence Guard:** Before invoking `git push`, `preflight_push_check` measures latency, checks detached HEAD status, verifies remote configuration, and evaluates divergence:
   - Non-force pushes to diverged branches are blocked before network execution.
   - Force pushes to non-protected branches bypass divergence blocks as intended for amended feature branches.
