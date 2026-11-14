# Project Lifecycle Hooks Specification

Project-level lifecycle hooks executed by autonomous agents, subagent runners, and orchestrators (e.g., T3 Code, `task-queue`, `bootstrap-worktree.sh`, and `verify-microcontract.sh`).

---

## 1. Canonical Hook Registry

Hooks reside in `.agent/hooks/<hook-name>` and MUST be executable shell scripts (`chmod +x`).

| Hook | Invocation Phase | Purpose & Typical Use Cases |
|---|---|---|
| **`post-worktree-create`** | Post-Provisioning | Executed immediately after a worktree is created, dependencies fast-linked, and `.env` copied. Use for setting up test databases, seeding fixtures, binding ephemeral ports, or generating local configs. |
| **`pre-verify`** | Pre-Verification | Executed immediately before the micro-contract `Verify:` command runs in an isolated worktree. Use for clearing stale test caches, resetting local test fixtures, or auditing pre-conditions. |
| **`post-worktree-teardown`** | Pre-Removal | Executed immediately before worktree removal (`git worktree remove`). Use for gracefully shutting down ephemeral background services or cleaning non-git temporary resources. |

---

## 2. Invariants & Execution Contract

1. **Environment Variables:**
   - `ROOT_DIR`: Absolute path to the primary repository root.
   - `WORKTREE_DIR`: Absolute path to the active isolated worktree.
   - `HOOK_NAME`: Name of the active hook being executed.
2. **Blast Radius & Confinement:**
   - Hooks MUST NOT mutate files in `ROOT_DIR`.
   - Hooks MUST operate strictly within `WORKTREE_DIR` or ephemeral `/tmp`.
3. **Deterministic Fail-Safe:**
   - Any non-zero exit code halts execution immediately (`set -euo pipefail`).
   - Hooks must complete quickly (target < 2s).
4. **Graceful Fallback:**
   - If a hook script does not exist, runners skip execution cleanly without error.
