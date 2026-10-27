# Architecture Decision Records (ADRs)

---

## ADR 001: Zero External Dependencies for Runtime CLI

- **Status:** Approved
- **Date:** 2026-10-20
- **Context:** `git-sync` is executed directly from host terminals, AI agent subshells, and background tasks across different Antigravity profiles without guaranteed virtual environments.
- **Decision:** Use only Python 3 standard library modules (`argparse`, `concurrent.futures`, `subprocess`, `json`, `pathlib`, `sys`).
- **Consequences:** Eliminates dependency installation steps (`pip install`, `uv sync`), guarantees instant startup, and avoids dependency conflicts.

---

## ADR 002: Prompt Suppression and Concurrent ThreadPool

- **Status:** Approved
- **Date:** 2026-10-20
- **Context:** Auditing 30+ repositories sequentially takes ~30 seconds and can hang indefinitely if Git prompts for credentials.
- **Decision:** Set `GIT_TERMINAL_PROMPT=0` in subshell environments and use a `ThreadPoolExecutor` with a default of 12 workers.
- **Consequences:** Reduces full scan time from ~30s to <3s and prevents hanging background agent tasks.

---

## ADR 003: Non-Destructive Fast-Forward Sincronization Policy

- **Status:** Approved
- **Date:** 2026-10-20
- **Context:** Automated or bulk `pull` commands risk introducing merge commits or conflicts if local commits exist.
- **Decision:** Mandate `--ff-only` by default. Sincronize auxiliary tracking branches via `git fetch . origin/<branch>:<branch>` to avoid switching working trees.
- **Consequences:** Completely prevents unintended merge commits or working-tree dirty conflicts during bulk sync.

---

## ADR 004: Agent Token Efficiency and Tool Directory Isolation

- **Status:** Approved
- **Date:** 2026-10-21
- **Context:** AI agents executing `git-sync status --json` in workspaces with dozens of repositories ingest tens of kilobytes (>10,000 tokens) of redundant metadata when 90% of repos are up to date. Furthermore, agentic workspaces (e.g., Multigravity worktrees) create transient `.multigravity/worktrees/` which trigger false-positive `NO_REMOTE` error reports.
- **Decision:**
  1. Introduce `--compact` / `--summary` payload formatting for `status --json` to emit high-level operational sets (`pull_pending`, `push_pending`, `dirty`, etc.) while omitting the extensive `items` list unless explicitly requested.
  2. Filter out hidden/agentic tooling directories (`.multigravity`, `.agent`, `.claude`, `.git-sync`) from directory tree discovery by default.
  3. Resolve workspace root dynamically via `GIT_SYNC_ROOT` -> `~/github` -> `~/Github` before hardcoded legacy path fallback.
- **Consequences:** Reduces agent token ingestion by >90% per status poll, eliminates ghost worktree error loops, and ensures zero-configuration portability across distinct host environments.

---

## ADR 005: Bulk Cache Optimizations, Multi-Remote Coordination, and Performance Telemetry

- **Status:** Approved
- **Date:** 2026-10-21
- **Context:** Auditing large multi-repository workspaces incurs redundant subprocess overhead:
  1. Repositories without `.gitmodules` still spawned `git submodule status` (~20-50ms per repo).
  2. Sibling worktrees in the same repository family independently queried `git worktree list --porcelain`.
  3. Repositories with auxiliary remotes (e.g. `upstream`, `backup`) only fetched `origin`, leaving auxiliary tracking branches stale.
  4. CI/CD runs and agent loops lacked high-resolution telemetry measuring worker pool concurrency, stage bottlenecks, and throughput (repos/sec).
- **Decision:**
  1. Implement `BulkCache` with thread-safe memoization of repository remotes and worktree topologies, auto-populating sibling worktrees across the same family upon initial topology discovery.
  2. Fast-fail submodule status queries by verifying the existence of `.gitmodules` before spawning Git subprocesses.
  3. Implement multi-remote coordination: query all remotes, execute `git fetch --all` with primary remote fallback, and support targeting specific remotes via `--remote <name>`.
  4. Implement `TelemetryBenchmark` measuring stage durations (`discovery`, `inspection`, `pull`, `push`) and throughput, surfaced via `--benchmark` / `--telemetry` in both text tables and JSON payloads (`telemetry` schema).
- **Consequences:** Eliminates dozens of redundant subprocess invocations across large workspaces, enables multi-remote tracking synchronization, and provides verifiable performance telemetry for agent execution loops.

---

## ADR 006: Global Installation Symlink Hygiene and Shell Autocompletion Architecture

- **Status:** Approved
- **Date:** 2026-10-21
- **Context:** Executing `git-sync` from arbitrary paths required configuring `PYTHONPATH` or calling the full repository path. Furthermore, interactive CLI shell usage (Bash and Zsh) lacked fast completion for subcommands, flags, and repository names.
- **Decision:**
  1. Provide safe, idempotent global deployment (`git-sync install`) into `~/.local/bin/git-sync` using atomic symlink replacement (`tempfile` + `os.replace`) to prevent broken link states and zero hanging.
  2. Maintain zero external runtime dependencies by generating hermetic, self-contained completion scripts (`git-sync completion bash` and `git-sync completion zsh`).
  3. Support fast dynamic repository enumeration via internal short-circuit hook (`--complete-repos`) reading discovered relative repository paths without executing full status audits.
  4. Provide automated completion installer (`git-sync completion <shell> --install`) writing to standard user directories (`~/.local/share/bash-completion/completions/` or `~/.zfunc/`).
- **Consequences:** Developers and AI agents can invoke `git-sync` from any working directory out of the box with zero runtime setup, complete tab auto-completion, and atomic installation hygiene.
