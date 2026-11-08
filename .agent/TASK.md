# TASK.md — Current Task and Roadmap

> WHAT to do now. Detailed history lives in `git log` and `ARCHIVE.md`. User requests during conversation
> take precedence — report discrepancies before acting.

---

## Active Task

### 📌 Task [00.19]: Automated Git LFS Object Tracking and Bandwidth-Aware Fetch Controls
- **Status:** APPROVED
- **Paths:** `src/git_sync/engine/git.py`, `src/git_sync/cli.py`, `tests/test_git_sync.py`
- **Description:** Detect Git LFS pointer files and `.gitattributes` filters during repository inspection, surface LFS status in `status` and `doctor`, and provide `--lfs` / `--no-lfs` controls during `pull`.
- **Systems Involved:** `src/git_sync/engine/git.py`, `src/git_sync/cli.py`, `tests/test_git_sync.py`
- **Acceptance Criteria:**
  - [ ] Repositories using Git LFS are identified with pointer/object hygiene and optional fetch controls.
  - [ ] Canonical test suite passes with exit code 0.

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
| [00.20] | Setuptools PEP 639 License Warning Suppression and Distribution Validator Completeness | [9ef3ab8] | 2026-11-08 |
| [00.21] | CI Pipeline Recursive Bytecode Compilation, Strict Mypy Gate, and Python Version Alignment | [f838659] | 2026-11-08 |
| [00.22] | Public Facade Re-Export Parity (`__all__`) and Dynamic `GIT_ENV` Construction | [ecc14c5] | 2026-11-08 |

---

## Backlog (Upcoming, in priority order)

> Tag pre-approved tasks having strictly disjoint file scopes with `[APPROVED]` `[PARALLEL: W1]` and `Paths: ...` for concurrent `task-queue` execution.

### 📌 Task [00.23]: Engine Modularization Phase 1 — Extract SSH Multiplexing & Credential Diagnostics (`engine/ssh.py`, `engine/credentials.py`)
- **Status:** APPROVED
- **Paths:** `src/git_sync/engine/ssh.py`, `src/git_sync/engine/credentials.py`, `src/git_sync/engine/git.py`, `src/git_sync/engine/__init__.py`, `src/git_sync/core.py`
- **Description:** Extract SSH ControlMaster auditing, socket lifecycle pruning, and handshake benchmarking into `src/git_sync/engine/ssh.py`, and extract credential helper chain inspection, `gh` CLI auth diagnostics, token environment checks, and remote reachability probing into `src/git_sync/engine/credentials.py`, preserving full re-export compatibility in `engine/git.py`, `engine/__init__.py`, and `core.py`.
- **Systems Involved:** `src/git_sync/engine/ssh.py`, `src/git_sync/engine/credentials.py`, `src/git_sync/engine/git.py`, `src/git_sync/engine/__init__.py`, `src/git_sync/core.py`
- **Acceptance Criteria:**
  - [ ] SSH and credential functions reside in dedicated cohesive modules with zero circular imports.
  - [ ] All existing imports from `git_sync.engine` and `git_sync.core` remain 100% backward-compatible.

### 📌 Task [00.24]: Engine Modularization Phase 2 — Extract Changelog Classifier & Diff Chunker (`engine/changelog.py`, `engine/diff.py`)
- **Status:** APPROVED
- **Paths:** `src/git_sync/engine/changelog.py`, `src/git_sync/engine/diff.py`, `src/git_sync/engine/git.py`, `src/git_sync/engine/__init__.py`, `src/git_sync/core.py`
- **Description:** Extract `ClassifiedCommit`, Conventional Commit parsing, and Keep-a-Changelog summarization into `src/git_sync/engine/changelog.py`, and extract token-budget aware diff splitting/truncation (`split_diff_files`, `truncate_diff`, `get_divergence_diff`) into `src/git_sync/engine/diff.py`.
- **Systems Involved:** `src/git_sync/engine/changelog.py`, `src/git_sync/engine/diff.py`, `src/git_sync/engine/git.py`, `src/git_sync/engine/__init__.py`, `src/git_sync/core.py`
- **Acceptance Criteria:**
  - [ ] Changelog and diff chunking logic are isolated into standalone testable modules.
  - [ ] Full unit test suite passes with zero regressions.

### 📌 Task [00.25]: Engine Modularization Phase 3 — Extract Worktree, Submodule & Sparse-Checkout Managers (`engine/topology.py`)
- **Status:** APPROVED
- **Paths:** `src/git_sync/engine/topology.py`, `src/git_sync/engine/git.py`, `src/git_sync/engine/__init__.py`, `src/git_sync/core.py`
- **Description:** Extract linked worktree inspection/pruning, submodule status/update/unshallow operations, sparse-checkout cone diagnostics, and stale lock recovery into `src/git_sync/engine/topology.py`, reducing `src/git_sync/engine/git.py` to core runner, discovery, status inspection, and pull/push/resolve orchestration.
- **Systems Involved:** `src/git_sync/engine/topology.py`, `src/git_sync/engine/git.py`, `src/git_sync/engine/__init__.py`, `src/git_sync/core.py`
- **Acceptance Criteria:**
  - [ ] `src/git_sync/engine/git.py` line count is reduced by >45% with clear domain boundaries.
  - [ ] Full unit test suite passes with zero regressions.

### 📌 Task [00.26]: Test Mock Decoupling and Elimination of Runtime `sys.modules` Dispatch (`_dispatch_fn`)
- **Status:** APPROVED
- **Paths:** `src/git_sync/engine/`, `tests/`
- **Description:** Replace the 32 dynamic `_dispatch_fn` runtime `sys.modules["git_sync.core"]` lookups with a clean, lightweight dependency/runner hook and standardized mock targets across `tests/`, removing runtime reflection overhead and simplifying static analysis.
- **Systems Involved:** `src/git_sync/engine/`, `tests/`
- **Acceptance Criteria:**
  - [ ] `_dispatch_fn` is removed from the engine while all unit tests and mock patches pass cleanly.
  - [ ] Zero runtime `sys.modules` reflection in hot execution paths.

### 📌 Task [00.27]: Strict Domain Payload Contracts (`TypedDict` / `@dataclass`) and Mypy Strict Compliance
- **Status:** APPROVED
- **Paths:** `src/git_sync/engine/types.py`, `src/git_sync/engine/`, `src/git_sync/cli.py`, `src/git_sync/ui/`
- **Description:** Define standard library `TypedDict` and `@dataclass` contracts in `src/git_sync/engine/types.py` for repository inspection (`RepoStatusRecord`), doctor diagnostics (`DoctorSanityReport`), and sync results (`PullResult`, `PushResult`, `ResolveResult`), replacing unchecked `Dict[str, Any]` signatures and ensuring 0 errors under `mypy --strict`.
- **Systems Involved:** `src/git_sync/engine/types.py`, `src/git_sync/engine/`, `src/git_sync/cli.py`, `src/git_sync/ui/`
- **Acceptance Criteria:**
  - [ ] Core engine and CLI functions use explicit typed contracts instead of `Dict[str, Any]`.
  - [ ] `mypy --strict src/git_sync` exits with code 0.

### 📌 Task [00.28]: CLI Subcommand Modularization and Output Language Standardization
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/ui/`, `tests/`
- **Description:** Split monolithic formatting/rendering helpers out of `src/git_sync/cli.py` into `src/git_sync/ui/formatters.py` and standardize CLI human-facing terminal output language (with consistent Technical English defaults and optional locale/PT-BR support) while preserving all existing test assertions and JSON contracts.
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/ui/`, `tests/`
- **Acceptance Criteria:**
  - [ ] `src/git_sync/cli.py` is focused on argument parsing and command dispatch (<1,200 lines).
  - [ ] All CLI and UI unit tests pass with exit code 0.

---

## Release / Cycle Wrap-up (Not the next task)

Release/tag only with explicit human request. When triggered, the ID is `[99.1]`. Do not number feature, hygiene, or CI tasks as `99.x`. Do not calculate next task ID from this section.

---

## Future Backlog / Ideas (Unprioritized)

- Visual TUI mode with curses or standard library terminal primitives.
