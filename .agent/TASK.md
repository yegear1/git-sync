# TASK.md — Current Task and Roadmap

> WHAT to do now. Detailed history lives in `git log` and `ARCHIVE.md`. User requests during conversation
> take precedence — report discrepancies before acting.

---

## Active Task

### 📌 Task [00.10]: Worktree Stale Pruning and Orphaned Worktree Cleanup Command

- **Description:** Extend `git-sync` with `git-sync worktree prune` and status diagnostics to identify detached/orphaned worktree admin directories and clean broken metadata safely.
- **Systems Involved:** `src/git_sync/engine/git.py`, `src/git_sync/cli.py`, `tests/test_git_sync.py`
- **Action Type:**
  - [x] Read-only / Documentation
  - [x] Source code changes
- **Status:** PLANNING

### Acceptance Criteria
- [ ] `git-sync worktree prune` cleans missing worktree metadata and warns on dirty linked worktrees.
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

---

## Backlog (Upcoming, in priority order)

### 📌 Task [00.11]: Shallow Submodule Deepening and Unshallow Fetch Recovery
- **Description:** Extend `git-sync` with automated detection of truncated shallow submodule trees (`.git/shallow`) and guided unshallow fetching (`--unshallow`) during branch synchronization.
- **Systems Involved:** `src/git_sync/engine/git.py`, `src/git_sync/cli.py`, `tests/test_git_sync.py`
- **Acceptance Criteria:**
  - [ ] Shallow submodule clones are safely detected and unshallowed without corrupting history.
  - [ ] Canonical test suite passes with exit code 0.

---

## Release / Cycle Wrap-up (Not the next task)

Release/tag only with explicit human request. When triggered, the ID is `[99.1]`. Do not number feature, hygiene, or CI tasks as `99.x`. Do not calculate next task ID from this section.

---

## Future Backlog / Ideas (Unprioritized)

- Visual TUI mode with curses or standard library terminal primitives.
