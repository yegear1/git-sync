# TASK.md — Current Task and Roadmap

> WHAT to do now. Detailed history lives in `git log` and `ARCHIVE.md`. User requests during conversation
> take precedence — report discrepancies before acting.

---

## Active Task

### 📌 Task [00.1]: Fine-Grained Partial Worktree Branch Synchronization and Conflict Isolation

- **Description:** Isolate active branches checked out across secondary worktrees from colliding bulk pull operations and provide granular progress reporting per individual worktree target.
- **Systems Involved:** `src/git_sync/engine/git.py`, `src/git_sync/cli.py`, `tests/test_git_sync.py`
- **Action Type:**
  - [x] Read-only / Documentation
  - [x] Source code changes
- **Status:** PLANNING

### Acceptance Criteria
- [ ] Active branch checkouts across secondary worktrees are isolated during auxiliary branch fast-forwards.
- [ ] Secondary worktree update progress reported per-target worktree path.
- [ ] Canonical test suite passes with exit code 0.

---

## Completed Tasks Log

| Task | Title | Commit(s) | Date |
|---|---|---|---|
| [00.0] | Initial clean public baseline of git-sync workspace orchestrator | [baseline] | 2026-10-27 |

---

## Backlog (Upcoming, in priority order)

- [00.2]: Interactive Webhook and Daemon Listener for Instant Workspace Sync
  - Support background socket or webhook event processing for event-driven pull triggers.

---

## Release / Cycle Wrap-up (Not the next task)

Release/tag only with explicit human request. When triggered, the ID is `[99.1]`. Do not number feature, hygiene, or CI tasks as `99.x`. Do not calculate next task ID from this section.

---

## Future Backlog / Ideas (Unprioritized)

- Visual TUI mode with curses or textual.
- Webhook trigger integration for automatic local pulls.
