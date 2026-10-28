# TASK.md — Current Task and Roadmap

> WHAT to do now. Detailed history lives in `git log` and `ARCHIVE.md`. User requests during conversation
> take precedence — report discrepancies before acting.

---

## Active Task

### 📌 Task [00.2]: Visual TUI and Compact Interactive Dashboard

- **Description:** Support high-density terminal dashboard mode for live multi-repository status and interactive pulls.
- **Systems Involved:** `src/git_sync/cli.py`, `src/git_sync/ui/`, `tests/`
- **Action Type:**
  - [x] Read-only / Documentation
  - [x] Source code changes
- **Status:** PLANNING

### Acceptance Criteria
- [ ] Interactive terminal dashboard view with real-time multi-repo status.
- [ ] Keyboard navigation and interactive pull trigger support.
- [ ] Canonical test suite passes with exit code 0.

---

## Completed Tasks Log

| Task | Title | Commit(s) | Date |
|---|---|---|---|
| [00.0] | Initial clean public baseline of git-sync workspace orchestrator | [baseline] | 2026-10-27 |
| [00.1] | Fine-Grained Partial Worktree Branch Synchronization and Conflict Isolation | [81009aa] | 2026-10-28 |

---

## Backlog (Upcoming, in priority order)

*(No upcoming tasks currently queued; next tasks will be derived as needed)*

---

## Release / Cycle Wrap-up (Not the next task)

Release/tag only with explicit human request. When triggered, the ID is `[99.1]`. Do not number feature, hygiene, or CI tasks as `99.x`. Do not calculate next task ID from this section.

---

## Future Backlog / Ideas (Unprioritized)

- Visual TUI mode with curses or standard library terminal primitives.
