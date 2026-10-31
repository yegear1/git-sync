---
id: "k-012"
title: "Unit Separator Log Framing and Deterministic Conventional Commit Classification"
date: 2026-10-31
tags:
  - "git"
  - "conventional-commits"
  - "keep-a-changelog"
  - "parsing"
severity: "low"
affected_components:
  - "src/git_sync/engine/git.py"
  - "src/git_sync/cli.py"
  - "src/git_sync/ui/dashboard.py"
  - "tests/test_git_sync.py"
symptoms:
  - "Multiline Git commit bodies or embedded lists corrupt standard line-based git log parsers"
  - "External changelog tools introduce heavy node/python runtime dependencies and latency"
---

# [k-012] Unit Separator Log Framing and Deterministic Conventional Commit Classification

## Problem / Symptom

When inspecting pending Git commit ranges (`HEAD..@{u}` incoming and `@{u}..HEAD` outgoing):
1. Parsing `git log` output using standard newline splitting (`\n`) breaks when developers write multi-line commit descriptions or include Markdown lists / paragraphs in commit bodies.
2. Standard Conventional Commit parsers or changelog generators often introduce external runtime dependencies (`npm`, `pydantic`, `rich`, or LLM API calls) which violate the zero-dependency invariant of `git-sync`.
3. AI coding agents and human developers inspecting pending workspace status lack fast operational visibility into what types of changes (`Added`, `Changed`, `Fixed`, `Breaking`) are pending pull or push.

---

## Approved Solution

1. **Deterministic Unit-Separator Framing (`%h%x00%s%x00%b%x1f`):**
   Execute `git log <range> --format="%h%x00%s%x00%b%x1f"`. The ASCII unit separator (`0x1F`) unambiguously delimits commit records across arbitrary newlines, and null bytes (`0x00`) delimit hash, subject, and body fields.

2. **Conventional Commit to Keep-a-Changelog Classification:**
   - Parse type, optional scope, breaking indicator (`!`), and description via regex:
     `^(?P<type>[a-zA-Z0-9_\-]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<description>.*)$`
   - Map types to Keep-a-Changelog buckets:
     `feat` -> `Added`, `fix` -> `Fixed`, `perf`/`refactor`/`docs`/`chore`/`build`/`ci`/`test` -> `Changed`, `revert`/`remove`/`deprecate` -> `Removed`, `sec`/`security` -> `Security`.
   - Breaking changes are flagged if `!` is present in the header prefix or if `BREAKING[- ]CHANGE:` appears in body/footers, classifying into the `Breaking` category.
   - Non-conventional commit messages fall back to deterministic keyword heuristics (`add`, `fix`, `remove`, `security`).

3. **Status and Dashboard Integration:**
   - `git-sync status --changelog`: Displays category breakdowns and commit lists in text tables, and structured `changelogs` in JSON summaries.
   - Interactive Dashboard (`dashboard` / `tui`): Displays category badges and exposes the `[c]` key to inspect changelogs interactively.
