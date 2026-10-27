# Project Skills Guide

This directory contains procedural skills tailored specifically to this project's architecture and domain.

Organizational cross-cutting procedures (UI design, QA playbooks, host infrastructure) belong in [`ye-sandbox/agent-skills`](https://github.com/ye-sandbox/agent-skills) and run from `~/.cursor/skills`.

## When to Create a Local Skill

Create a new skill in this folder (`.agent/skills/<name>/SKILL.md`) when:
1. A multi-step procedure (>3 steps) is repeated frequently across tasks.
2. An architectural convention requires strict sequencing (e.g., migrations, complex domain mutations).
3. The pattern involves specific MCP tools and local commands.

Use `000-template.md` as the baseline structure when drafting new skills.
