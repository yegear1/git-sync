You are the lead software engineer developing this project: **git-sync**.

> **Greenfield** baseline: explicit contracts, formal ADRs, strict typing, zero external runtime dependencies.

---

## Non-Negotiable Engineering Standards

A task is **DONE** only when all applicable conditions are satisfied:
   - [ ] Strict Typing: Typecheck command exits with code 0 (`mypy` / python strict typing).
   - [ ] Automated Tests: All unit test suites exit with code 0 (`python3 -m unittest discover tests`).
   - [ ] Linters: Syntax and formatter checks exit with code 0.
   - [ ] Git Cleanliness: `git diff --check` exits with code 0.
   - [ ] Knowledge Preservation: Document obscure platform/runtime gotchas or workarounds as an atomic card in `.agent/knowledge/` (if applicable).
   - [ ] Atomic Commit: Conventional Commits in English (`feat(scope): ...`).
   - [ ] Task Log: Active task logged in `.agent/TASK.md` with commit hash; next task promoted.

---

## Task Numbering (`[XX.Y]`)

Format: `[Epic].[Sequence]` with two-digit epics. Subtasks: `[XX.Y.Z]`. Exactly **one** task active in `RUNNING` status. IDs are immutable within a release cycle. After Git tag: archive to `ARCHIVE.md`, restart at `[00.1]`/`[01.1]`, and update active task ID.

**Next ID:** Derived solely from Active Task + Log of current cycle. Ignore Future Backlog and closing sections. Same epic → `Y+1`. New epic → `[XX+1.1]`. Never jump to `90.x`/`99.x` unless performing refactoring/release explicitly requested by user.

**Release:** `[99.1]` is not a queue item. It becomes active only with explicit human instruction. Never trigger release tags autonomously; never treat `99.x` as an artificial ceiling.

When human requests release:
1. **Archive:** Move completed log from `TASK.md` to `ARCHIVE.md` under `## [vX.Y.Z] - YYYY-MM-DD`.
2. **Clear log:** Keep only header and empty table with next cycle seed (`[00.1]` or `[01.1]`).
3. **Commit:** `chore(release): prepare vX.Y.Z`.
4. **Tag:** `git tag -a vX.Y.Z -m "vX.Y.Z"`.

---

## Operating Invariants

- **Project Root:** Current repository root (`.`)
- **Default Workspaces Directory:** Configurable via `--root`, `GIT_SYNC_ROOT`, XDG config, or automatic discovery (`~/github`, `~/Github`, `~/src`, `.` / CWD)
- **OS / Shell:** Linux / POSIX Bash
- **Language / Runtime:** Python 3.12+ (standard library only for runtime)
- **Architecture:** Modular CLI utility (CLI parser, concurrent worker pool, Git subshell abstraction)
- **Global Path Entrypoint:** `~/.local/bin/git-sync`

---

## Mapped Skills

| Skill | Trigger & Scope |
|---|---|
| `git-sync` | High-level CLI for bulk repository inspection and fast-forward pull across workspace roots |
| `python-dev` | Python async, typing, standard library practices and test harnesses |

---

## Validation Commands

- Run tests: `python3 -m unittest discover -s tests` (Exit code MUST be 0)
- Syntax & compilation: `python3 -m py_compile src/git_sync/*.py tests/*.py` (Exit code MUST be 0)
- Smoke test: `python3 -m git_sync.cli status --no-fetch` (Exit code MUST be 0)

---

## Golden Rules

- **MUST NOT** introduce external third-party runtime dependencies (`requests`, `rich`, etc.) unless approved by the user; runtime MUST remain zero-dependency using Python standard library.
- **MUST NOT** execute Git operations without `GIT_TERMINAL_PROMPT=0` to guarantee that CLI processes never hang indefinitely in background tasks.
- **MUST NOT** use non-fast-forward pulls by default; pulls MUST enforce `--ff-only` unless explicitly overridden.
- **MUST NOT** invent ad-hoc test compilation loops. The test suite MUST run via the project's canonical test command (`python3 -m unittest discover -s tests`).
- **MUST NOT** break payload contracts documented in `.agent/NOTES.md`.
- **MUST** document non-obvious domain logic constraints, ordering dependencies, hardware/protocol quirks, and safety invariants as in-code comments using explicit invariant prefixes (`# INVARIANT: <rationale>`).
- **MUST** write all agent-facing directives, skills, contracts, and task logs (`AGENTS.md`, `.agent/*`) in concise Technical English to maximize instruction fidelity and minimize context token overhead.

---

## Git Conventions

- **Atomic Commits:** Each commit MUST represent a single logical change.
- **Conventional Commits:** MUST follow `<type>(<scope>): <summary in English imperative>`.
  - `feat`: new feature with automated test
  - `fix`: bug fix with regression test
  - `refactor`: structural change preserving behavior
  - `test`: test suite addition/update
  - `chore`: maintenance, dependencies, configs
  - `docs`: documentation only
- **Branch Strategy:** Trunk-based on `main`.
- **Safety:** Push only upon explicit user request; **MUST NOT** force-push (`--force`) to primary branches.
