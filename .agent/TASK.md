# TASK.md — Current Task and Roadmap

> WHAT to do now. Detailed history lives in `git log` and `ARCHIVE.md`. User requests during conversation
> take precedence — report discrepancies before acting.

---

## Active Task

### 📌 Task [00.4]: Hierarchical Config Merging, Source Provenance Tracking, and Interactive Config Bootstrap

- **Description:** Upgrade configuration resolution to deep-merge across precedence layers (`defaults < XDG config < ~/.git-sync.json < ./.git-sync.json < env < profile < CLI`), track override provenance in `GitSyncConfig.sources: List[str]`, and add `git-sync config --init` / `--edit` with `$EDITOR` (`$VISUAL` / `nano`) and non-TTY path-only fallback.
- **Systems Involved:** `src/git_sync/config.py`, `src/git_sync/cli.py`, `.agent/NOTES.md`, `tests/test_config.py`
- **Action Type:**
  - [x] Read-only / Documentation
  - [x] Source code changes
- **Status:** PLANNING

### Acceptance Criteria
- [ ] `GitSyncConfig` records ordered `sources` list and exposes it in `git-sync config` (table and `--json`).
- [ ] Hierarchical deep-merge combines user-level XDG/home config with workspace-level `.git-sync.json` and env overrides.
- [ ] `git-sync config --init` / `--edit` creates default template if missing without clobbering, opens `$EDITOR` on TTY, or prints file path when piped/non-TTY.
- [ ] Canonical test suite passes with exit code 0.

---

## Completed Tasks Log

| Task | Title | Commit(s) | Date |
|---|---|---|---|
| [00.0] | Initial clean public baseline of git-sync workspace orchestrator | [baseline] | 2026-10-27 |
| [00.1] | Fine-Grained Partial Worktree Branch Synchronization and Conflict Isolation | [92e882d] | 2026-10-28 |
| [00.2] | Open Source Readiness, MIT Licensing, Sanitization, and Community Governance | [6442a85, 2e0e7bb] | 2026-10-31 |
| [00.3] | Visual TUI and Compact Interactive Dashboard | [08769e6] | 2026-10-31 |

---

## Backlog (Upcoming, in priority order)

### 📌 Task [00.5]: Token-Budget Aware Diff Truncation for Agent and CLI Inspection
- **Description:** Implement deterministic, file-boundary aware diff truncation (`truncate_diff(diff, max_tokens)`) splitting on `diff --git ` headers so `git-sync resolve --diff` and agent JSON payloads never exceed context token budgets.
- **Systems Involved:** `src/git_sync/engine/git.py`, `src/git_sync/cli.py`, `.agent/NOTES.md`, `tests/test_resolve.py`
- **Acceptance Criteria:**
  - [ ] `truncate_diff` preserves whole file chunks up to `max_tokens` (~4 chars/token), partially truncates oversized single-file diffs, and appends an explicit `[... truncated X more file(s), ~Y chars ...]` marker.
  - [ ] CLI exposes `--max-tokens <n>` on `resolve --diff` (with safe default in `--compact` mode).
  - [ ] Canonical test suite passes with exit code 0.

### 📌 Task [00.6]: Deterministic Conventional Commit Classifier for Pending Upstream & Local Commits
- **Description:** Parse pending commit ranges (`HEAD..@{u}` and `@{u}..HEAD`) using null/unit-separator log formatting (`%h%x00%s%x00%b%x1f`) and classify Conventional Commits (`feat`, `fix`, `perf`, `refactor`, `revert`, breaking `!`) into Keep-a-Changelog buckets (`Added`, `Changed`, `Fixed`, `Removed`, `Security`, `Breaking`) without external dependencies or LLM calls.
- **Systems Involved:** `src/git_sync/engine/git.py`, `src/git_sync/cli.py`, `.agent/NOTES.md`, `tests/`
- **Acceptance Criteria:**
  - [ ] Deterministic parser classifies Conventional Commits and flags breaking changes (`feat!:`, `fix!:`).
  - [ ] `git-sync status --changelog` (and JSON/TUI views) summarizes incoming/outgoing commit categories per pending repository.
  - [ ] Canonical test suite passes with exit code 0.

### 📌 Task [00.7]: Native `git sync` Subcommand Autocompletion and SVG Terminal Demo
- **Description:** Extend Bash and Zsh completion scripts so native `git sync <TAB>` invocation resolves subcommands and dynamic repository names alongside `git-sync <TAB>`, and add a lightweight vector terminal showcase (`docs/demo.svg`) with text fallback in `README.md` and `README.pt-br.md`.
- **Systems Involved:** `src/git_sync/installer.py`, `docs/demo.svg`, `README.md`, `README.pt-br.md`, `tests/test_install_and_completion.py`
- **Acceptance Criteria:**
  - [ ] Bash (`_git_sync`) and Zsh (`#compdef git-sync git-sync=git`) completions support both `git-sync` and `git sync`.
  - [ ] `docs/demo.svg` (<10 KB pure SVG) embedded in `README.md` and `README.pt-br.md` with `<details>` text fallback.
  - [ ] Canonical test suite passes with exit code 0.

---

## Release / Cycle Wrap-up (Not the next task)

Release/tag only with explicit human request. When triggered, the ID is `[99.1]`. Do not number feature, hygiene, or CI tasks as `99.x`. Do not calculate next task ID from this section.

---

## Future Backlog / Ideas (Unprioritized)

- Visual TUI mode with curses or standard library terminal primitives.
