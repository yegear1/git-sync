# TASK.md — Current Task and Roadmap

> WHAT to do now. Detailed history lives in `git log` and `ARCHIVE.md`. User requests during conversation
> take precedence — report discrepancies before acting.

---

## Active Task

### 📌 Task [00.35]: Token-Budget Optimization and Structured Diffstat Summarization in `--compact` Pull & Push JSON
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/engine/diff.py`, `src/git_sync/ui/formatters.py`, `tests/`
- **Description:** Optimize token consumption in `git-sync pull` and `git-sync push` when `--compact` is active (the default) by summarizing verbose raw `git pull`/`git push` `stdout`, diffstats, and uncommitted file listings into concise structured metrics (`files_changed`, `insertions`, `deletions`) and pruning empty/null fields (`stderr: ""`, `stale_refs: []`, `pruned_refs: []`, `would_prune_refs: []`, `worktree_isolated: {}`, empty `submodules` / `aux_branches`) to guarantee <150 tokens per repository entry while preserving full raw output under `--full` (`--no-compact`). Also provide single-line summary reporting in `--human --compact` mode.
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/engine/diff.py`, `src/git_sync/ui/formatters.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] `git-sync pull` and `git-sync push` in `--compact` JSON mode (default) emit concise structured payloads omitting raw bloated diffs/stdout and empty collections (`stale_refs`, `pruned_refs`, `would_prune_refs`, `worktree_isolated`, empty `submodules`, empty `stderr`).
  - [ ] Fast-forward and update outputs are parsed into consolidated diffstat metrics (`files_changed`, `insertions`, `deletions`).
  - [ ] Passing `--full` (`--no-compact`) preserves the full raw `stdout`, `stderr`, and all collection keys.
  - [ ] Strict token budget: each repository entry in compact JSON consumes <150 tokens.
  - [ ] Human text mode with `--human --compact` renders a concise single-line summary (`Total: N repos | Updated: X | Up-to-date: Y | Errors: Z`).
  - [ ] Unit tests verify token-budget bounds, `--compact` vs `--full` JSON schema parity, and `--human --compact` summary output.

---

## Completed Tasks Log

| Task | Title | Commit(s) | Date |
|---|---|---|---|
| [00.0] | Initial clean public baseline of git-sync workspace orchestrator | [baseline] | 2026-10-27 |
| [00.1] | Fine-Grained Partial Worktree Branch Synchronization and Conflict Isolation | [92e882d] | 2026-10-28 |
| [00.2] | Open Source Readiness, MIT Licensing, Sanitization, and Community Governance | [6442a85, 2e0e7bb] | 2026-10-31 |
| [00.3] | Visual TUI and Compact Interactive Dashboard | [08769e6] | 2026-10-31 |
| [00.4] | Hierarchical Config Merging, Source Provenance Tracking, and Interactive Config Bootstrap | [eee95d3] | 2026-10-31 |
| [00.5] | Token-Budget Aware Diff Truncation for Agent and CLI Inspection | [09fd287] | 2026-10-31 |
| [00.6] | Deterministic Conventional Commit Classifier for Pending Upstream & Local Commits | [dfc876a] | 2026-10-31 |
| [00.7] | Native `git sync` Subcommand Autocompletion and SVG Terminal Demo | [d0d64cd] | 2026-10-31 |
| [00.8] | Remote Health Check Telemetry and Latency Grading in Doctor | [9e584da] | 2026-10-31 |
| [00.9] | Automated Git Submodule Drift Detection and Optional Recursive Update | [bcba8cb] | 2026-10-31 |
| [00.10] | Worktree Stale Pruning and Orphaned Worktree Cleanup Command | [6834019] | 2026-10-31 |
| [00.11] | Shallow Submodule Deepening and Unshallow Fetch Recovery | [2d52d52] | 2026-10-31 |
| [00.12] | Sparse-Checkout Cone Pattern Detection and Synchronization Diagnostic | [c7b4f13] | 2026-10-31 |
| [00.13] | Multi-Profile Dynamic Workspace Switching and Profile Scoped Defaults | [71f9962] | 2026-10-31 |
| [00.14] | Direct Push Pre-Flight Telemetry and Protected Branch Force-Push Defense | [64533e6] | 2026-10-31 |
| [00.15] | Dry-Run Interactive Confirmation and Bulk Action Preview Mode | [1fd38ca] | 2026-10-31 |
| [00.16] | SSH ControlMaster Multiplexing Lifecycle and Stale Socket Auto-Pruning | [dfb4969] | 2026-10-31 |
| [00.17] | Fine-Grained Git Credential Helper Triage and Keychain Expiry Diagnostics | [e1e0571] | 2026-10-31 |
| [00.18] | Parallel Batch Worktree Health Audit and Multi-Remote Connectivity Matrix | [3fadd3e] | 2026-11-08 |
| [00.19] | Automated Git LFS Object Tracking and Bandwidth-Aware Fetch Controls | [c7e5dd0] | 2026-11-08 |
| [00.20] | Setuptools PEP 639 License Warning Suppression and Distribution Validator Completeness | [9ef3ab8] | 2026-11-08 |
| [00.21] | CI Pipeline Recursive Bytecode Compilation, Strict Mypy Gate, and Python Version Alignment | [f838659] | 2026-11-08 |
| [00.22] | Public Facade Re-Export Parity (`__all__`) and Dynamic `GIT_ENV` Construction | [ecc14c5] | 2026-11-08 |
| [00.23] | Engine Modularization Phase 1 — Extract SSH Multiplexing & Credential Diagnostics | [2c2a2d5] | 2026-11-08 |
| [00.24] | Engine Modularization Phase 2 — Extract Changelog Classifier & Diff Chunker | [ccbb332] | 2026-11-08 |
| [00.25] | Engine Modularization Phase 3 — Extract Worktree, Submodule & Sparse-Checkout Managers | [84ea732] | 2026-11-08 |
| [00.26] | Test Mock Decoupling and Elimination of Runtime sys.modules Dispatch (_dispatch_fn) | [1aad0a1] | 2026-11-09 |
| [00.27] | Strict Domain Payload Contracts (`TypedDict` / `@dataclass`) and Mypy Strict Compliance | [387c626] | 2026-11-09 |
| [00.28] | CLI Subcommand Modularization into `src/git_sync/ui/formatters.py` | [17d0f9c] | 2026-11-09 |
| [00.29] | Auxiliary Branch Push and Remediation Parity (`AUX_PUSH_PENDING` / `--all-branches` / `--sync-branches`) | [e064366] | 2026-11-09 |
| [00.30] | CLI Output and Table English Standardization | [7df64a5] | 2026-11-09 |
| [00.31] | Interactive Dashboard TUI and Progress Indicators English Standardization | [7e123a9] | 2026-11-09 |
| [00.32] | Test Suite English Assertion Alignment and Regression Gate | [7e123a9] | 2026-11-09 |
| [00.33] | Agent-First CLI Defaults (`--json` & `--compact` by Default) and Configurable Human/Full Overrides | [cdee860] | 2026-11-10 |
| [00.34] | Concurrent Multi-Repository `git-sync push` Parallelization | [2d0431e] | 2026-11-10 |

---

## Backlog (Upcoming, in priority order)

> Tag pre-approved tasks having strictly disjoint file scopes with `[APPROVED]` `[PARALLEL: W1]` and `Paths: ...` for concurrent `task-queue` execution.


### 📌 Task [00.36]: Anomaly-Only Compact Mode for `git-sync doctor` and `git-sync worktree list`
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/ui/formatters.py`, `tests/`
- **Description:** Optimize `git-sync doctor` and `git-sync worktree list` in default `--compact` JSON mode so that healthy repository entries (`Grade A`/`B` reachable remotes in `multi_remote_matrix.matrix` and healthy repositories in `worktree_health.items` or clean main worktrees in `worktree list`) are omitted, surfacing only aggregate health metrics plus actionable anomalies (`unreachable`, `warnings`, `errors`, `suggestions`, `stale_worktrees`, `dirty_worktrees`, `locked_repos`), while `--full` (`--no-compact`) retains the complete per-repository matrices.
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/ui/formatters.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] `git-sync doctor` (default `--compact --json`) omits healthy per-repository entries from `multi_remote_matrix.matrix` and `worktree_health.items`, retaining aggregate counts (`total_repos_audited`, `healthy_endpoints`, `unreachable_endpoints`, `grade_distribution`) and only anomalous repo entries (`has_unreachable=True` or `is_healthy=False`).
  - [ ] `git-sync doctor --full` includes the complete unfiltered `matrix` and `items` arrays.
  - [ ] `git-sync worktree list` (default `--compact --json`) filters to repositories that have linked worktrees, stale/prunable worktrees, or locked worktrees, while `--full` lists every single-worktree repository.
  - [ ] Unit tests verify compact vs full JSON payloads for both `doctor` and `worktree list`.


### 📌 Task [00.37]: Structured `REPO_NOT_FOUND` JSON Diagnostics and Non-Zero Exit Code on Unmatched Repository Targets
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/engine/types.py`, `tests/`
- **Description:** Fix silent failure in `cmd_pull`, `cmd_push`, `cmd_resolve`, `cmd_unlock`, and `cmd_worktree` when an AI agent or user specifies an unmatched repository name in JSON mode. Instead of silently skipping unmatched names and returning `[]` with exit code `0`, append a structured error entry (`{"repo": query, "repo_rel": query, "success": False, "error": "REPO_NOT_FOUND", "diagnostic": {"category": "REPO_NOT_FOUND", "action_required": "verify_repo_name", "message": ..., "suggestion": "git-sync status"}}`) and return exit code `1`.
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/engine/types.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] Passing a non-existent repository name to `pull`, `push`, `resolve`, `unlock`, or `worktree` in `--json` mode includes a structured `REPO_NOT_FOUND` error record in the output payload instead of silently omitting it.
  - [ ] Commands return exit code `1` whenever one or more requested repository names cannot be matched in the workspace.
  - [ ] Human text mode (`--human`) continues to print `❌ No repository found matching: '<query>'` and returns exit code `1`.
  - [ ] Unit tests verify `REPO_NOT_FOUND` JSON error payload and exit code `1` across `pull`, `push`, `resolve`, `unlock`, and `worktree`.


---

## Release / Cycle Wrap-up (Not the next task)

Release/tag only with explicit human request. When triggered, the ID is `[99.1]`. Do not number feature, hygiene, or CI tasks as `99.x`. Do not calculate next task ID from this section.

---

## Future Backlog / Ideas (Unprioritized)

- Visual TUI mode with curses or standard library terminal primitives.
