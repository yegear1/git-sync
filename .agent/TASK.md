# TASK.md — Current Task and Roadmap

> WHAT to do now. Detailed history lives in `git log` and `ARCHIVE.md`. User requests during conversation
> take precedence — report discrepancies before acting.

---

## Active Task

### 📌 Task [00.28]: CLI Subcommand Modularization
- **Status:** RUNNING
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/ui/`
- **Description:** Split monolithic formatting/rendering helpers out of `src/git_sync/cli.py` into `src/git_sync/ui/formatters.py`, keeping `src/git_sync/cli.py` focused strictly on argument parsing and command dispatch (<1,200 lines).
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/ui/`
- **Acceptance Criteria:**
  - [ ] Monolithic CLI rendering helpers extracted to `src/git_sync/ui/formatters.py`.
  - [ ] `src/git_sync/cli.py` focused on argument parsing and dispatch.
  - [ ] All CLI tests pass with exit code 0.

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

---

## Backlog (Upcoming, in priority order)

> Tag pre-approved tasks having strictly disjoint file scopes with `[APPROVED]` `[PARALLEL: W1]` and `Paths: ...` for concurrent `task-queue` execution.

### 📌 Task [00.29]: Auxiliary Branch Push and Remediation Parity (`AUX_PUSH_PENDING` / `--all-branches` / `--sync-branches`)
- **Status:** APPROVED
- **Paths:** `src/git_sync/engine/git.py`, `src/git_sync/config.py`, `src/git_sync/cli.py`, `tests/`
- **Description:** Fix `AUX_PUSH_PENDING` remediation string to `git-sync push <repo> --sync-branches` (or `--all-branches`) and implement auxiliary branch push capabilities across engine (`push_repo` with `sync_branches`), configuration (`push_sync_branches`), and CLI (`--sync-branches` / `--all-branches` / `--no-sync-branches`). Add smart CLI disambiguation when repository arguments are passed with `--all` and ensure individual branch protection and divergence guards for each auxiliary branch.
- **Systems Involved:** `src/git_sync/engine/git.py`, `src/git_sync/config.py`, `src/git_sync/cli.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] `inspect_repo` sets remediation to `git-sync push {rel_path} --sync-branches` when `aux_branches_ahead` is detected.
  - [ ] `push_repo` accepts `sync_branches: bool = True`, discovering auxiliary tracking branches ahead of upstream and pushing them safely.
  - [ ] Safety guards (protected branch policy, force-push block on protected branches, divergence check) applied to each auxiliary branch independently.
  - [ ] CLI `push` accepts `--sync-branches`, `--all-branches` (alias), and `--no-sync-branches`.
  - [ ] CLI smart disambiguation: passing `git-sync push <repo> --all` treats `--all` as `--all-branches` for the specified repo rather than triggering global discovery across all repos.
  - [ ] Bulk preview and confirmation modes display auxiliary branches pending push.
  - [ ] Human terminal output renders auxiliary branches push results cleanly with status icons.
  - [ ] Unit and integration tests verify auxiliary branch push, dry-run, protected branch defenses, and CLI disambiguation.

### 📌 Task [00.30]: CLI Output and Table English Standardization
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`
- **Description:** Eliminate all hardcoded Portuguese strings in `src/git_sync/cli.py` and standardize human-facing terminal output exclusively in Technical English, aligning CLI tables, status details, doctor diagnostics, SSH multiplexing audits, worktree pruning, and lock recovery with existing JSON enums.
- **Systems Involved:** `src/git_sync/cli.py`
- **Acceptance Criteria:**
  - [ ] Zero hardcoded Portuguese strings remaining in `src/git_sync/cli.py`.
  - [ ] Table headers (`STATUS`, `REPOSITORY`, `BRANCH`, `DETAILS`) and status descriptions (`no remote configured`, `synced`, `N behind`, `N ahead`, `dirty(N files)`) standardized in English.
  - [ ] Doctor, SSH audit, Worktree prune, Unlock, and Config outputs rendered in English.
  - [ ] All JSON output contracts remain unchanged and strictly in English.

### 📌 Task [00.31]: Interactive Dashboard TUI and Progress Indicators English Standardization
- **Status:** APPROVED
- **Paths:** `src/git_sync/ui/dashboard.py`, `src/git_sync/ui/progress.py`
- **Description:** Eliminate hardcoded Portuguese strings in the interactive dashboard TUI (`dashboard.py`) and progress/confirmation helpers (`progress.py`), standardizing all status badges, navigation hints, diff inspection reports, changelog views, and bulk confirmation prompts in Technical English.
- **Systems Involved:** `src/git_sync/ui/dashboard.py`, `src/git_sync/ui/progress.py`
- **Acceptance Criteria:**
  - [ ] Zero hardcoded Portuguese strings in `src/git_sync/ui/dashboard.py` and `src/git_sync/ui/progress.py`.
  - [ ] TUI keybindings bar, status messages, and feedback rendered in English.
  - [ ] Bulk action preview headers and interactive confirmation prompts (`[y/N]`) rendered in English.

### 📌 Task [00.32]: Test Suite English Assertion Alignment and Regression Gate
- **Status:** APPROVED
- **Paths:** `tests/test_git_sync.py`, `tests/test_progress_telemetry_dryrun.py`, `tests/test_ui_dashboard_and_style.py`
- **Description:** Update legacy unit test assertions that check Portuguese string fragments (`sem remote configurado`, `Total: 1 repositórios`, `commit(s) atrás`, `commit(s) à frente`, `Pull concluído`, etc.) to assert the standardized English strings, ensuring hermetic pass across the entire test suite.
- **Systems Involved:** `tests/`
- **Acceptance Criteria:**
  - [ ] All test assertions checking textual output match the new English CLI and TUI output.
  - [ ] `uv run --with pytest --with setuptools pytest` passes with 100% success (312+ tests passing).
  - [ ] `mypy --strict src/git_sync` exits with code 0.

### 📌 Task [00.33]: Concurrent Multi-Repository `git-sync push` Parallelization
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/engine/concurrency.py`, `tests/`
- **Description:** Parallelize multi-repository `git-sync push` execution using `ThreadPoolExecutor` and dynamic worker scaling via `AdaptiveConcurrencyController` (`determine_workers` with `is_fetch=True` to account for remote network latency), eliminating sequential push bottlenecks while maintaining pre-flight safety checks and thread-safe telemetry.
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/engine/concurrency.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] `cmd_push` determines `effective_push_workers` via `controller.determine_workers(len(targets), is_fetch=True, configured_workers=configured_w)` honoring `--workers` and `--auto-tune`.
  - [ ] `benchmark.active_workers` and `benchmark.optimal_workers` accurately recorded during push phase.
  - [ ] Multi-repository pushes execute concurrently via `ThreadPoolExecutor(max_workers=effective_push_workers)`.
  - [ ] `ProgressIndicator` advances dynamically in real-time as each worker future completes via `as_completed`.
  - [ ] Results sorted deterministically by `repo_rel` prior to console rendering or JSON output.
  - [ ] Thread safety preserved across `BulkCache`, `TelemetryBenchmark`, and individual repo `preflight_push_check` runs.
  - [ ] Unit and telemetry benchmark tests verify concurrent push execution, worker tuning, and benchmark metrics recording.

### 📌 Task [00.34]: Token-Budget Optimization and Structured Diffstat Summarization in `--compact` Pull JSON
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/engine/diff.py`, `tests/`
- **Description:** Optimize token consumption in `git-sync pull --compact --json` by summarizing/truncating verbose raw `git pull` stdout, diffstats, and uncommitted file listings to strict concise metrics (<150 tokens per repository) for consumption by AI agents, and provide concise single-line summary reporting in text mode.
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/engine/diff.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] `git-sync pull --compact --json` produces concise structured payloads omitting raw bloated diffs and stdout.
  - [ ] Fast-forward and update outputs summarized into consolidated metrics (e.g., changed file counts, insertions, deletions).
  - [ ] Empty or redundant nested collections (`stale_refs`, `pruned_refs`, `would_prune_refs`, `worktree_isolated`, empty submodules) omitted from compact JSON items.
  - [ ] Strict token budget: each repository entry in compact JSON consumes <150 tokens.
  - [ ] Text mode `git-sync pull --compact` renders concise single-line summary (e.g., `Total: N repos | Updated: X | Up-to-date: Y | Errors: Z`).
  - [ ] Unit tests verify token-budget bounds, compact JSON schema contract, and text-mode summary output.


---

## Release / Cycle Wrap-up (Not the next task)

Release/tag only with explicit human request. When triggered, the ID is `[99.1]`. Do not number feature, hygiene, or CI tasks as `99.x`. Do not calculate next task ID from this section.

---

## Future Backlog / Ideas (Unprioritized)

- Visual TUI mode with curses or standard library terminal primitives.
