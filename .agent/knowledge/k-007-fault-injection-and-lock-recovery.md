---
id: "k-007"
title: "Git Index Lock Recovery, Fault Injection Hardening, and Concurrency Saturation"
date: 2026-10-21
tags:
  - "git"
  - "index-lock"
  - "fault-injection"
  - "concurrency"
  - "worker-saturation"
  - "stale-locks"
severity: "medium"
affected_components:
  - "src/git_sync/core.py"
  - "src/git_sync/cli.py"
  - "tests/test_stress_and_hardening.py"
symptoms:
  - "fatal: Unable to create '.git/index.lock': File exists caused by crashed git processes or race conditions"
  - "Operations blocking or failing when stale lock files remain on disk"
  - "Interrupted network fetches leaving dangling lock or corrupt temporary pack files"
  - "Worker pool contention or deadlocks during saturated concurrent operations across 50+ repositories"
---

# [k-007] Git Index Lock Recovery, Fault Injection Hardening, and Concurrency Saturation

## Problem / Symptom

1. **Stale Lock Contention from Crashed Git Subshells:** When background processes, terminal sessions, or prior sync operations are killed unexpectedly (OOM, SIGKILL, system reboot), Git leaves `.git/index.lock`, `FETCH_HEAD.lock`, or `shallow.lock` on disk. Subsequent operations fail with `fatal: Unable to create '.git/index.lock': File exists`.
2. **False Concurrency Interlocks:** Unaware automation agents may stall indefinitely or fail bulk operations across entire workspaces if even a single repository has a stale lock file.
3. **Transient Interrupted Fetches:** Subprocess network timeouts or connection termination mid-transfer (`fetch-pack: invalid index-pack output`, `RPC failed; curl 56`, `early EOF`, `protocol error: bad line length`) cause transient errors that must be safely retried without repository corruption.
4. **Worker Thread Saturation across 50+ Repositories:** High thread concurrency (`workers >= 32`) can trigger race conditions, thread pool starvation, file descriptor exhaustion, or corrupted cache state if synchronization primitives are not thread-safe.

---

## Approved Solution

1. **Lock File Auditing and Stale Age Classification (`find_repo_locks` & `find_locked_repos`):**
   - Inspect `.git/index.lock`, `FETCH_HEAD.lock`, `shallow.lock`, and `refs/heads/*.lock` without mutating repository state.
   - Calculate lock file age (`time.time() - mtime`). Classify locks older than threshold (default 300 seconds) as `stale`, while preserving fresh locks (`active`) to prevent race conditions with running Git commands.
2. **Safe Stale Lock Recovery (`recover_index_lock` & `recover_repo_locks`):**
   - Automatically refuse deletion of active locks (< 300s) unless `--force` is explicitly passed.
   - Safely remove confirmed stale lock files and return structured operational logs.
3. **Structured Diagnostics (`INDEX_LOCKED` Category):**
   - Parse `index.lock` errors in `diagnose_git_error` to output `category: "INDEX_LOCKED"`, `action_required: "unlock_index"`, and actionable suggestions.
4. **Automated Pull Recovery (`--recover-locks` / `recover_stale_lock=True`):**
   - In `pull_repo`, if an autostash or fast-forward pull encounters `INDEX_LOCKED` and `recover_stale_lock` is active, attempt stale lock recovery and retry the operation seamlessly.
5. **CLI `git-sync unlock` Subcommand:**
   - Expose `git-sync unlock [repos...] [--all] [--force] [--json]` to triage and clean stale locks across workspaces.
   - Surface locked repositories in `git-sync doctor` output with clear warnings and recommendations.
6. **Concurrency Saturation & Thread Safety:**
   - Guard shared cache and benchmark metrics using `threading.Lock`.
   - Scale worker pools smoothly up to 64 threads across 50+ repositories without deadlocks or unhandled exceptions.

---

## What NEVER to Do

- **NEVER** indiscriminately delete fresh `.git/index.lock` files without checking mtime age or verifying whether a Git subshell is actively writing. Doing so can cause repository index corruption.
- **NEVER** leave background subshells hanging when index locks are encountered; always fail fast with structured `INDEX_LOCKED` diagnostics or recover stale locks safely.
- **NEVER** mutate repository state when inspecting locks; auditing lock existence must be read-only.
- **NEVER** retry permanent authentication failures when encountering interrupted fetches; only retry transient network errors.
