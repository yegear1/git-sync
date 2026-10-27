# Knowledge Card: k-001-git-terminal-prompt-and-parallel-walk

- **Title:** Non-Interactive Git Subshell Hanging & Directory Traversal Invariants
- **Domain:** Git / Python Subprocess / Concurrency
- **Date:** 2026-10-20
- **Applies to:** `git-sync`

---

## 1. Problem Description

When running batch Git commands (like `git fetch` or `git pull`) across dozens of repositories in an automated CLI or agent session:
1. If any repository requires credentials or an SSH key passphrase, Git defaults to launching an interactive terminal prompt. In headless/agent execution environments, this causes the process to hang forever until timed out.
2. When traversing directories with `os.walk(root)` to discover `.git` repositories, adding `".git"` to the blacklist of directories to ignore *before* inspecting `dirnames` strips `.git` prematurely, causing the discovery logic to report 0 repositories found.

---

## 2. Solution & Invariants

1. **Prompt Suppression:** Always inject `GIT_TERMINAL_PROMPT="0"` into the subprocess environment and provide explicit command timeouts (`timeout=20`).
2. **Directory Traversal Invariant:** In `os.walk`:
   ```python
   # Filter non-relevant branches first
   dirnames[:] = [d for d in dirnames if d not in {"node_modules", ".venv", "dist"}]
   # Check for .git repository presence
   if ".git" in dirnames:
       repos.append(dirpath)
       dirnames.remove(".git") # Prevent recursing inside .git itself
   ```
3. **Atomic Tracking Branch Fast-Forward:** To update auxiliary branches without checking them out:
   ```bash
   git fetch . origin/<branch>:<branch>
   ```
