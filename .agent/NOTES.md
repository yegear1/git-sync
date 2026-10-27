# NOTES.md — Active Contracts and Runtime Memory

> Stores the active runtime contracts (WHAT interacts with WHAT), CLI interface schemas, and critical gotchas.
> Historical architectural decisions live in [`docs/adr/decisions.md`](../docs/adr/decisions.md).

---

## Active Contracts

### 1. CLI Commands & Invocation Protocols

`git-sync` exposes ten core operations via subcommands:

| Subcommand | Flag Options | Primary Output | Use Case |
|---|---|---|---|
| `git-sync status` | `--fetch`/`--no-fetch`, `--pending`, `--compact`/`--summary`, `--remote <name>`, `--no-cache`, `--benchmark`, `--preflight`, `--retries <n>`, `--group <dir>`, `--json` | Table / JSON | Inspection of sync states across all repos in root |
| `git-sync doctor` | `--ssh`, `--group <dir>`, `--json` | Diagnostics table / JSON | Proactive credential sanity check (SSH agent, keys, credential helper, SSH multiplexing, remote reachability, locked repos) |
| `git-sync pull` | `[repos...]`, `--all`, `--dry-run`/`--no-dry-run`, `--ff-only`/`--no-ff-only`, `--autostash`/`--no-autostash`, `--sync-branches`/`--no-sync-branches`, `--strategy <abort\|rebase\|merge>`, `--remote <name>`, `--no-cache`, `--benchmark`, `--preflight`, `--retries <n>`, `--recover-locks`, `-i`/`--interactive`, `--group <dir>`, `--json` | Progress logs / JSON | Fast-forward update of current branch + auxiliary branches with dirty tree autostash, divergence resolution & stale lock recovery |
| `git-sync resolve` | `[repos...]`, `--all`, `--dry-run`/`--no-dry-run`, `--strategy <abort\|rebase\|merge\|stash>`, `--diff`, `-i`/`--interactive`, `--group <dir>`, `--fetch`/`--no-fetch`, `--json` | Interactive prompt / JSON | Guided diagnostics, diff inspection, and resolution for diverged branches |
| `git-sync push` | `[repos...]`, `--all`, `--dry-run`/`--no-dry-run`, `--branch <name>`, `--remote <name>`, `--no-cache`, `--benchmark`, `--preflight`, `--retries <n>`, `--group <dir>`, `--json` | Progress logs / JSON | Push committed branches to upstream remote tracking targets |
| `git-sync dirty` | `--group <dir>`, `--json` | List of modified files | Quick audit of uncommitted work |
| `git-sync unlock` | `[repos...]`, `--all`, `--force`, `--group <dir>`, `--json` | Progress logs / JSON | Safely inspect and recover active or stale Git lock files (.git/*.lock) |
| `git-sync config` | `--json` | Table / JSON | Inspection of active workspace profile and resolved flags |
| `git-sync install` | `--target-dir <dir>`, `--force`, `--uninstall`, `--json` | Progress logs / JSON | Safe idempotent global deployment of launcher symlink to `~/.local/bin/git-sync` |
| `git-sync completion` | `[bash\|zsh]`, `--install`, `--target-dir <dir>`, `--json` | Script / JSON | Generate or install shell autocompletion definitions with dynamic repo lookup |

Global options (usable before or after subcommands):
`-v`, `--version`, `--config <path>`, `--profile <name>`, `--root <dir>`, `--workers <n|auto>`, `--auto-tune` / `--adaptive`, `--timeout <sec>`, `--retries <n>`, `--preflight`, `--exclude <patterns...>`, `--compact`, `--summary`, `--benchmark`, `--telemetry`, `--no-cache`, `--remote <name>`, `--dry-run`, `--no-dry-run`, `--json`.

### 2. Agent JSON Schema (`--json`)

When invoked with `--json`, `git-sync status` provides two modes:

#### 2.1 Compact Mode (`--compact` or `--summary`) — Recommended for AI Agents (~90% token reduction)
Omit the heavy `items` list and output only high-level operational triage arrays:

```json
{
  "root": "/home/yegear/github",
  "total": 30,
  "pull_pending": ["ye-sandbox/template-agent"],
  "push_pending": ["ye-dev/git-sync"],
  "dirty": ["ye-dev/zimbra-glpi"],
  "diverged": [],
  "worktrees": [],
  "has_submodules": [],
  "no_remote": [],
  "no_upstream": [],
  "offline": [],
  "errors": [],
  "remediations": [
    {
      "repo": "ye-dev/scratch-repo",
      "status": "NO_REMOTE",
      "network_state": "unconfigured",
      "action_required": "configure_remote",
      "remediation": "git -C '...' remote add origin <url>"
    }
  ]
}
```

#### 2.2 Full Detail Mode (Default `--json`)
Outputs the top-level summary plus the complete array of repository metadata:

```json
{
  "root": "/workspace/repos",
  "total": 30,
  "pull_pending": ["sandbox/template-agent"],
  "push_pending": [],
  "dirty": ["dev/zimbra-glpi"],
  "diverged": [],
  "worktrees": [],
  "has_submodules": [],
  "no_remote": [],
  "errors": [],
  "items": [
    {
      "name": "template-agent",
      "path": "/workspace/repos/sandbox/template-agent",
      "rel_path": "sandbox/template-agent",
      "group": "sandbox",
      "branch": "main",
      "remote_url": "https://github.com/sandbox/template-agent.git",
      "remotes": {
        "origin": "https://github.com/sandbox/template-agent.git",
        "upstream": "https://github.com/upstream/template-agent.git"
      },
      "target_remote": "origin",
      "multi_remote": true,
      "is_dirty": false,
      "dirty_files": [],
      "fetch_ok": true,
      "fetch_error": null,
      "upstream": "origin/main",
      "ahead": 0,
      "behind": 2,
      "aux_branches_behind": {"baremetal": 1},
      "status": "PULL_PENDING",
      "action_required": "pull",
      "is_worktree": false,
      "worktree_main_path": null,
      "worktrees": [
        {
          "path": "/workspace/repos/sandbox/template-agent",
          "head": "a1b2c3d...",
          "branch": "main",
          "is_main": true
        }
      ],
      "submodules": []
    }
  ]
}
```

#### 2.3 Telemetry Benchmark Schema (`--benchmark` or `--telemetry`)
When `--benchmark` is combined with `--json`, a `telemetry` block is included in the output summary with stage, latency, concurrency, alerts, and failure breakdowns:

```json
{
  "duration_seconds": 0.23,
  "total_repos": 38,
  "workers": 12,
  "repos_per_second": 165.22,
  "stages": {
    "discovery": 0.09,
    "inspection": 0.14
  },
  "cache": {
    "hits": 15,
    "misses": 38
  },
  "concurrency": {
    "configured_workers": 12,
    "active_workers": 12,
    "optimal_workers": 12,
    "auto_tuned": true
  },
  "latencies": {
    "network_seconds": 0.11,
    "disk_seconds": 0.12,
    "avg_disk_seconds": 0.0032,
    "avg_network_seconds": 0.0029,
    "disk_threshold_seconds": 0.20,
    "network_threshold_seconds": 2.0
  },
  "failures": {
    "total": 0,
    "by_type": {}
  },
  "retries": {
    "total": 0,
    "recovered": 0
  },
  "alerts": [
    {
      "category": "DISK_CONTENTION",
      "severity": "WARNING",
      "message": "High average disk latency (0.280s > threshold 0.200s) indicates I/O contention across 12 workers.",
      "threshold_seconds": 0.20,
      "observed_seconds": 0.28,
      "recommended_workers": 6
    }
  ]
}
```

### 3. Workspace Profiles & `.git-sync.json` Schema

Configuration can be located in:
1. Custom `--config <path>` file.
2. `GIT_SYNC_CONFIG` environment variable path.
3. Current working directory `./.git-sync.json`.
4. User home directory `~/.git-sync.json`.

```json
{
  "root": "/workspace/repos",
  "workers": 12,
  "timeout": 20,
  "exclude": ["legacy", "third-party/*"],
  "groups": ["dev", "sandbox"],
  "pull": {
    "ff_only": true,
    "sync_branches": true,
    "autostash": true,
    "all": false
  },
  "push": {
    "dry_run": false,
    "all": false
  },
  "status": {
    "no_fetch": false,
    "pending": false
  },
  "default_profile": "work",
  "profiles": {
    "work": {
      "root": "/workspace/work",
      "groups": ["backend", "infra"]
    },
    "personal": {
      "root": "/workspace/personal",
      "exclude": ["archives"]
    }
  }
}
```

**Precedence Hierarchy (highest to lowest):**
1. Explicit CLI arguments (e.g. `--root /custom`, `--no-ff-only`, `--dry-run`, `--group dev`)
2. Selected Workspace Profile (`--profile <name>` or `default_profile`)
3. Top-level `.git-sync.json` or XDG `~/.config/git-sync/config.json` configuration
4. Environment variables (`GIT_SYNC_ROOT`, etc.)
5. Dynamic user home discovery (`~/github` -> `~/Github` -> `~/src` -> `~/projects`)
6. Current working directory fallback (`Path.cwd()`)

---

## Gotchas & Pitfalls

- **Git Credential Prompts:** Running `git fetch` or `git pull` in non-interactive background subshells will hang indefinitely if credentials or SSH passphrases are requested. **Mitigation:** Always set `GIT_TERMINAL_PROMPT=0` and pass execution timeouts to `subprocess.run`.
- **Directory Walking:** Never filter out `.git` before inspecting `dirnames` in `os.walk`; filtering `.git` prematurely prevents identifying the current directory as a Git repository.
- **Ephemeral Tooling and Multi-Agent Worktrees:** Multi-agent orchestrators create secondary linked worktrees under `.multigravity/worktrees/` or hidden tool directories. These worktrees lack standalone remotes and cause phantom `NO_REMOTE` errors if traversed by `discover_repos`. Tooling directories starting with `.` (e.g. `.multigravity`, `.git-sync`, `.agent`, `.claude`) MUST be pruned from discovery traversal.
- **Autostash and Dirty Tree Preservation:** When autostashing uncommitted changes during `git-sync pull`, staged and untracked files must be preserved using `git stash push -u -m "<tag>"`. If an unstash (`git stash pop`) encounters a merge conflict, the stash MUST NOT be discarded; the conflict must be isolated and flagged to the agent with instructions without data loss.
- **Fast-Forward Auxiliary Branches:** To update local branches that track remote branches without checking them out into the working tree, use `git fetch . origin/<branch>:<branch>`. This guarantees atomic fast-forward without disturbing the active worktree.
- **Worktree Active Branch Fetch Refusal:** `git fetch . origin/<branch>:<branch>` fails with exit code 128 if `<branch>` is checked out in an active secondary worktree. The branch must be skipped from auxiliary fetch in the primary repo and pulled in the worktree itself.
- **Nested Worktree False Dirty:** Worktrees nested inside a repository directory appear in `git status --porcelain` as untracked files (`?? worktrees/`). These must be matched against `git worktree list --porcelain` and excluded from `dirty_files`.
- **Submodule Discovery Deduplication:** Submodules have `.git` files pointing to `/modules/`. They must be excluded from `discover_repos` and audited via `git submodule status` from their parent repo to avoid duplicate operations.
- **Automated Conflict Recovery:** Automated rebases and merges during bulk operations must immediately invoke `git rebase --abort` or `git merge --abort` when conflicts are detected. This prevents leaving background subshells in hanging rebase loops or leaving persistent unmerged conflict markers in developer working copies.
- **Submodule Pre-Check Avoidance:** Never invoke `git submodule status` unless `.gitmodules` exists in the repository root. Spawning unnecessary subshells across dozens of standard repositories wastes hundreds of milliseconds of compute.
- **Multi-Remote Fetch Coordination:** When repositories configure auxiliary remotes (`upstream`, `backup`), invoking `git fetch --all` ensures tracking refs for auxiliary branches are updated. If `fetch --all` fails due to an unreachable auxiliary remote, a fallback fetch against the primary remote (`origin`) prevents false `FETCH_ERROR` failures.
- **Sibling Worktree Cache Population:** Inspecting linked worktrees returns the full topology for the repository family. Caching this topology across all member worktree paths eliminates duplicate `git worktree list --porcelain` subshell executions.
- **Terminal Progress Clean Erasure and Non-TTY Suppression:** Terminal progress indicators (`ProgressIndicator`) write ephemeral lines using carriage returns (`\r`). If stderr or stdout is redirected (pipes, files, subshells) or `--json` is enabled, indicators MUST automatically suppress all output. When active, lines MUST be cleanly erased with padded spaces upon completion to prevent terminal corruption.
- **Dry-Run Safety Invariant:** When `--dry-run` is active on `pull` or `resolve`, operations MUST NOT alter the repository state. Specifically, no working tree modifications, branch updates (`git pull` or `git fetch . b:b`), stash mutations (`git stash push`), rebases, or merges may be executed; operations must only inspect and preview feasibility.
- **Selective Network Retry vs Fast Auth Failure:** Only transient network issues (timeouts, connection resets, DNS hiccups, curl 56/28 errors) are retried with bounded exponential backoff (`retries=2`, doubling from 0.5s to 4.0s). Permanent authentication failures (`Permission denied (publickey)`, `Authentication failed`, `terminal prompts disabled`) MUST NEVER be retried; they must fail fast and attach actionable resolution guidance.
- **Non-Blocking Pre-flight Probing:** Pre-flight credential sanity checks (`git-sync doctor` and `--preflight`) probe unique remotes using `git ls-remote --heads` with `GIT_TERMINAL_PROMPT=0`. Unique remotes are deduplicated to prevent redundant network traffic across large workspaces.
- **Atomic Symlink Deployment & PATH Resolution:** Installing `git-sync` uses atomic symlink creation (via temporary link and `os.replace`) to prevent interrupted writes or dangling links. The launcher script dynamically resolves the underlying source tree via `Path(__file__).resolve().parent / "src"`. In multi-profile setups, user home PATH candidates (`$XDG_BIN_HOME`, `~/.local/bin`, `/home/$USER/.local/bin`) are inspected and preferred based on active presence in `$PATH`.
- **Fast Completion Dynamic Lookup:** Shell completions for Bash and Zsh invoke `git-sync --complete-repos`, an internal fast hook that executes workspace repository discovery and returns relative paths instantly (<60ms) without initiating remote network operations or full inspections.
- **PEP 621 Author Email Validation in setuptools:** Modern setuptools (`build_meta`) strictly enforces PEP 621 / RFC 5322 email syntax with valid FQDN domain (`user@domain.tld`). Using local addresses like `user@local` raises `ValueError: configuration error: project.authors[0].email must be idn-email`.
- **Zero Runtime Dependency Packaging Invariant:** Release distributions bundle pure Python modules into `git-sync` wheels and source distributions with standard `setuptools.build_meta`. Runtime code must never import non-standard library packages. Automated AST audits in `tests/test_packaging.py` verify that all runtime imports reside strictly within `sys.stdlib_module_names` or internal `git_sync` modules.
- **Stale Index Lock Recovery & Active Preservation:** Git lock files (`.git/index.lock`, `FETCH_HEAD.lock`, `shallow.lock`) left behind by interrupted or crashed processes cause subsequent Git mutations to fail with `INDEX_LOCKED`. Locks younger than 300 seconds are preserved to avoid race conditions with concurrently executing commands unless `--force` is explicitly passed; locks older than 300 seconds are safely removed during `git-sync unlock` or when `pull --recover-locks` is specified.
- **Dynamic Worker Auto-Tuning & Latency Threshold Telemetry:** Worker concurrency is dynamically auto-tuned via `AdaptiveConcurrencyController` based on host core count (`os.cpu_count()`) and operation type (network fetch vs disk status). To prevent I/O thrashing and socket exhaustion, concurrency is clamped to repository count and degrades gracefully when disk contention (`avg_disk > 0.20s`) or network saturation (`avg_net > 2.0s`) thresholds are breached. Benchmark output (`--benchmark` / `--telemetry`) surfaces actionable structured alerts (`DISK_CONTENTION`, `NETWORK_SATURATION`, `WORKER_SATURATION`) to guide human developers and AI agents.
- **Unconfigured (`NO_REMOTE`) Repositories vs Runtime Errors:** Local repositories without a configured remote (`NO_REMOTE`) represent valid unlinked or scratch git repos, not operational execution errors. In JSON summaries, unconfigured repositories are segregated into `"no_remote": []`, reserving `"errors": []` exclusively for actionable execution failures (`FETCH_ERROR`, timeouts, authentication rejections). Furthermore, local branch identification and working tree dirty checks MUST be evaluated before declaring `NO_REMOTE` to guarantee complete local visibility and prevent uncommitted changes from going undetected.
- **SSH Multiplexing (ControlMaster) & Socket Directory Validation:** When configuring SSH connection reuse via `ControlMaster auto` and `ControlPath`, OpenSSH does not automatically create parent directories for socket paths (e.g. `~/.ssh/sockets/`). If the parent directory does not exist, all SSH operations fail immediately with `ControlPath: no such file or directory`. The `git-sync doctor --ssh` diagnostic validates directory existence and benchmarks handshake overhead with and without multiplexing using isolated temporary sockets, terminating spawned master processes via `ssh -O exit` to prevent orphaned sockets.
- **GitHub CLI (`gh`) Integration & Transient Offline State Segregation:** In environments using GitHub CLI and HTTPS remotes, credential validity is governed by `gh auth status` rather than SSH keys. The `doctor` command inspects `gh` installation, login state, and token scopes. During status checks, transient network drops (`NETWORK_DNS_FAILURE`, `NETWORK_TIMEOUT`, `NETWORK_UNREACHABLE`) are differentiated from permanently unconfigured remotes (`NO_REMOTE`) and missing tracking branches (`NO_UPSTREAM`), attaching structured actionable remediation commands (e.g. `git-sync status --no-fetch`, `gh auth login`, `git remote add`) in both table and JSON outputs.

---

## Deliberate Technical Debt

| Debt | Rationale | Revisit When |
|---|---|---|
| Zero external dependencies | Avoid virtualenv management and maximize portability across profiles | If visual UI/TUI is requested requiring `curses` or external UI libs |
