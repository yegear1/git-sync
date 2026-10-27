# Knowledge Card Specification & Schema (`.agent/knowledge/`)

This document defines the formal schema and authoring standards for **Knowledge Cards** stored in `.agent/knowledge/`.

---

## 1. Purpose & Progressive Disclosure

Knowledge Cards capture atomic, reusable engineering lessons, platform quirks, and workarounds for obscure runtime/build bugs.

### Context Economy
- **Do NOT put runtime gotchas in `NOTES.md`:** `NOTES.md` is default always-on context loaded on every turn; storing transient gotchas there leads to context bloat.
- **Do NOT create full incident postmortems for everyday gotchas:** `docs/incidents/` is reserved for Tier 3 system-wide outages and post-mortems.
- **On-Demand Retrieval:** `.agent/knowledge/` cards are indexed by filename and YAML frontmatter (`tags`, `symptoms`), allowing agents to search and load them via `grep_search` or `find_by_name` only when troubleshooting relevant components.

---

## 2. File Naming Convention

Every knowledge card must adhere to the following naming pattern:

```text
k-<3-digit-id>-<kebab-case-slug>.md
```

Examples:
- `k-001-docker-mtu-blackhole.md`
- `k-002-sqlite-wal-checkpoint-deadlock.md`
- `k-003-prisma-engine-alpine-musl.md`

`k-000-template.md` is reserved as the canonical authoring template.

---

## 3. Metadata Specification (YAML Frontmatter)

Every card MUST start with a valid YAML frontmatter block containing the following fields:

```yaml
---
id: "k-001"                         # Required. Format: k-XXX (zero-padded 3-digit integer)
title: "Imperative Title"           # Required. Concise description of the gotcha or fix
date: YYYY-MM-DD                    # Required. ISO-8601 date when the card was recorded
tags:                               # Required. Array of 2-6 lowercase keywords for ripgrep search
  - "runtime"
  - "docker"
  - "networking"
severity: "medium"                  # Required. Enum: [low, medium, high, critical]
affected_components:                # Required. Paths, services, or modules impacted
  - "compose.yaml"
  - "src/infra/network.ts"
symptoms:                           # Required. Array of exact error strings or observable signs
  - "TLS handshake timeout"
  - "connection reset by peer on MTU > 1420"
---
```

### Field Definitions

| Field | Type | Description |
|---|---|---|
| `id` | string | Immutable 3-digit card index prefixed by `k-`. Matches the filename prefix. |
| `title` | string | Short, imperative title summarizing the problem and domain. |
| `date` | string | Creation date in `YYYY-MM-DD` format. |
| `tags` | string[] | Technology, protocol, and library keywords to aid agent ripgrep queries. |
| `severity` | enum | Impact level: `low` (cosmetic/minor), `medium` (build/test failure), `high` (runtime degradation), `critical` (crash/data hazard). |
| `affected_components` | string[] | Specific files, subsystems, or configuration entries affected. |
| `symptoms` | string[] | Exact error messages, stack trace keywords, or behavior flags to facilitate match-per-line searches. |

---

## 4. Required Markdown Sections

Following the frontmatter, the body MUST contain the three canonical sections:

### 1. `## Problem / Symptom` (`Problema`)
- Document the exact failure symptoms, error logs, stack traces, and reproduction environment.
- State the conditions under which the issue occurs (e.g., OS, kernel, container network, dependency version).

### 2. `## Approved Solution` (`Solução Aprovada`)
- Provide the verified, deterministically reproducible fix.
- Include explicit code diffs, configuration adjustments, or CLI commands.
- Explain the underlying root cause succinctly (why this fix works).

### 3. `## What NEVER to Do` (`O que NUNCA fazer` / Anti-Patterns)
- Explicitly list naive or destructive workarounds that were attempted or might be attempted by an LLM (e.g., disabling SSL verification, running as root, `chmod 777`, arbitrary sleep timers).
- Explain *why* these antipatterns are rejected.

---

## 5. Optional Sections

- `## Verification`: Exact smoke test or validation command verifying the resolution.
- `## References`: Links to upstream bug trackers, GitHub issues, or official vendor documentation.
