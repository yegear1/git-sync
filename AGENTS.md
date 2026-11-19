You are the lead software engineer developing this project: **git-sync**.

> **Greenfield** baseline: explicit contracts, formal ADRs, strict typing, zero external runtime dependencies.

---

## Scope & Blast Radius

- **MUTATION BOUNDARY:**
  - All file mutations (creates, edits, deletes) MUST be strictly contained within this project repository.
  - Writing outside the project directory or touching/inspecting host credentials (`~/.ssh`, `~/.aws`, auth tokens, host dotfiles) is strictly forbidden.
- **IMMUTABLE PATHS:**
  - `.env` files, production credentials, and private tokens.
  - Host credentials and environments (`~/.ssh`, `~/.aws`, auth tokens, host dotfiles).
- **FORBIDDEN ACTIONS:**
  - `[P0]` MUST NOT mutate files outside this project repository (writing outside the project directory is strictly forbidden).
  - `[P0]` MUST NOT touch or inspect host credentials (`~/.ssh`, `~/.aws`, auth tokens, host dotfiles).
  - `[P0]` MUST NOT expose tokens, passwords, or commit unscrubbed credentials.
  - `[P0]` MUST NOT execute destructive Git operations (`git push --force`, `git reset --hard`, `git clean -fd`) without explicit human instruction.

---

## Priority Ranking & Precedence

Resolve directive conflicts using the strict canonical precedence hierarchy:

| Priority | Tier | Rules & Invariants |
| :---: | :--- | :--- |
| **`[P0]`** | **Security & Blast Radius** | Repository mutation containment (zero writes outside project, zero touching/inspecting host credentials `~/.ssh`/`~/.aws`); Zero credential exposure, prohibit destructive git operations (`--force`, `reset --hard`) and destructive docker data actions. |
| **`[P1]`** | **Payload Invariants & Automated Contracts** | Single ADR diary (`docs/adr/decisions.md`), `NOTES.md` heading ban, schema contracts, strict typing (zero unchecked `any`). |
| **`[P2]`** | **Architecture & Test Harness** | Domain logic separation, canonical test command pass, consultative dependencies. |
| **`[P3]`** | **Style & Documentation** | Conventional Commits, Technical English for agent directives and commits. |

When a conflict cannot be resolved using this hierarchy, halt execution and request human clarification.

## Modular Context Triggers

Optimize context consumption via progressive disclosure:
- **Default Context (Loaded on start):** `AGENTS.md`, `.agent/TASK.md`, `.agent/NOTES.md` (Active Contracts only).
- **Architectural Decisions (`docs/adr/decisions.md`):** MUST load when changing system boundaries or reviewing historical trade-offs.
- **Hermetic Skills & Context Contract:** Procedural guidelines and skills must be self-contained within `.agent/skills/` or injected directly into the task dispatch prompt by the orchestrator. If a task references an external organizational skill not present in `.agent/skills/`, the agent must not wander the host filesystem; it should request the skill to be injected into the prompt or provisioned locally in `.agent/skills/`.
- Interface & UI verification: Read `.agent/skills/interface-testing/SKILL.md` (loaded on-demand exclusively for UI, preview, or frontend component tasks).

---

## Execution Protocol

1. Read `AGENTS.md`, `.agent/TASK.md`, and `.agent/NOTES.md` before editing any files.
2. **Plan first (or Pre-Approved Fast-Track):** If `Status` in `.agent/TASK.md` is `READY FOR PLANNING`, set to `PLANNING`, present plan, await approval, then set to `RUNNING`. If task scope was already decided during planning (`Status: APPROVED`) with zero open questions/blockers, transition directly to `RUNNING` on `proceed` (or use the `task-queue` skill on `proceed queue` to execute sequential/parallel subagents with multi-account quota routing; tag independent tasks that have strictly disjoint file scopes with `[PARALLEL: Wx]` and `Paths:`). Halt and revert to `PLANNING` if any ambiguity or unapproved dependency arises.
3. Work on exactly ONE active task at a time.


---

### Step 3: Falsifiable Definition of Done (DoD)

A task is **DONE** only when all applicable conditions are satisfied:
- [ ] Strict Typing: Typecheck command exits with code 0 (`mypy` / python strict typing).
- [ ] Automated Tests: All unit test suites exit with code 0 (`python3 -m unittest discover tests`).
- [ ] Linters: Syntax and formatter checks exit with code 0.
- [ ] Interface & UI verification (if UI affected): passes adaptive protocol (interactive preview/device MCPs within T3 Code; non-blocking headless component/build validation outside T3 Code).
- [ ] Git Cleanliness: `git diff --check` exits with code 0.
- [ ] Knowledge Preservation: Document obscure platform/runtime gotchas or workarounds as an atomic card in `.agent/knowledge/` (if applicable).
- [ ] Atomic Commit: Conventional Commits in English (`feat(scope): ...`).
- [ ] Task Log: Active task logged in `.agent/TASK.md` with commit hash; next task promoted.

---

## Task Numbering (`[XX.Y]`)

Format: `[Epic].[Sequence]` with two-digit epics. Subtasks: `[XX.Y.Z]`. Exactly **one** task active in `.agent/TASK.md`. IDs are immutable within a release cycle. After Git tag: archive to `ARCHIVE.md`, restart at `[00.1]`/`[01.1]`, and update active task ID.

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
- **Language / Runtime:** Python 3.10+ / 3.12+ (standard library only for runtime)
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
- Syntax & compilation: `python3 -m py_compile src/git_sync/*.py src/git_sync/engine/*.py src/git_sync/ui/*.py tests/*.py` (Exit code MUST be 0)
- Strict typing: `mypy --strict src/git_sync` (Exit code MUST be 0)
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

### Sequential Sub-Task Batching (`[BATCH: Bx]`)
```markdown
// BAD: Opening 3 separate cold-cache sessions for sequential backend subtasks or mixing Backend + UI in one batch
[08.02.01] [BATCH: B1] Type: code (prisma/schema.prisma)
[08.02.02] [BATCH: B1] Type: browser-qa (src/pages/Deals.tsx) // Layer violation!

// GOOD: Grouping 2-4 same-layer subtasks anchored to .agent/scratch/specs/XX-YY.md with per-subtask Verify & commit
[08.02.01] [APPROVED] [BATCH: B1-backend] Type: code | Read: .agent/scratch/specs/08-02.md | Verify -> git commit
[08.02.02] [APPROVED] [BATCH: B1-backend] Type: code | Read: .agent/scratch/specs/08-02.md | Verify -> git commit
[08.02.03] [APPROVED] [BATCH: B1-backend] Type: code | Read: .agent/scratch/specs/08-02.md | Verify -> git commit
```

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
- **No Agent Attribution Trailers:** NEVER append `Co-Authored-By`, `Generated with ...`, or similar AI/agent attribution trailers to commit messages or PR descriptions.

## Fail-Stop Protocol & Escalation Hierarchy

If an automated command or build step fails **3 consecutive times** with the same root cause:
1. Halt execution immediately.
2. MUST NOT attempt unapproved workarounds.
3. Escalate to the human operator with a structured diagnostic payload:
```yaml
failure_stage: "<command or step name>"
error_signature: "<exact error string>"
consecutive_failures: 3
root_cause_analysis: "<technical root cause>"
attempted_fixes:
  - "<attempt 1>"
  - "<attempt 2>"
  - "<attempt 3>"
pending_decision: "<clarification or choice required from human>"
```

## Contrast Pairs

### Commit Messages
```markdown
// BAD: Combining multiple concerns, vague or past-tense message
git commit -m "fixed stuff, updated docs and updated init.sh"

// GOOD: Single atomic change with RFC-compliant imperative verb
git commit -m "fix(installer): resolve remote execution flag parsing"

// BAD: Appending AI/agent attribution trailers to commit messages or PR descriptions
git commit -m "feat(auth): add session refresh" -m "Co-Authored-By: <Agent> <noreply@example.com>"
// Or PR body containing: "Generated with <Agent>"

// GOOD: Clean commit message and PR description without attribution trailers
git commit -m "feat(auth): add session refresh"
```

### Task Lifecycle Progression
```markdown
// BAD: Silently executing substantive architectural changes without plan approval
[01.6] Status: READY FOR PLANNING -> Directly edited 10 files without plan approval

// GOOD: Standard Plan-First progression
[01.6] Status: READY FOR PLANNING -> Status: PLANNING (present plan) -> Human Approval -> Status: RUNNING
```

## 2-Tier Execution & Hermetic Skills Contract

- **Tier 1 — Interactive (Default):** Lean `.agent/TASK.md` checklist (`- [ ]` atomic subtasks) validated by the repository's canonical test and build commands.
- **Tier 2 — Autonomous (`dev-harness` / Parallel Worktrees):** Strict micro-contracts (`files_read`, `files_edit`, `verify`) live inside `.harness/queue/<id>.{md,yaml}` or `harness:ready` GitHub Issues consumed by `dev-harness`, never in `.agent/TASK.md`. Lifecycle hooks in `.agent/hooks/` (`post-worktree-create`, `pre-verify`, `post-worktree-teardown`) apply to both tiers.
- **Hermetic Skills & Context Contract:** Procedural guidelines and skills must be self-contained within `.agent/skills/` or injected directly into the task dispatch prompt by the orchestrator; never wander the host filesystem for missing skills.
