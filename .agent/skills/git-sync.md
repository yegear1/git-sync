# Skill: git-sync

## Trigger
Use this skill when inspecting, auditing, or synchronizing Git repositories across user workspaces (`~/github` or configured root).

## Directives
- **DO NOT** execute ad-hoc Python loops or raw chained git commands in prompt bash tools.
- **DO** call `git-sync status --compact --json` (or `git-sync status --pending --compact --json`) to detect repositories requiring attention with maximum token savings (<1KB output).
- **DO** call `git-sync pull --all --json` to fast-forward all pending branches with autostash safety.
- **DO** call `git-sync push --dry-run --all --json` to audit outgoing commits safely before pushing.
- **DO** call `git-sync push --all --json` to push unpushed commits across repositories.
- **DO** call `git-sync dirty --json` to inspect unstaged changes.
