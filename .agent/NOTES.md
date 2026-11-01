# NOTES.md — Active Contracts and Runtime Memory

> Stores the active runtime contracts (WHAT interacts with WHAT), CLI interface schemas, and critical gotchas.
> Historical architectural decisions live in [`docs/adr/decisions.md`](../docs/adr/decisions.md).

---

## Active Contracts

- **Pre-Approved Tasks (`APPROVED`), Parallel Waves (`[PARALLEL: Wx]`) & `task-queue` Skill:** Tasks finalized during interactive planning use `Status: APPROVED` in `.agent/TASK.md` (with independent disjoint-file tasks tagged `[PARALLEL: Wx]` + `Paths:`), allowing fresh sessions (`proceed`) or isolated worker subagents and multi-account quota failover (`task-queue` skill on `proceed queue`) to transition directly to `RUNNING` without a redundant `PLANNING` pause, halting only when ambiguities, unapproved dependencies, or blockers require human intervention.


### 1. CLI Commands & Invocation Protocols

`git-sync` exposes eleven core operations via subcommands:

| Subcommand | Flag Options | Primary Output | Use Case |
|---|---|---|---|
| `git-sync status` | `--fetch`/`--no-fetch`, `--pending`, `--compact`/`--summary`, `--changelog`, `--submodules`, `--dashboard`/`--tui`, `-i`/`--interactive`, `--remote <name>`, `--no-cache`, `--benchmark`, `--preflight`, `--retries <n>`, `--group <dir>`, `--json` | Table / Dashboard / JSON | Inspection of sync states across all repos in root |
| `git-sync dashboard` / `tui` | `--fetch`/`--no-fetch`, `--pending`, `--changelog`, `--group <dir>` | Interactive Dashboard | High-density terminal dashboard with single-key action loop (`Enter`, `p`, `w`/`Tab`, `r`, `c`, `d`, `j`/`k`, `q`) |
| `git-sync doctor` | `--ssh`, `--prune-ssh`, `--group <dir>`, `--json` | Diagnostics table / JSON | Proactive credential sanity check (SSH agent, keys, credential helper, SSH multiplexing, stale socket pruning, remote reachability, locked repos) |
| `git-sync pull` | `[repos...]`, `--all`, `--dry-run`/`--no-dry-run`, `--ff-only`/`--no-ff-only`, `--autostash`/`--no-autostash`, `--sync-branches`/`--no-sync-branches`, `--submodules`/`--no-submodules`, `--unshallow`, `--strategy <abort\|rebase\|merge>`, `--remote <name>`, `--no-cache`, `--benchmark`, `--preflight`, `--retries <n>`, `--recover-locks`, `-i`/`--interactive`, `--group <dir>`, `--json` | Progress logs / JSON | Fast-forward update of current branch + auxiliary branches with dirty tree autostash, divergence resolution, stale lock recovery & shallow submodule deepening |
| `git-sync resolve` | `[repos...]`, `--all`, `--dry-run`/`--no-dry-run`, `--strategy <abort\|rebase\|merge\|stash>`, `--diff`, `--max-tokens <n>`, `-i`/`--interactive`, `--group <dir>`, `--fetch`/`--no-fetch`, `--compact`/`--summary`, `--json` | Interactive prompt / JSON | Guided diagnostics, diff inspection, and resolution for diverged branches |
| `git-sync push` | `[repos...]`, `--all`, `--dry-run`/`--no-dry-run`, `--branch <name>`, `--remote <name>`, `--force`, `--block-protected`, `--allow-protected`, `--protected-branches <patterns>`, `--no-cache`, `--benchmark`, `--preflight`, `--retries <n>`, `--group <dir>`, `--json` | Progress logs / JSON | Push committed branches to upstream remote targets with proactive pre-flight telemetry and protected branch force-push defense |
| `git-sync dirty` | `--group <dir>`, `--json` | List of modified files | Quick audit of uncommitted work |
| `git-sync unlock` | `[repos...]`, `--all`, `--force`, `--group <dir>`, `--json` | Progress logs / JSON | Safely inspect and recover active or stale Git lock files (.git/*.lock) |
| `git-sync config` | `--init`, `--edit`, `--global`, `--json` | Table / JSON / Interactive Editor | Inspect resolved configuration, trace provenance sources, or bootstrap/edit templates |
| `git-sync install` | `--target-dir <dir>`, `--force`, `--uninstall`, `--json` | Progress logs / JSON | Safe idempotent global deployment of launcher symlink to `~/.local/bin/git-sync` |
| `git-sync completion` | `[bash\|zsh]`, `--install`, `--target-dir <dir>`, `--json` | Script / JSON | Generate or install shell autocompletion definitions with dynamic repo lookup |
| `git-sync worktree` | `prune`/`list`, `[repos...]`, `--all`, `--expire <time>`, `--group <dir>`, `--dry-run`, `--json` | Table / Progress logs / JSON | Inspect linked worktree topology, clean missing worktree metadata/orphaned admin dirs, and warn on dirty linked worktrees |

Global options (usable before or after subcommands):
`-v`, `--version`, `--config <path>`, `--profile <name>`, `--root <dir>`, `--workers <n|auto>`, `--auto-tune` / `--adaptive`, `--timeout <sec>`, `--retries <n>`, `--preflight`, `--exclude <patterns...>`, `--compact`, `--summary`, `--changelog`, `--submodules` / `--no-submodules`, `--unshallow`, `--max-tokens <n>`, `--benchmark`, `--telemetry`, `--no-cache`, `--remote <name>`, `--dry-run`, `--no-dry-run`, `--json`.

### 2. Agent JSON Schema (`--json`)

When invoked with `--json`, `git-sync status` provides two modes:

#### 2.1 Compact Mode (`--compact` or `--summary`) — Recommended for AI Agents (~90% token reduction)
Omit the heavy `items` list and output only high-level operational triage arrays:

```json
{
  "root": "/home/user/github",
  "total": 30,
  "pull_pending": ["org/template-agent"],
  "push_pending": ["org/git-sync"],
  "dirty": ["org/service-app"],
  "diverged": [],
  "worktrees": [],
  "stale_worktrees": [],
  "has_submodules": [],
  "submodule_drift": [],
  "shallow_submodules": [],
  "no_remote": [],
  "no_upstream": [],
  "offline": [],
  "errors": [],
  "remediations": [
    {
      "repo": "org/scratch-repo",
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
  "stale_worktrees": [],
  "has_submodules": [],
  "submodule_drift": [],
  "shallow_submodules": [],
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

#### 2.4 Token-Budget Aware Diff Inspection Schema (`resolve --diff`)
When inspecting divergent branch diffs with `git-sync resolve --diff` (either directly or with `--json`), token budgets can be enforced via `--max-tokens <n>` (defaulting to 1000 tokens / ~4000 chars when `--compact` or `--summary` is active). The deterministic chunker (`truncate_diff`) preserves complete file boundaries (`diff --git `) up to the budget, partially truncates oversized single-file diffs at line boundaries, and appends an explicit truncation marker:

```
[... truncated X more file(s), ~Y chars ...]
```

#### 2.5 Conventional Commit Classifier & Changelog Schema (`status --changelog`)
When inspecting pending repositories with `git-sync status --changelog`, pending commits (`HEAD..@{u}` incoming and `@{u}..HEAD` outgoing) are parsed using `%h%x00%s%x00%b%x1f` (unit-separator delimited) and classified into Keep-a-Changelog buckets (`Breaking`, `Security`, `Added`, `Fixed`, `Changed`, `Removed`) without external dependencies or LLM overhead. Breaking changes are flagged deterministically via `!` prefix (`feat!:`, `fix(auth)!:`) or `BREAKING[- ]CHANGE:` footers.

In compact JSON mode (`--compact` / `--summary`):
```json
{
  "changelogs": {
    "org/repo": {
      "incoming_categories": {"Added": 2, "Fixed": 1},
      "outgoing_categories": {"Changed": 1},
      "incoming_breaking": 0,
      "outgoing_breaking": 0,
      "has_breaking": false
    }
  }
}
```

In detailed mode (`items[...]`):
```json
{
  "changelog": {
    "incoming": [
      {
        "hash": "a1b2c3d",
        "subject": "feat(api): add v2 endpoint",
        "body": "",
        "type": "feat",
        "scope": "api",
        "description": "add v2 endpoint",
        "is_breaking": false,
        "category": "Added",
        "breaking_note": null
      }
    ],
    "outgoing": [],
    "incoming_categories": {"Added": 1},
    "outgoing_categories": {},
    "incoming_breaking": 0,
    "outgoing_breaking": 0,
    "has_breaking": false
  }
}
```

### 3. Workspace Profiles, Hierarchical Config Merging, and `.git-sync.json` Schema

Configuration is deep-merged across seven precedence layers (`defaults < XDG config < ~/.git-sync.json < ./.git-sync.json < env < profile < CLI`).
Override provenance is tracked in `GitSyncConfig.sources: List[str]` in ascending order of precedence.

Configuration layers:
1. Built-in defaults (`defaults`)
2. XDG Base Directory specification (`$XDG_CONFIG_HOME/git-sync/config.json` or `~/.config/git-sync/config.json`)
3. User home directory (`~/.git-sync.json`)
4. Workspace directory (`./.git-sync.json` or custom `--config <path>` / `GIT_SYNC_CONFIG`)
5. Environment variables (`GIT_SYNC_PROFILE`, `GIT_SYNC_ROOT`, `GIT_SYNC_WORKERS`, `GIT_SYNC_TIMEOUT`, `GIT_SYNC_AUTO_TUNE`, `GIT_SYNC_REMOTE`, `GIT_SYNC_RETRIES`, `GIT_SYNC_PREFLIGHT`, `GIT_SYNC_BENCHMARK`)
6. Workspace profile (`--profile <name>` or `default_profile`)
7. Explicit CLI arguments (`CLI`)

```json
{
  "root": "/workspace/repos",
  "workers": 12,
  "timeout": 20,
  "exclude": ["legacy", "third-party/*"],
  "groups": ["dev", "sandbox"],
  "sources": [
    "defaults",
    "/home/user/.config/git-sync/config.json",
    "/workspace/repos/.git-sync.json",
    "profile:work",
    "cli"
  ],
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

#### Interactive Config Bootstrap (`--init` / `--edit`)
- `git-sync config --init`: Creates default configuration template at `./.git-sync.json` (or `--global` for XDG config `~/.config/git-sync/config.json`) if missing without clobbering existing configuration.
- `git-sync config --edit`: If run on interactive TTY, opens the configuration file in `$VISUAL` or `$EDITOR` (fallback `nano` / `vi`). If run in non-TTY or piped mode, outputs the resolved configuration file path directly. Automatically bootstraps template if missing.

---

## Gotchas & Pitfalls

- **Git Credential Prompts:** Running `git fetch` or `git pull` in non-interactive background subshells will hang indefinitely if credentials or SSH passphrases are requested. **Mitigation:** Always set `GIT_TERMINAL_PROMPT=0` and pass execution timeouts to `subprocess.run`.
- **Directory Walking:** Never filter out `.git` before inspecting `dirnames` in `os.walk`; filtering `.git` prematurely prevents identifying the current directory as a Git repository.
- **Ephemeral Tooling and Multi-Agent Worktrees:** Multi-agent orchestrators create secondary linked worktrees under `.multigravity/worktrees/` or hidden tool directories. These worktrees lack standalone remotes and cause phantom `NO_REMOTE` errors if traversed by `discover_repos`. Tooling directories starting with `.` (e.g. `.multigravity`, `.git-sync`, `.agent`, `.claude`) MUST be pruned from discovery traversal.
- **Autostash and Dirty Tree Preservation:** When autostashing uncommitted changes during `git-sync pull`, staged and untracked files must be preserved using `git stash push -u -m "<tag>"`. If an unstash (`git stash pop`) encounters a merge conflict, the stash MUST NOT be discarded; the conflict must be isolated and flagged to the agent with instructions without data loss.
- **Fast-Forward Auxiliary Branches:** To update local branches that track remote branches without checking them out into the working tree, use `git fetch . origin/<branch>:<branch>`. This guarantees atomic fast-forward without disturbing the active worktree.
- **Worktree Active Branch Fetch Refusal & Conflict Isolation:** `git fetch . origin/<branch>:<branch>` fails with exit code 128 if `<branch>` is checked out in an active secondary worktree. The active branch is isolated from auxiliary fetch, captured in `worktree_isolated`, and reported per-target worktree path (`[worktree]`) so that each secondary worktree target updates independently.
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
- **Fast Completion Dynamic Lookup & Native `git sync` Subcommand Dispatch:** Shell completions for Bash (`_git_sync` + `complete -F _git_sync_completions git-sync`) and Zsh (`#compdef git-sync git-sync=git` + `_git-sync`) support both standalone `git-sync <TAB>` and Git's native subcommand dispatcher `git sync <TAB>`. Dynamic repository completion invokes `git-sync --complete-repos`, an internal fast hook that executes workspace repository discovery and returns relative paths instantly (<60ms) without initiating remote network operations or full inspections.
- **PEP 621 Author Email Validation in setuptools:** Modern setuptools (`build_meta`) strictly enforces PEP 621 / RFC 5322 email syntax with valid FQDN domain (`user@domain.tld`). Using local addresses like `user@local` raises `ValueError: configuration error: project.authors[0].email must be idn-email`.
- **Zero Runtime Dependency Packaging Invariant:** Release distributions bundle pure Python modules into `git-sync` wheels and source distributions with standard `setuptools.build_meta`. Runtime code must never import non-standard library packages. Automated AST audits in `tests/test_packaging.py` verify that all runtime imports reside strictly within `sys.stdlib_module_names` or internal `git_sync` modules.
- **Stale Index Lock Recovery & Active Preservation:** Git lock files (`.git/index.lock`, `FETCH_HEAD.lock`, `shallow.lock`) left behind by interrupted or crashed processes cause subsequent Git mutations to fail with `INDEX_LOCKED`. Locks younger than 300 seconds are preserved to avoid race conditions with concurrently executing commands unless `--force` is explicitly passed; locks older than 300 seconds are safely removed during `git-sync unlock` or when `pull --recover-locks` is specified.
- **Dynamic Worker Auto-Tuning & Latency Threshold Telemetry:** Worker concurrency is dynamically auto-tuned via `AdaptiveConcurrencyController` based on host core count (`os.cpu_count()`) and operation type (network fetch vs disk status). To prevent I/O thrashing and socket exhaustion, concurrency is clamped to repository count and degrades gracefully when disk contention (`avg_disk > 0.20s`) or network saturation (`avg_net > 2.0s`) thresholds are breached. Benchmark output (`--benchmark` / `--telemetry`) surfaces actionable structured alerts (`DISK_CONTENTION`, `NETWORK_SATURATION`, `WORKER_SATURATION`) to guide human developers and AI agents.
- **Unconfigured (`NO_REMOTE`) Repositories vs Runtime Errors:** Local repositories without a configured remote (`NO_REMOTE`) represent valid unlinked or scratch git repos, not operational execution errors. In JSON summaries, unconfigured repositories are segregated into `"no_remote": []`, reserving `"errors": []` exclusively for actionable execution failures (`FETCH_ERROR`, timeouts, authentication rejections). Furthermore, local branch identification and working tree dirty checks MUST be evaluated before declaring `NO_REMOTE` to guarantee complete local visibility and prevent uncommitted changes from going undetected.
- **SSH Multiplexing (ControlMaster) & Socket Directory Validation:** When configuring SSH connection reuse via `ControlMaster auto` and `ControlPath`, OpenSSH does not automatically create parent directories for socket paths (e.g. `~/.ssh/sockets/`). If the parent directory does not exist, all SSH operations fail immediately with `ControlPath: no such file or directory`. The `git-sync doctor --ssh` diagnostic validates directory existence and benchmarks handshake overhead with and without multiplexing using isolated temporary sockets, terminating spawned master processes via `ssh -O exit` to prevent orphaned sockets.
- **GitHub CLI (`gh`) Integration & Transient Offline State Segregation:** In environments using GitHub CLI and HTTPS remotes, credential validity is governed by `gh auth status` rather than SSH keys. The `doctor` command inspects `gh` installation, login state, and token scopes. During status checks, transient network drops (`NETWORK_DNS_FAILURE`, `NETWORK_TIMEOUT`, `NETWORK_UNREACHABLE`) are differentiated from permanently unconfigured remotes (`NO_REMOTE`) and missing tracking branches (`NO_UPSTREAM`), attaching structured actionable remediation commands (e.g. `git-sync status --no-fetch`, `gh auth login`, `git remote add`) in both table and JSON outputs.
- **ANSI `NO_COLOR` Compliance & Interactive Dashboard Raw TTY Handling:** Terminal styling (`AnsiStyler`, `format_diff_stat`) strictly honors the `NO_COLOR` specification (any non-empty `NO_COLOR` disables ANSI codes) and `sys.stdout.isatty()`. The interactive dashboard (`InteractiveDashboard`) uses `termios`/`tty.setraw` on interactive TTYs with `finally` attribute restoration and line-based fallback on non-TTYs, returning exit code `130` on `SIGINT` (`Ctrl+C`) or `EOF` (`Ctrl+D`).
- **Token-Budget Aware Diff Truncation & File-Boundary Preservation:** Naive character slicing of Git diffs splits across patch headers or file hunks, producing broken diffs that fail LLM parsing or syntax highlighting. `truncate_diff` parses unified diffs by chunking on `diff --git ` boundaries. Whole file chunks are preserved up to `max_tokens * 4` characters. If a single file chunk exceeds the budget, it is partially truncated at the closest newline boundary, and an explicit `[... truncated X more file(s), ~Y chars ...]` marker is appended.
- **Deterministic Conventional Commit Classification & Unit Separator Log Framing:** Parsing Git commit logs with standard newlines breaks on multi-line commit bodies or embedded markdown lists. Using ASCII unit separator formatting (`%h%x00%s%x00%b%x1f`) unambiguously delimits commit records (`0x1F`) and field boundaries (`0x00`). Conventional Commit headers are deterministically parsed and mapped into Keep-a-Changelog buckets (`Breaking`, `Security`, `Added`, `Fixed`, `Changed`, `Removed`) with breaking changes flagged via `!` or `BREAKING[- ]CHANGE:` footers without LLM latency or external dependencies.
- **Submodule Drift Detection & Autostash Isolation:** When `git status --porcelain` detects uncommitted submodule commits (` M vendor/sub`), running `git stash push -u` does not stash submodule commits and outputs `No local changes to save`. Autostash handlers must inspect this output to prevent recording phantom stashes that fail during unstash (`git stash pop`). Submodules are updated recursively via `update_submodules` using `git submodule update --init --recursive` with strict `GIT_TERMINAL_PROMPT=0` environment isolation.
- **Shallow Submodule Deepening & Unshallow Fetch Recovery:** Truncated shallow submodule clones (`.git/shallow`) lack full commit history. Executing `git fetch --unshallow` on a complete repository throws fatal error 128 (`fatal: --unshallow on a complete repository does not make sense`). Shallow states must be inspected directly via on-disk inode checks (`get_repo_git_dir(path) / "shallow"`), bypassing subprocess overhead, and safely deepened via `unshallow_submodule` or `git-sync pull --submodules --unshallow` before submodule checkout.
- **Bulk Action Preview & Interactive Confirmation:** Batch mutations (`pull --all`, `push --all`, `worktree prune`) with `--confirm` or `--preview` inspect affected repositories without altering state, building structured `BulkActionItem` records. The preview formats safety indicators (dirty trees requiring autostash, protected branch targets, ahead/behind commits, submodule drift) and requires affirmative user confirmation (`y`, `yes`, `s`, `sim`) or `-y`/`--yes` before executing mutations. In non-interactive `--json` mode without `--yes`, `--confirm` safely errors with code 1 rather than hanging on headless stdin.
- **Credential Helper Chain Reset, Socket Expiry & Keychain Triage:** Git allows resetting inherited `credential.helper` entries using an empty string `""` in `.gitconfig`. `inspect_credential_helpers` evaluates effective helper chains after resets, probes `git-credential-cache` UNIX domain sockets for daemon liveness and TTL expiry (`--timeout`), audits `~/.git-credentials` for POSIX `0o600` file permissions and placeholder/expired tokens without leaking secret values, and detects headless `libsecret` sessions missing `DBUS_SESSION_BUS_ADDRESS`.

---

## Deliberate Technical Debt

| Debt | Rationale | Revisit When |
|---|---|---|
| Zero external dependencies | Avoid virtualenv management and maximize portability across profiles | If visual UI/TUI is requested requiring `curses` or external UI libs |
