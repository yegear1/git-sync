# TASK.md — Current Task and Roadmap

> WHAT to do now. Detailed history lives in `git log` and `ARCHIVE.md`. User requests during conversation
> take precedence — report discrepancies before acting.

---

## Active Task

### 📌 Task [00.26]: Test Mock Decoupling and Elimination of Runtime `sys.modules` Dispatch (`_dispatch_fn`)
- **Status:** RUNNING
- **Paths:** `src/git_sync/engine/`, `tests/`
- **Description:** Replace the 32 dynamic `_dispatch_fn` runtime `sys.modules["git_sync.core"]` lookups with a clean, lightweight dependency/runner hook and standardized mock targets across `tests/`, removing runtime reflection overhead and simplifying static analysis.
- **Systems Involved:** `src/git_sync/engine/`, `tests/`
- **Acceptance Criteria:**
  - [ ] `_dispatch_fn` is removed from the engine while all unit tests and mock patches pass cleanly.
  - [ ] Zero runtime `sys.modules` reflection in hot execution paths.
  - [ ] Full packaging symbol parity and unit test suite pass with exit code 0.

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

---

## Backlog (Upcoming, in priority order)

> Tag pre-approved tasks having strictly disjoint file scopes with `[APPROVED]` `[PARALLEL: W1]` and `Paths: ...` for concurrent `task-queue` execution.

### 📌 Task [00.27]: Strict Domain Payload Contracts (`TypedDict` / `@dataclass`) and Mypy Strict Compliance
- **Status:** APPROVED
- **Paths:** `src/git_sync/engine/types.py`, `src/git_sync/engine/`, `src/git_sync/cli.py`, `src/git_sync/ui/`
- **Description:** Define standard library `TypedDict` and `@dataclass` contracts in `src/git_sync/engine/types.py` for repository inspection (`RepoStatusRecord`), doctor diagnostics (`DoctorSanityReport`), and sync results (`PullResult`, `PushResult`, `ResolveResult`), replacing unchecked `Dict[str, Any]` signatures and ensuring 0 errors under `mypy --strict`.
- **Systems Involved:** `src/git_sync/engine/types.py`, `src/git_sync/engine/`, `src/git_sync/cli.py`, `src/git_sync/ui/`
- **Acceptance Criteria:**
  - [ ] Core engine and CLI functions use explicit typed contracts instead of `Dict[str, Any]`.
  - [ ] `mypy --strict src/git_sync` exits with code 0.

### 📌 Task [00.28]: CLI Subcommand Modularization
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/ui/`
- **Description:** Split monolithic formatting/rendering helpers out of `src/git_sync/cli.py` into `src/git_sync/ui/formatters.py`, keeping `src/git_sync/cli.py` focused strictly on argument parsing and command dispatch (<1,200 lines).
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/ui/`
- **Acceptance Criteria:**
  - [ ] Monolithic CLI rendering helpers extracted to `src/git_sync/ui/formatters.py`.
  - [ ] `src/git_sync/cli.py` focused on argument parsing and dispatch.
  - [ ] All CLI tests pass with exit code 0.

### 📌 Task [00.29]: Auxiliary Branch Push and Remediation Parity (`push_aux` / `--all-branches`)
- **Status:** APPROVED
- **Paths:** `src/git_sync/engine/git.py`, `src/git_sync/config.py`, `src/git_sync/cli.py`, `tests/`
- **Description:** Fix `AUX_PUSH_PENDING` remediation string to `git-sync push <repo> --all-branches` and implement auxiliary branch push capabilities across engine (`push_repo` with `push_aux`), configuration (`push_all_branches`), and CLI (`--all-branches` / `--push-aux` flags).
- **Systems Involved:** `src/git_sync/engine/git.py`, `src/git_sync/config.py`, `src/git_sync/cli.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] `inspect_repo` sets remediation to `git-sync push {rel_path} --all-branches` when `aux_branches_ahead` is detected.
  - [ ] `push_repo` accepts `push_aux: bool = False`, discovering auxiliary tracking branches ahead of upstream and pushing them safely.
  - [ ] Safety guards (protected branch policy and divergence check) applied to each auxiliary branch.
  - [ ] CLI `push` accepts `--all-branches` (alias `--push-aux`) and `--no-all-branches`.
  - [ ] Bulk preview and confirmation modes display auxiliary branches pending push.
  - [ ] Unit and integration tests verify auxiliary branch push, dry-run, and safety guards.

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

---

## Release / Cycle Wrap-up (Not the next task)

Release/tag only with explicit human request. When triggered, the ID is `[99.1]`. Do not number feature, hygiene, or CI tasks as `99.x`. Do not calculate next task ID from this section.

---

## Future Backlog / Ideas (Unprioritized)

- Visual TUI mode with curses or standard library terminal primitives.
