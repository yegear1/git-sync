---
id: "k-006"
title: "Global Installation Symlink Hygiene and Shell Autocompletion Architecture"
date: 2026-10-21
tags:
  - "installation"
  - "symlink"
  - "path"
  - "bash-completion"
  - "zsh-completion"
  - "posix"
severity: "low"
affected_components:
  - "src/git_sync/installer.py"
  - "src/git_sync/cli.py"
  - "git-sync"
  - "scripts/install.sh"
symptoms:
  - "ModuleNotFoundError: No module named 'git_sync' when running launcher via symlink from outside repository"
  - "Broken or dangling symlinks if installation process is interrupted"
  - "Shell tab autocompletion hanging or lagging in terminal"
  - "Installed binary not found due to isolated profile HOME differing from host PATH"
---

# [k-006] Global Installation Symlink Hygiene and Shell Autocompletion Architecture

## Problem / Symptom

1. **Working Directory Path Resolution:** When executing Python CLI utilities via symlinks located in system bin paths (`~/.local/bin/git-sync`), naive relative path resolution (`Path(__file__).parent / "src"`) resolves relative to the symlink directory (`~/.local/bin/src`), causing fatal `ModuleNotFoundError`.
2. **Race Conditions & Interrupted Symlink Writes:** Directly removing and re-symlinking an active binary can leave developers or background subshells with broken links if interrupted midway.
3. **Multi-Profile Environment Drift:** In environments with isolated user profile directories (e.g. Antigravity or containerized user profiles where `$HOME` points to a profile directory), `~/.local/bin` may differ from the host user's active PATH entry (e.g. `/home/$USER/.local/bin`), leading to false missing PATH warnings or inaccessible executables.
4. **Shell Completion Latency:** Autocompletion scripts that query remotes or execute slow subprocess chains freeze the developer's interactive shell during tab-completion.

---

## Approved Solution

1. **Canonical Symlink Resolution in Launcher (`git-sync`):**
   - Use `Path(__file__).resolve().parent / "src"` in the launcher script.
   - Calling `.resolve()` traverses all intermediate symlinks to the canonical repository root, guaranteeing that `sys.path.insert(0, str(src_dir))` locates `git_sync` regardless of the invocation directory or symlink location.
2. **Atomic Symlink Deployment (`install_symlink`):**
   - Generate a temporary symlink in the destination directory: `tmp_link = target_dir / f".git-sync-tmp-{pid}-{timestamp}"`.
   - Atomically replace the destination using `os.replace(tmp_link, dest_link)`.
   - Guarantees POSIX atomicity and prevents broken link windows.
3. **PATH Candidate Preference:**
   - Check `$XDG_BIN_HOME`, `$HOME/.local/bin`, and primary host user home (`pwd.getpwuid(os.getuid()).pw_dir / ".local/bin"`).
   - Prefer the candidate directory already present in the current `$PATH` environment variable.
4. **Sub-60ms Dynamic Autocompletion (`--complete-repos`):**
   - Provide `git-sync --complete-repos` short-circuiting parser and inspections to instantly enumerate relative repository paths.
   - In Bash and Zsh completion scripts, query `git-sync --complete-repos 2>/dev/null` for repository arguments while preserving static completions for subcommands and flags.

---

## What NEVER to Do

- **NEVER** use non-atomic `unlink` followed by `symlink` in global install routines. Use temporary file creation and `os.replace`.
- **NEVER** initiate network fetches or full status audits during shell autocompletion. Autocompletion must return in under 100ms.
- **NEVER** resolve launcher paths with `Path(__file__).parent` without calling `.resolve()`. Symlinks will break import resolution immediately.
