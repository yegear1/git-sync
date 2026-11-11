# TASK.md — Current Task and Roadmap

> WHAT to do now. Detailed history lives in `git log` and `ARCHIVE.md`. User requests during conversation
> take precedence — report discrepancies before acting.

---

## Active Task

### 📌 Task [00.40]: Concurrent Multi-Repository `git-sync pull` Execution with Adaptive ThreadPool
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `tests/`
- **Description:** Parallelize multi-repository pull execution in `cmd_pull` using `concurrent.futures.ThreadPoolExecutor` and `AdaptiveConcurrencyController` when running non-interactively (default `--json` mode or when interactive TTY divergence resolution is not active), matching the concurrent architecture of `cmd_push` (`[00.34]`). Preserve deterministic sorting by `repo_rel`, thread-safe progress updates, and sequential fallback only when `-i`/`--interactive` is active on a TTY.
- **Systems Involved:** `src/git_sync/cli.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] Non-interactive `cmd_pull` executes `pull_repo` across target repositories concurrently via `ThreadPoolExecutor(max_workers=effective_workers)`.
  - [ ] `pull_results` are deterministically sorted by `repo_rel` regardless of worker completion order.
  - [ ] Unexpected worker exceptions are caught and converted into structured `PULL_EXCEPTION` failure records without crashing the batch.
  - [ ] Interactive TTY mode (`-i`/`--interactive` with `not args.json and sys.stdin.isatty()`) retains safe sequential execution for interactive resolution prompts.
  - [ ] Unit tests verify concurrent pull execution, deterministic output ordering, and exception isolation.

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
| [00.35] | Token-Budget Optimization and Structured Diffstat Summarization in `--compact` Pull & Push JSON | [75f70a4] | 2026-11-10 |
| [00.36] | Anomaly-Only Compact Mode for `git-sync doctor` and `git-sync worktree list` | [63abe2a] | 2026-11-10 |
| [00.37] | Structured `REPO_NOT_FOUND` Diagnostics and Failure Exit Codes for Unmatched Repository Targets | [8ff913d] | 2026-11-10 |
| [00.38] | Fix `AUX_PUSH_PENDING` Visibility in Status Summary & Compact Anomaly Key Pruning | [f49306d] | 2026-11-10 |
| [00.39] | Positional Repository Target Filtering (`[repos ...]`) for `git-sync status` and `git-sync dirty` | [f11b1c4] | 2026-11-10 |

---

## Backlog (Upcoming, in priority order)

> Tag pre-approved tasks having strictly disjoint file scopes with `[APPROVED]` `[PARALLEL: W1]` and `Paths: ...` for concurrent `task-queue` execution.


### 📌 Task [00.41]: Token-Budget Compact Formatter (`compact_resolve_item`) for `git-sync resolve`
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/ui/formatters.py`, `src/git_sync/ui/__init__.py`, `tests/`
- **Description:** Implement `compact_resolve_item` in `src/git_sync/ui/formatters.py` and apply it in `cmd_resolve` when `--compact` is active (default) so `git-sync resolve` emits token-minimized JSON payloads (<150 tokens per repository) omitting raw `stdout`/`stderr` and verbose commit arrays in `details` (`local_commits`, `remote_commits`, `dirty_files`) while surfacing concise divergence metrics (`branch`, `upstream`, `ahead`, `behind`, `is_dirty`, `dirty_files_count`, `strategy`, `success`, `diagnostic`). Preserve the full unmodified payload under `--full` (`--no-compact`) and support concise single-line summary output in `--human --compact` mode.
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/ui/formatters.py`, `src/git_sync/ui/__init__.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] `git-sync resolve` in default `--compact --json` mode transforms each result via `compact_resolve_item` (<150 tokens/repo), omitting raw `stdout` and verbose commit/file lists while keeping `ahead`, `behind`, `is_dirty`, `dirty_files_count`, `strategy`, `success`, and `diagnostic`.
  - [ ] Passing `--full` (`--no-compact`) preserves the complete raw `ResolveResult` and `details` payload.
  - [ ] Human compact mode (`--human --compact`) prints a single-line summary (`Total: N repos | Updated: X | Up-to-date: Y | Errors: Z`).
  - [ ] Unit tests verify compact vs. full `resolve` JSON schemas, token budget (<150 tokens/entry), and `--human --compact` output.


### 📌 Task [00.42]: Structured JSON Configuration & Profile Error Diagnostics
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `tests/`
- **Description:** Update pre-parse configuration error handling in `main()` so that `FileNotFoundError`, `ValueError`, and `OSError` raised by `load_config()` emit a structured JSON error payload (`{"ok": False, "error": "CONFIG_ERROR", "message": ..., "diagnostic": {"category": "CONFIG_ERROR", "action_required": "fix_configuration", "message": ..., "suggestion": "git-sync config --init"}}`) when JSON output is active by default (i.e., when `--human`, `--no-json`, `--text`, `--table`, `--dashboard`, `--tui`, or `-i` are not present in `argv`), while preserving human error output on `stderr` when human flags are passed.
- **Systems Involved:** `src/git_sync/cli.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] Invalid `--config` path, malformed JSON config, or unknown `--profile` emits a valid JSON error object with `error: "CONFIG_ERROR"` and structured `diagnostic` by default and exits with code `1`.
  - [ ] Passing `--human` (or `--no-json`, `--text`, `--table`) prints `❌ Configuration error: ...` to `stderr` and exits with code `1`.
  - [ ] Unit tests verify both JSON and human error output for configuration and profile errors.


### 📌 Task [00.43]: English Standardization of JSON Preview Warnings and Removal of `legacy_preview_item` PT-BR Translation
- **Status:** APPROVED
- **Paths:** `src/git_sync/cli.py`, `src/git_sync/ui/formatters.py`, `tests/`
- **Description:** Remove the legacy Portuguese regex translation in `legacy_preview_item` so `--preview` and `--confirm` JSON payloads across `pull`, `push`, and `resolve` emit canonical English warning strings (`"Dirty working tree..."`, `"Protected branch: ..."`, `"Shallow repository"`, etc.), completing the English standardization started in `[00.30]`–`[00.32]`.
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/ui/formatters.py`, `tests/`
- **Acceptance Criteria:**
  - [ ] `git-sync pull --preview`, `git-sync push --preview`, and `git-sync resolve --preview` emit English `warnings` in JSON mode without translating back to Portuguese.
  - [ ] `legacy_preview_item` is either removed or becomes a pass-through identity helper.
  - [ ] All preview/confirm test assertions in `tests/` are updated and passing in English.

---

## Release / Cycle Wrap-up (Not the next task)

Release/tag only with explicit human request. When triggered, the ID is `[99.1]`. Do not number feature, hygiene, or CI tasks as `99.x`. Do not calculate next task ID from this section.

---

## Future Backlog / Ideas (Unprioritized)

- Visual TUI mode with curses or standard library terminal primitives.
