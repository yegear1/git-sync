"""
Core business logic and Git automation engine for git-sync.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
import errno
import fnmatch
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from typing import TYPE_CHECKING
from git_sync.engine.concurrency import AdaptiveConcurrencyController

if TYPE_CHECKING:
    from git_sync.telemetry import BulkCache, TelemetryBenchmark

def resolve_default_root() -> str:
    """
    Dynamically resolve default workspace root following precedence:
    1. GIT_SYNC_ROOT environment variable (if non-empty)
    2. Dynamic user home discovery: ~/github, ~/Github, ~/src, ~/projects, ~/repos (if exists as directory)
    3. Fallback: Current working directory (Path.cwd())
    """
    env_root = os.environ.get("GIT_SYNC_ROOT")
    if env_root and env_root.strip():
        return env_root.strip()

    candidate_homes: List[Path] = []
    try:
        candidate_homes.append(Path.home())
    except Exception:
        pass

    try:
        import pwd
        pw_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if pw_home not in candidate_homes:
            candidate_homes.append(pw_home)
    except Exception:
        pass

    user = os.environ.get("USER")
    if user:
        u_home = Path(f"/home/{user}")
        if u_home not in candidate_homes:
            candidate_homes.append(u_home)

    for h in candidate_homes:
        for candidate in ("github", "Github", "src", "projects", "repos"):
            p = h / candidate
            if p.is_dir():
                return str(p)

    try:
        return str(Path.cwd().resolve())
    except Exception:
        return "."


DEFAULT_ROOT = resolve_default_root()
DEFAULT_WORKERS = 12
DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_FACTOR = 0.5
DEFAULT_MAX_BACKOFF = 4.0
DEFAULT_DISK_LATENCY_THRESHOLD = 0.20
DEFAULT_NETWORK_LATENCY_THRESHOLD = 2.0
DEFAULT_MAX_WORKERS = 32
DEFAULT_MIN_WORKERS = 1
from git_sync.engine.diff import (
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_COMPACT_DIFF_TOKENS,
)
DEFAULT_PROTECTED_BRANCHES: List[str] = ["main", "master", "release/*", "production", "prod"]
from git_sync.engine.credentials import (
    GRADE_A_THRESHOLD_SECONDS,
    GRADE_B_THRESHOLD_SECONDS,
    GRADE_C_THRESHOLD_SECONDS,
)

DEFAULT_IGNORE_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".cache",
    ".multigravity",
    ".agent",
    ".claude",
    ".git-sync",
    ".gemini",
    ".idea",
    ".vscode",
}

TOOL_DIR_PREFIXES = (
    ".multigravity",
    ".agent",
    ".claude",
    ".git-sync",
    ".gemini",
)

def get_git_env(extra_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Dynamically build subprocess execution environment inheriting active os.environ while enforcing safety invariants.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 guarantees git never blocks on interactive auth/passphrase prompts.
    # INVARIANT: PAGER=cat prevents paginated output hangs.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat"}
    if extra_env:
        env.update(extra_env)
    return env


# INVARIANT: GIT_TERMINAL_PROMPT=0 guarantees git never blocks on interactive auth/passphrase prompts
# Static mapping retained for backwards compatibility
GIT_ENV = get_git_env()


def _real_run_git(
    repo_path: str,
    args: List[str],
    timeout: int = DEFAULT_TIMEOUT,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", repo_path] + args
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=get_git_env(extra_env=extra_env),
    )


def run_git(
    repo_path: str,
    args: List[str],
    timeout: int = DEFAULT_TIMEOUT,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a git command within a repository directory with safe timeouts and environment."""
    return _run_git(repo_path, args, timeout=timeout, extra_env=extra_env)


def _run_git(
    repo_path: str,
    args: List[str],
    timeout: int = DEFAULT_TIMEOUT,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    core = sys.modules.get("git_sync.core")
    if core is not None:
        target = getattr(core, "run_git", None)
        if target is not None and target is not run_git:
            try:
                return target(repo_path, args, timeout=timeout, extra_env=extra_env)
            except TypeError:
                return target(repo_path, args, timeout=timeout)
    return _real_run_git(repo_path, args, timeout=timeout, extra_env=extra_env)


def _dispatch_fn(name: str, fallback: Any) -> Any:
    if hasattr(fallback, "_mock_return_value") or hasattr(fallback, "_mock_wraps") or hasattr(fallback, "mock_calls"):
        return fallback
    core = sys.modules.get("git_sync.core")
    if core is not None:
        target = getattr(core, name, None)
        if target is not None and target is not fallback:
            return target
    return fallback


def diagnose_git_error(error_text: str, remote_url: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    Analyze Git or SSH error output and produce actionable diagnostic guidance.
    Returns a dict with category, message, suggestion, and action_required, or None if no match.
    # INVARIANT: Actionable diagnostic categories enable automated agent triage and clear human guidance.
    """
    if not error_text or not error_text.strip():
        return None

    txt = error_text.lower()

    # 1. SSH Authentication & Agent errors
    if "permission denied (publickey" in txt:
        return {
            "category": "SSH_PERMISSION_DENIED",
            "message": "SSH public key authentication failed for remote.",
            "suggestion": "Verify your SSH key is added to the remote Git provider (e.g. GitHub Settings -> SSH and GPG keys) or add identity to agent via 'ssh-add ~/.ssh/id_ed25519'.",
            "action_required": "check_ssh_keys",
        }
    if "could not open a connection to your authentication agent" in txt or "error connecting to agent" in txt:
        return {
            "category": "SSH_NO_AGENT",
            "message": "SSH agent is unavailable or connection was refused.",
            "suggestion": "Start SSH agent with 'eval $(ssh-agent -s)' and load your identity with 'ssh-add ~/.ssh/id_ed25519'.",
            "action_required": "start_ssh_agent",
        }
    if "agent refused operation" in txt or "signing failed" in txt:
        return {
            "category": "SSH_AGENT_REFUSED",
            "message": "SSH agent refused signing operation.",
            "suggestion": "Reload your key into ssh-agent with 'ssh-add -D' followed by 'ssh-add ~/.ssh/id_ed25519'.",
            "action_required": "reload_ssh_agent",
        }
    if "host key verification failed" in txt:
        host = ""
        if remote_url and "@" in remote_url and ":" in remote_url:
            host = remote_url.split("@", 1)[1].split(":", 1)[0]
        elif remote_url and "://" in remote_url:
            host = remote_url.split("://", 1)[1].split("/", 1)[0]
        scan_cmd = f"ssh-keyscan -t ed25519,rsa {host or '<hostname>'} >> ~/.ssh/known_hosts"
        return {
            "category": "SSH_HOST_UNVERIFIED",
            "message": "Remote host key is not trusted in ~/.ssh/known_hosts.",
            "suggestion": f"Trust the remote host key by running: '{scan_cmd}'.",
            "action_required": "verify_ssh_host",
        }
    if "control socket" in txt or ("controlpath" in txt and "connection refused" in txt) or "corrupted control socket" in txt:
        return {
            "category": "SSH_CONTROL_SOCKET_ERROR",
            "message": "Defunct or corrupted SSH ControlMaster socket detected.",
            "suggestion": "Run 'git-sync doctor --prune-ssh' to inspect and auto-prune stale SSH control sockets, or clean them from ~/.ssh.",
            "action_required": "prune_ssh_sockets",
        }

    # 2. HTTP/HTTPS Authentication, Keychain & Token Expiry errors
    if "terminal prompts disabled" in txt or "could not read username" in txt or "could not read password" in txt:
        return {
            "category": "HTTP_AUTH_REQUIRED",
            "message": "HTTP authentication prompt was disabled (GIT_TERMINAL_PROMPT=0) and no saved credentials were found.",
            "suggestion": "Configure a git credential helper ('git config --global credential.helper store') or provide a Personal Access Token via GITHUB_TOKEN or credentials store.",
            "action_required": "configure_http_credentials",
        }
    if ("token" in txt and ("expired" in txt or "revoked" in txt)) or "expired token" in txt or "token has expired" in txt:
        return {
            "category": "TOKEN_EXPIRED",
            "message": "Authentication token has expired or been revoked.",
            "suggestion": "Refresh or regenerate your Personal Access Token (PAT) via 'gh auth refresh', 'gh auth login', or update ~/.git-credentials.",
            "action_required": "refresh_expired_token",
        }
    if "keychain" in txt or "libsecret" in txt or "gnome-keyring" in txt or "secret service" in txt:
        return {
            "category": "KEYCHAIN_LOCKED_OR_UNAVAILABLE",
            "message": "OS credential keychain or secret service is locked or unavailable.",
            "suggestion": "Unlock your OS keychain (e.g. 'security unlock-keychain' on macOS or start gnome-keyring/D-Bus on Linux) or switch credential helper.",
            "action_required": "unlock_keychain",
        }
    if "authentication failed" in txt or "invalid username or password" in txt or "401" in txt or "403" in txt:
        return {
            "category": "HTTP_AUTH_FAILED",
            "message": "HTTP authentication failed (401/403 or invalid credentials).",
            "suggestion": "Update your expired Personal Access Token (PAT) or credentials in ~/.git-credentials.",
            "action_required": "update_http_credentials",
        }

    # 3. Network & Connection errors
    if "could not resolve host" in txt:
        return {
            "category": "NETWORK_DNS_FAILURE",
            "message": "Could not resolve remote hostname.",
            "suggestion": "Check internet connectivity and DNS configuration.",
            "action_required": "check_network",
        }
    if "timeout" in txt or "timed out" in txt:
        return {
            "category": "NETWORK_TIMEOUT",
            "message": "Network connection timed out.",
            "suggestion": "Check remote host reachability or firewall/proxy configuration, or increase --timeout.",
            "action_required": "check_network_timeout",
        }
    if "connection refused" in txt or "network is unreachable" in txt or "failed to connect to" in txt:
        return {
            "category": "NETWORK_UNREACHABLE",
            "message": "Network connection refused or unreachable.",
            "suggestion": "Verify remote host address, network route, and firewall settings.",
            "action_required": "check_network",
        }

    # 4. Remote repository existence / permissions
    if "repository not found" in txt or "project not found" in txt:
        return {
            "category": "REMOTE_NOT_FOUND",
            "message": "Remote repository not found or access denied.",
            "suggestion": "Verify remote URL spelling and ensure your account has read/write permissions for this repository.",
            "action_required": "verify_remote_url",
        }

    # 5. Lock file conflicts (e.g. index.lock, FETCH_HEAD.lock, shallow.lock)
    if "index.lock" in txt and ("file exists" in txt or "unable to create" in txt or "another git process" in txt or "locked" in txt):
        return {
            "category": "INDEX_LOCKED",
            "message": "Git index is locked (.git/index.lock), preventing Git operations.",
            "suggestion": "Wait for active Git operations to complete or remove stale '.git/index.lock' if no Git process is running.",
            "action_required": "unlock_index",
        }
    if ("fetch_head.lock" in txt or "shallow.lock" in txt or "head.lock" in txt) and "file exists" in txt:
        return {
            "category": "INDEX_LOCKED",
            "message": "Git reference lock exists, preventing concurrent Git operations.",
            "suggestion": "Wait for active Git operations to complete or remove stale .git/*.lock files.",
            "action_required": "unlock_index",
        }

    # 6. Sparse checkout errors & checkout conflicts
    if "sparse-checkout" in txt or "sparse checkout" in txt:
        return {
            "category": "SPARSE_CHECKOUT_CONFLICT",
            "message": "Sparse-checkout pattern conflict or error in working tree.",
            "suggestion": "Check configured sparse-checkout patterns with 'git sparse-checkout list' or reapply with 'git sparse-checkout reapply --cone'.",
            "action_required": "reapply_sparse_checkout",
        }
    if "would be overwritten by merge" in txt or "would be overwritten by checkout" in txt or "updating the following directories would lose untracked files" in txt:
        return {
            "category": "CHECKOUT_CONFLICT",
            "message": "Untracked or uncommitted local files would be overwritten by pull/checkout.",
            "suggestion": "Move, commit, or stash conflicting untracked files, or run 'git-sync pull --autostash' / 'git sparse-checkout reapply'.",
            "action_required": "resolve_checkout_conflict",
        }

    # 7. Push protection & remote hook rejections
    if "protected branch" in txt or "gh006" in txt or "pre-receive hook declined" in txt or "branch is protected" in txt:
        return {
            "category": "PROTECTED_BRANCH_REJECTED",
            "message": "Remote repository rejected push due to protected branch policies.",
            "suggestion": "Push is restricted by branch protection rules on remote. Submit a pull request instead of pushing directly.",
            "action_required": "create_pull_request",
        }
    if "non-fast-forward" in txt or "fetch first" in txt or "updates were rejected because the remote contains work" in txt:
        return {
            "category": "NON_FAST_FORWARD_REJECTED",
            "message": "Remote rejected push because remote contains commits not present locally.",
            "suggestion": "Run 'git-sync pull' to fetch and fast-forward remote commits before pushing.",
            "action_required": "pull_remote_first",
        }

    return None


def is_transient_network_error(exc: Optional[Exception], stderr: str) -> bool:
    """
    Determine if an error is a transient network issue suitable for retry with exponential backoff.
    # INVARIANT: Permanent authentication failures and invalid repository paths MUST NOT be retried.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return True

    if not stderr:
        return False

    txt = stderr.lower()

    # Never retry permanent authentication or not-found errors
    permanent_patterns = (
        "permission denied (publickey",
        "authentication failed",
        "terminal prompts disabled",
        "could not read username",
        "could not read password",
        "host key verification failed",
        "repository not found",
        "project not found",
        "token expired",
        "token has expired",
        "expired token",
        "expired or revoked",
        "keychain",
        "libsecret",
        "gnome-keyring",
        "secret service",
    )
    if any(p in txt for p in permanent_patterns):
        return False

    transient_patterns = (
        "timed out",
        "timeout",
        "operation timed out",
        "connection timed out",
        "could not resolve host",
        "failed to connect to",
        "the remote end hung up unexpectedly",
        "connection reset by peer",
        "rpc failed; curl 56",
        "rpc failed; curl 28",
        "early eof",
        "fetch-pack: invalid index-pack output",
        "network is unreachable",
        "index-pack failed",
        "protocol error: bad line length",
        "connection closed by remote host",
        "connection refused",
        "interrupted system call",
        "transfer closed with outstanding read data remaining",
        "control socket",
    )
    return any(p in txt for p in transient_patterns)


def run_git_with_retry(
    repo_path: str,
    args: List[str],
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_backoff: float = DEFAULT_MAX_BACKOFF,
    benchmark: Optional[TelemetryBenchmark] = None,
    sleep_func: Any = time.sleep,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    """
    Execute a Git command with safe timeout, environment, and bounded exponential backoff
    for transient network errors.
    # INVARIANT: Retries only transient network errors; auth and fatal repository errors fail fast.
    """
    last_proc: Optional[subprocess.CompletedProcess[str]] = None
    last_timeout_exc: Optional[subprocess.TimeoutExpired] = None

    for attempt in range(retries + 1):
        try:
            proc = run_git(repo_path, args, timeout=timeout, extra_env=extra_env)
            if proc.returncode == 0:
                if attempt > 0 and benchmark:
                    benchmark.record_retry(recovered=True)
                return proc

            last_proc = proc
            if attempt < retries and is_transient_network_error(None, proc.stderr):
                if proc.stderr and (
                    "control socket" in proc.stderr.lower()
                    or ("controlpath" in proc.stderr.lower() and "connection refused" in proc.stderr.lower())
                ):
                    try:
                        _dispatch_fn("prune_stale_ssh_control_sockets", prune_stale_ssh_control_sockets)()
                    except Exception:
                        pass
                if benchmark:
                    benchmark.record_retry(recovered=False)
                delay = min(max_backoff, backoff_factor * (2 ** attempt))
                sleep_func(delay)
                continue
            return proc

        except subprocess.TimeoutExpired as exc:
            last_timeout_exc = exc
            if attempt < retries:
                if benchmark:
                    benchmark.record_retry(recovered=False)
                delay = min(max_backoff, backoff_factor * (2 ** attempt))
                sleep_func(delay)
                continue
            raise exc

    if last_timeout_exc:
        raise last_timeout_exc
    if last_proc is not None:
        return last_proc
    return run_git(repo_path, args, timeout=timeout)


# Re-exported from git_sync.engine.topology
from git_sync.engine.topology import (
    get_repo_git_dir,
    get_sparse_checkout_info,
    is_path_in_cone,
    is_shallow_repository,
    reapply_sparse_checkout,
)



def is_lfs_pointer_file(file_path: str | Path) -> bool:
    """
    Check if a file on disk is an unsmudged Git LFS pointer text file.
    # INVARIANT: Git LFS pointer files begin with standard specification header.
    """
    try:
        p = Path(file_path)
        if not p.is_file() or p.stat().st_size > 1024:
            return False
        with open(p, "rb") as f:
            header = f.read(200)
            return header.startswith(b"version https://git-lfs.github.com/spec/v1")
    except (OSError, UnicodeDecodeError):
        return False


def inspect_git_lfs(timeout: int = 5) -> Dict[str, Any]:
    """
    Probe host environment for git-lfs CLI extension availability and version.
    # INVARIANT: Uses GIT_TERMINAL_PROMPT=0 to guarantee probing never hangs in automated environments.
    """
    try:
        proc = subprocess.run(
            ["git", "lfs", "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=get_git_env(),
        )
        if proc.returncode == 0:
            version_line = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
            return {
                "installed": True,
                "version": version_line,
                "ok": True,
            }
    except Exception:
        pass
    return {
        "installed": False,
        "version": None,
        "ok": False,
    }


def format_bytes_human(num_bytes: int) -> str:
    """Format byte counts into human-readable strings (B, KB, MB, GB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def get_lfs_info(repo_path: str, timeout: int = 5) -> Dict[str, Any]:
    """
    Inspect Git LFS tracking configuration, tracked patterns, storage size, and pointer objects.
    # INVARIANT: Zero external dependencies; fast on-disk inspection before optional CLI queries.
    """
    repo = Path(repo_path)
    git_dir = get_repo_git_dir(repo_path)

    # 1. Parse .gitattributes for filter=lfs patterns
    gitattributes_file = repo / ".gitattributes"
    patterns: List[str] = []
    if gitattributes_file.is_file():
        try:
            content = gitattributes_file.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "filter=lfs" in line:
                    tokens = line.split()
                    if tokens:
                        pattern = tokens[0]
                        if pattern not in patterns:
                            patterns.append(pattern)
        except Exception:
            pass

    # 2. Check on-disk .git/lfs directory and calculate storage size
    lfs_dir = Path(git_dir) / "lfs"
    objects_dir = lfs_dir / "objects"
    storage_bytes = 0
    objects_count = 0
    if objects_dir.is_dir():
        try:
            for root, _, files in os.walk(objects_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        storage_bytes += os.path.getsize(fp)
                        objects_count += 1
                    except OSError:
                        pass
        except Exception:
            pass

    lfs_env = _dispatch_fn("inspect_git_lfs", inspect_git_lfs)(timeout=timeout)
    installed = bool(lfs_env.get("installed", False))

    has_lfs = bool(patterns or objects_dir.is_dir())
    if not has_lfs:
        return {
            "has_lfs": False,
            "enabled": False,
            "installed": installed,
            "patterns": [],
            "pattern_count": 0,
            "files": [],
            "files_count": 0,
            "pointers_count": 0,
            "storage_bytes": 0,
            "storage_human": "0 B",
            "missing_objects": 0,
            "objects_count": 0,
            "hygiene": "NO_LFS",
            "warning": None,
        }

    # 3. Query git lfs ls-files if available, or fall back to scanning files matching patterns
    files_tracked: List[str] = []
    pointers_count = 0
    proc = run_git(repo_path, ["lfs", "ls-files"], timeout=timeout)
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=2)
            if len(parts) >= 3:
                _oid, marker, f_path = parts[0], parts[1], parts[2]
                files_tracked.append(f_path)
                if marker == "-":
                    pointers_count += 1
            elif len(parts) == 2:
                files_tracked.append(parts[1])
            elif len(parts) == 1:
                files_tracked.append(parts[0])
    else:
        # Fallback to scanning repo for pointer files matching tracked patterns or known pointer header
        for root, dirs, f_names in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d != ".git" and not d.startswith(".")]
            for f_name in f_names:
                full_p = os.path.join(root, f_name)
                rel_p = os.path.relpath(full_p, repo_path)
                is_match = any(fnmatch.fnmatch(f_name, pat) or fnmatch.fnmatch(rel_p, pat) for pat in patterns)
                is_ptr = is_lfs_pointer_file(full_p)
                if is_match or is_ptr:
                    if rel_p not in files_tracked:
                        files_tracked.append(rel_p)
                    if is_ptr:
                        pointers_count += 1

    if not installed:
        hygiene = "LFS_NOT_INSTALLED"
        warning = "Repository configures Git LFS attributes but 'git-lfs' is not installed; files remain raw pointers"
    elif pointers_count > 0:
        hygiene = "POINTERS_UNSMUDGED"
        warning = f"{pointers_count} unsmudged Git LFS pointer(s) detected; local objects missing"
    else:
        hygiene = "HEALTHY"
        warning = None

    return {
        "has_lfs": True,
        "enabled": True,
        "installed": installed,
        "patterns": patterns,
        "pattern_count": len(patterns),
        "files": files_tracked,
        "files_count": len(files_tracked),
        "pointers_count": pointers_count,
        "storage_bytes": storage_bytes,
        "storage_human": format_bytes_human(storage_bytes),
        "missing_objects": pointers_count,
        "objects_count": objects_count,
        "hygiene": hygiene,
        "warning": warning,
    }


def pull_lfs_objects(
    repo_path: str,
    remote: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Explicitly pull Git LFS objects for the current checkout via `git lfs pull`.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 guarantees authentication failures never block the process.
    # INVARIANT: When dry_run is True, no LFS object transfer occurs.
    """
    if dry_run:
        return {
            "success": True,
            "stdout": "",
            "stderr": "",
            "dry_run": True,
            "would_pull": True,
        }
    cmd = ["lfs", "pull"]
    if remote:
        cmd.append(remote)
    proc = run_git(repo_path, cmd, timeout=timeout)
    return {
        "success": proc.returncode == 0,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "dry_run": False,
        "would_pull": False,
    }


# Re-exported from git_sync.engine.topology
from git_sync.engine.topology import (
    find_locked_repos,
    find_repo_locks,
    recover_index_lock,
    recover_repo_locks,
)



# Re-exported from git_sync.engine.ssh
from git_sync.engine.ssh import (
    _parse_ssh_config_content,
    audit_ssh_multiplexing,
    benchmark_ssh_multiplexing,
    close_ssh_control_socket,
    find_ssh_control_sockets,
    inspect_ssh_config,
    inspect_ssh_control_socket,
    prune_stale_ssh_control_sockets,
)


# Re-exported from git_sync.engine.credentials
from git_sync.engine.credentials import (
    _resolve_credential_helper_binary,
    check_credentials_sanity,
    compute_latency_grade,
    inspect_credential_helpers,
    inspect_github_cli,
    inspect_token_env,
    probe_remote_reachability,
)


def audit_multi_remote_matrix(
    root_dir: str,
    repos: Optional[List[str]] = None,
    group: Optional[str] = None,
    timeout: int = 5,
    workers: int | str = DEFAULT_WORKERS,
    max_remotes_to_test: int = 30,
) -> Dict[str, Any]:
    """
    Perform parallel multi-remote reachability probing and construct a reachability matrix across all configured remotes.
    # INVARIANT: Probes are executed in parallel across unique remote endpoints with GIT_TERMINAL_PROMPT=0.
    """
    eff_workers = max(1, min(32, workers if isinstance(workers, int) else 8))
    target_repos = repos
    if target_repos is None:
        discover_fn = _dispatch_fn("discover_repos", discover_repos)
        target_repos = discover_fn(root_dir)
        if group:
            target_repos = [r for r in target_repos if os.path.relpath(r, root_dir).startswith(group)]

    # 1. Map each repo to its configured remotes
    repo_remotes_map: Dict[str, Dict[str, str]] = {}
    unique_remotes_to_sample: Dict[str, str] = {}  # url -> sample_repo

    get_remotes_fn = _dispatch_fn("get_repo_remotes", get_repo_remotes)
    for r_path in target_repos:
        r_map = get_remotes_fn(r_path, timeout=timeout)
        if r_map:
            repo_remotes_map[r_path] = r_map
            for _, r_url in r_map.items():
                if r_url and r_url not in unique_remotes_to_sample:
                    if len(unique_remotes_to_sample) < max_remotes_to_test:
                        unique_remotes_to_sample[r_url] = r_path

    # 2. Probe unique remotes concurrently
    probe_fn = _dispatch_fn("probe_remote_reachability", probe_remote_reachability)

    def _probe_worker(item: Tuple[str, str]) -> Tuple[str, Dict[str, Any]]:
        url, sample_repo = item
        res = probe_fn(remote_url=url, sample_repo=sample_repo, timeout=timeout)
        return url, res

    items_to_probe = list(unique_remotes_to_sample.items())
    probed_results: Dict[str, Dict[str, Any]] = {}
    if items_to_probe:
        eff_probe_workers = min(len(items_to_probe), eff_workers)
        if eff_probe_workers > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=eff_probe_workers) as executor:
                for u, res in executor.map(_probe_worker, items_to_probe):
                    probed_results[u] = res
        else:
            for u, sample_repo in items_to_probe:
                probed_results[u] = probe_fn(remote_url=u, sample_repo=sample_repo, timeout=timeout)

    # 3. Build repository matrix entries
    matrix_entries: List[Dict[str, Any]] = []
    grade_distribution: Dict[str, int] = {"A": 0, "B": 0, "C": 0, "F": 0}
    total_endpoints_tested = 0
    healthy_endpoints = 0
    unreachable_endpoints = 0

    for r_path in target_repos:
        rel_p = os.path.relpath(r_path, root_dir) if root_dir else os.path.basename(r_path)
        r_map = repo_remotes_map.get(r_path, {})
        remotes_detail: Dict[str, Dict[str, Any]] = {}
        repo_grades: Dict[str, str] = {}
        has_unreachable = False

        for r_name, r_url in r_map.items():
            r_info = probed_results.get(r_url)
            if r_info is None:
                is_ssh = "@" in r_url or r_url.startswith("ssh://")
                r_info = {
                    "ok": True,
                    "protocol": "ssh" if is_ssh else "https",
                    "host": "",
                    "latency_seconds": None,
                    "grade": "B",
                    "error": None,
                    "diagnostic": None,
                    "unprobed": True,
                }
            else:
                total_endpoints_tested += 1
                g = r_info.get("grade", "F")
                grade_distribution[g] = grade_distribution.get(g, 0) + 1
                if r_info.get("ok"):
                    healthy_endpoints += 1
                else:
                    unreachable_endpoints += 1
                    has_unreachable = True

            remotes_detail[r_name] = {
                "name": r_name,
                "url": r_url,
                "protocol": r_info.get("protocol", "https"),
                "host": r_info.get("host", ""),
                "ok": r_info.get("ok", False),
                "latency_seconds": r_info.get("latency_seconds"),
                "grade": r_info.get("grade", "F"),
                "error": r_info.get("error"),
                "diagnostic": r_info.get("diagnostic"),
            }
            repo_grades[r_name] = r_info.get("grade", "F")

        matrix_entries.append({
            "repo": rel_p,
            "path": r_path,
            "multi_remote": len(r_map) > 1,
            "remotes_count": len(r_map),
            "remotes": remotes_detail,
            "grades": repo_grades,
            "has_unreachable": has_unreachable,
            "primary_grade": repo_grades.get("origin") or (list(repo_grades.values())[0] if repo_grades else "N/A"),
        })

    multi_remote_count = sum(1 for m in matrix_entries if m["multi_remote"])
    return {
        "total_repos_audited": len(target_repos),
        "multi_remote_repos": multi_remote_count,
        "total_unique_remotes_tested": len(probed_results),
        "total_endpoints_tested": total_endpoints_tested,
        "healthy_endpoints": healthy_endpoints,
        "unreachable_endpoints": unreachable_endpoints,
        "grade_distribution": grade_distribution,
        "matrix": matrix_entries,
        "probed_remotes": probed_results,
    }


# Re-exported from git_sync.engine.topology
from git_sync.engine.topology import (
    audit_worktree_health_batch,
    get_worktree_info,
    prune_repo_worktrees,
)





def get_repo_remotes(repo_path: str, timeout: int = 5) -> Dict[str, str]:
    """
    Query all configured Git remotes and their fetch URLs for repo_path.
    Returns a mapping of remote_name -> fetch_url.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 prevents interactive prompts.
    """
    proc = run_git(repo_path, ["remote", "-v"], timeout=timeout)
    if proc.returncode != 0:
        return {}
    remotes: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "(fetch)" in line:
            parts = line.split()
            if len(parts) >= 2:
                remotes[parts[0]] = parts[1]
    if not remotes:
        for line in proc.stdout.splitlines():
            line = line.strip()
            if "(push)" in line:
                parts = line.split()
                if len(parts) >= 2 and parts[0] not in remotes:
                    remotes[parts[0]] = parts[1]
    return remotes




# Re-exported from git_sync.engine.topology
from git_sync.engine.topology import (
    _real_unshallow_submodule,
    _real_update_submodules,
    get_submodules_status,
    unshallow_submodule,
    update_submodules,
)



def discover_repos(root_dir: str, exclude: Optional[List[str]] = None) -> List[str]:
    """
    Recursively discover all git repositories under root_dir, including secondary linked worktrees.
    Submodules are deliberately excluded to prevent duplicate audit operations.
    Directories matching default ignore patterns or custom exclusion patterns are pruned.
    # INVARIANT: tool directories like .multigravity, .agent, .claude, .git-sync are pruned to prevent ghost worktrees
    # INVARIANT: dirnames must not strip '.git' before checking its presence, otherwise no repos are found.
    # INVARIANT: excluded directory trees are pruned early to prevent traversal overhead into large vendor/archive trees.
    """
    repos: List[str] = []
    root = Path(root_dir).resolve()
    if not root.is_dir():
        return []

    exclude_patterns = [p.strip() for p in (exclude or []) if p and p.strip()]

    def _is_pruned_dir(dname: str) -> bool:
        if dname in DEFAULT_IGNORE_DIRS:
            return True
        for prefix in TOOL_DIR_PREFIXES:
            if dname == prefix or dname.startswith(prefix + "-") or dname.startswith(prefix + "_") or dname.startswith(prefix + "/"):
                return True
        return False

    def _is_excluded(path_str: str) -> bool:
        if not exclude_patterns:
            return False
        try:
            rel = os.path.relpath(path_str, root)
        except ValueError:
            rel = path_str
        norm_rel = rel.replace("\\", "/")
        bname = os.path.basename(path_str)
        for pat in exclude_patterns:
            norm_pat = pat.replace("\\", "/").rstrip("/")
            if norm_rel == norm_pat or norm_rel.startswith(norm_pat + "/"):
                return True
            if bname == norm_pat:
                return True
            if fnmatch.fnmatch(norm_rel, norm_pat) or fnmatch.fnmatch(bname, norm_pat):
                return True
        return False

    for dirpath, dirnames, filenames in os.walk(root):
        if _is_excluded(dirpath):
            dirnames.clear()
            continue

        dirnames[:] = [
            d for d in dirnames
            if not _is_pruned_dir(d) and not _is_excluded(os.path.join(dirpath, d))
        ]

        is_repo = False
        if ".git" in dirnames:
            is_repo = True
            # Remove .git so os.walk does not traverse inside git metadata
            dirnames.remove(".git")
        elif ".git" in filenames:
            # Check if this is a linked worktree or a submodule
            git_file = os.path.join(dirpath, ".git")
            try:
                with open(git_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
                norm_content = content.replace("\\", "/")
                # INVARIANT: Linked worktrees point to $GIT_DIR/worktrees/<name>; submodules point to /modules/<name>
                if norm_content.startswith("gitdir:") and "/worktrees/" in norm_content:
                    is_repo = True
            except OSError:
                pass

        if is_repo:
            repos.append(dirpath)

    return sorted(repos)


def match_repo(query: str, all_repos: List[str], root_dir: str) -> Optional[str]:
    """Match a user/agent query string against discovered repositories."""
    query_norm = query.strip().rstrip("/")
    if not query_norm:
        return None

    # 1. Exact path match
    for r in all_repos:
        if r == query_norm or os.path.abspath(r) == os.path.abspath(query_norm):
            return r

    # 2. Relative path match
    for r in all_repos:
        rel = os.path.relpath(r, root_dir)
        if rel == query_norm or rel.lower() == query_norm.lower():
            return r

    # 3. Basename match
    for r in all_repos:
        if os.path.basename(r) == query_norm or os.path.basename(r).lower() == query_norm.lower():
            return r

    # 4. Unique substring match
    matches = [r for r in all_repos if query_norm.lower() in r.lower()]
    if len(matches) == 1:
        return matches[0]

    return None


def check_repo_dirty(
    repo_path: str,
    worktrees: Optional[List[Dict[str, Any]]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    cache: Optional[BulkCache] = None,
) -> Tuple[bool, List[str]]:
    """
    Check if a repository contains uncommitted or untracked changes.
    # INVARIANT: Filter out linked worktree directories located inside repo_path to avoid false dirty flags.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 prevents interactive prompts during status inspection.
    """
    p_status = run_git(repo_path, ["status", "--porcelain"], timeout=timeout)
    if not p_status.stdout.strip():
        return False, []

    raw_dirty_lines = [line.strip() for line in p_status.stdout.strip().splitlines()]
    if worktrees is None:
        wt_info = cache.get_worktree_info(repo_path, timeout=timeout) if cache else get_worktree_info(repo_path, timeout=timeout)
        worktrees = wt_info.get("worktrees", [])

    wt_inside_relpaths: List[str] = []
    repo_abs = os.path.abspath(repo_path)
    for wt in worktrees:
        wt_abs = wt.get("path", "")
        if wt_abs != repo_abs and wt_abs.startswith(repo_abs + os.sep):
            rel = os.path.relpath(wt_abs, repo_abs)
            wt_inside_relpaths.append(rel.rstrip(os.sep))
            wt_inside_relpaths.append(rel.rstrip(os.sep) + "/")

    filtered_dirty_lines: List[str] = []
    for line in raw_dirty_lines:
        is_wt_artifact = False
        entry_path = line[3:].strip() if len(line) > 3 else ""
        if entry_path and line.startswith("?? "):
            for wt_rel in wt_inside_relpaths:
                if entry_path.startswith(wt_rel) or wt_rel.startswith(entry_path.rstrip("/")):
                    is_wt_artifact = True
                    break
        if not is_wt_artifact:
            filtered_dirty_lines.append(line)

    return bool(filtered_dirty_lines), filtered_dirty_lines


def stash_changes(
    repo_path: str,
    message: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Tuple[bool, str, Optional[str]]:
    """
    Safely stash uncommitted changes including staged, unstaged, and untracked files.
    # INVARIANT: git stash push -u preserves untracked files and staged/unstaged separation without data loss.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 prevents interactive prompts.
    """
    tag = message or f"git-sync-autostash-{int(time.time() * 1000)}"
    proc = run_git(repo_path, ["stash", "push", "-u", "-m", tag], timeout=timeout)
    if proc.returncode == 0:
        combined = (proc.stdout or "") + " " + (proc.stderr or "")
        if "No local changes to save" in combined:
            return True, tag, "NO_CHANGES"
        return True, tag, None
    err = proc.stderr.strip() or proc.stdout.strip() or "Failed to stash changes"
    return False, tag, err


def restore_stash(
    repo_path: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Safely restore stashed changes with index preservation and conflict safeguards.
    # INVARIANT: Tries git stash pop --index first to reinstate staged and unstaged index changes.
    # INVARIANT: If index conflict occurs without worktree merge conflict, falls back to git stash pop.
    # INVARIANT: If unmerged conflict occurs, git preserves the stash entry in the stash stack (stash@{0}).
    #            The conflict is isolated and flagged with unmerged conflict files without data loss.
    """
    p_pop_index = run_git(repo_path, ["stash", "pop", "--index"], timeout=timeout)
    if p_pop_index.returncode == 0:
        return {
            "success": True,
            "conflict": False,
            "conflict_files": [],
            "index_restored": True,
            "stash_ref": None,
            "error": None,
        }

    out = (p_pop_index.stdout + "\n" + p_pop_index.stderr).strip()
    if "No stash entries found" in out or "No stash found" in out:
        return {
            "success": True,
            "conflict": False,
            "conflict_files": [],
            "index_restored": False,
            "stash_ref": None,
            "error": None,
        }

    # Check if there are conflict files in working copy
    p_status = run_git(repo_path, ["status", "--porcelain"], timeout=timeout)
    conflict_files: List[str] = []
    for line in p_status.stdout.splitlines():
        line = line.strip()
        if len(line) >= 3:
            code = line[:2]
            if "U" in code or code in ("AA", "DD"):
                conflict_files.append(line[3:].strip())

    if conflict_files:
        return {
            "success": False,
            "conflict": True,
            "conflict_files": conflict_files,
            "index_restored": False,
            "stash_ref": "stash@{0}",
            "error": f"Merge conflict in {', '.join(conflict_files)} while restoring stashed changes. Stash entry preserved in stash@{{0}}.",
        }

    # If no conflict files, but --index failed (e.g. index patch rejected), try standard stash pop
    p_pop = run_git(repo_path, ["stash", "pop"], timeout=timeout)
    if p_pop.returncode == 0:
        return {
            "success": True,
            "conflict": False,
            "conflict_files": [],
            "index_restored": False,
            "stash_ref": None,
            "error": None,
        }

    p_status2 = run_git(repo_path, ["status", "--porcelain"], timeout=timeout)
    conflict_files2: List[str] = []
    for line in p_status2.stdout.splitlines():
        line = line.strip()
        if len(line) >= 3:
            code = line[:2]
            if "U" in code or code in ("AA", "DD"):
                conflict_files2.append(line[3:].strip())

    if conflict_files2:
        return {
            "success": False,
            "conflict": True,
            "conflict_files": conflict_files2,
            "index_restored": False,
            "stash_ref": "stash@{0}",
            "error": f"Merge conflict in {', '.join(conflict_files2)} while restoring stashed changes. Stash entry preserved in stash@{{0}}.",
        }

    err_msg = p_pop.stderr.strip() or p_pop.stdout.strip() or out or "Failed to pop stash"
    return {
        "success": False,
        "conflict": False,
        "conflict_files": [],
        "index_restored": False,
        "stash_ref": "stash@{0}",
        "error": f"Failed to restore stash: {err_msg}. Stash preserved in stash@{{0}}.",
    }


def prune_repo_remotes(
    repo_path: str,
    remote: Optional[str] = None,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    benchmark: Optional[TelemetryBenchmark] = None,
    retries: int = DEFAULT_RETRIES,
    cache: Optional[BulkCache] = None,
) -> Dict[str, Any]:
    """
    Proactively audit and safely prune stale remote-tracking references (e.g. deleted remote branches).
    # INVARIANT: GIT_TERMINAL_PROMPT=0 prevents interactive prompts during network inspection.
    # INVARIANT: When dry_run is True, stale references are discovered without altering Git refs.
    """
    remotes_dict = cache.get_remotes(repo_path, timeout=timeout) if cache else get_repo_remotes(repo_path, timeout=timeout)

    target_remotes: List[str] = []
    if remote:
        if remote in remotes_dict:
            target_remotes.append(remote)
        else:
            return {
                "repo": repo_path,
                "success": False,
                "remotes": [],
                "stale_refs": [],
                "pruned_refs": [],
                "would_prune_refs": [],
                "dry_run": dry_run,
                "errors": [f"Remote '{remote}' not configured in repository"],
            }
    else:
        target_remotes = list(remotes_dict.keys())

    if not target_remotes:
        return {
            "repo": repo_path,
            "success": True,
            "remotes": [],
            "stale_refs": [],
            "pruned_refs": [],
            "would_prune_refs": [],
            "dry_run": dry_run,
            "errors": [],
        }

    stale_refs: List[str] = []
    pruned_refs: List[str] = []
    would_prune_refs: List[str] = []
    errors: List[str] = []

    for r_name in target_remotes:
        # Step 1: Detect stale refs via --dry-run
        t_net = time.perf_counter()
        try:
            p_dry = run_git_with_retry(
                repo_path,
                ["remote", "prune", r_name, "--dry-run"],
                timeout=timeout,
                retries=retries,
                benchmark=benchmark,
            )
        finally:
            if benchmark:
                benchmark.record_network_latency(time.perf_counter() - t_net)

        if p_dry.returncode != 0:
            err = p_dry.stderr.strip() or p_dry.stdout.strip()
            errors.append(f"Failed to inspect stale refs for remote '{r_name}': {err}")
            continue

        found = re.findall(r"\*\s*\[(?:would prune|pruned)\]\s+(\S+)", p_dry.stdout)
        stale_refs.extend(found)

        if dry_run:
            would_prune_refs.extend(found)
        elif found:
            # Step 2: Safely prune stale remote refs
            t_net = time.perf_counter()
            try:
                p_prune = run_git_with_retry(
                    repo_path,
                    ["remote", "prune", r_name],
                    timeout=timeout,
                    retries=retries,
                    benchmark=benchmark,
                )
            finally:
                if benchmark:
                    benchmark.record_network_latency(time.perf_counter() - t_net)

            if p_prune.returncode == 0:
                pruned = re.findall(r"\*\s*\[pruned\]\s+(\S+)", p_prune.stdout)
                pruned_refs.extend(pruned if pruned else found)
            else:
                err = p_prune.stderr.strip() or p_prune.stdout.strip()
                errors.append(f"Failed to prune remote '{r_name}': {err}")

    stale_refs = list(dict.fromkeys(stale_refs))
    pruned_refs = list(dict.fromkeys(pruned_refs))
    would_prune_refs = list(dict.fromkeys(would_prune_refs))

    return {
        "repo": repo_path,
        "success": len(errors) == 0,
        "remotes": target_remotes,
        "stale_refs": stale_refs,
        "pruned_refs": pruned_refs,
        "would_prune_refs": would_prune_refs,
        "dry_run": dry_run,
        "errors": errors,
    }


def audit_branch_hygiene(
    repo_path: str,
    default_branch: Optional[str] = None,
    audit_stale_remotes: bool = False,
    timeout: int = 5,
    benchmark: Optional[TelemetryBenchmark] = None,
    cache: Optional[BulkCache] = None,
) -> Dict[str, Any]:
    """
    Proactively audit local and remote-tracking branch health across a repository.
    Identifies:
    1. Gone branches: local branches whose upstream tracking reference was deleted or pruned.
    2. Merged branches: local branches fully merged into default branch (safe to delete).
    3. Untracked branches: local branches with no upstream tracking reference.
    4. Stale remote tracking refs: remote branches deleted on server but still tracked locally.
    # INVARIANT: Branches checked out in active or secondary worktrees MUST NOT be marked as merged or deletable.
    # INVARIANT: Current branch and default branch are NEVER marked as merged/deletable.
    """
    t_start = time.perf_counter()
    wt_info = cache.get_worktree_info(repo_path, timeout=timeout) if cache else get_worktree_info(repo_path, timeout=timeout)
    checked_out_branches = set(wt_info.get("checked_out_branches", {}).keys())

    p_curr = run_git(repo_path, ["branch", "--show-current"], timeout=timeout)
    curr_branch = p_curr.stdout.strip()
    if curr_branch:
        checked_out_branches.add(curr_branch)

    p_branches = run_git(
        repo_path,
        ["for-each-ref", "--format=%(refname:short)|%(upstream:short)|%(upstream:track)", "refs/heads/"],
        timeout=timeout,
    )
    all_local_branches: List[str] = []
    gone_branches: List[str] = []
    untracked_branches: List[str] = []
    tracking_branches: Dict[str, str] = {}

    for line in p_branches.stdout.splitlines():
        parts = line.split("|")
        if len(parts) >= 3:
            b_name, b_up, b_track = parts[0], parts[1], parts[2]
            if not b_name:
                continue
            all_local_branches.append(b_name)
            if b_up:
                tracking_branches[b_name] = b_up
                if "[gone]" in b_track:
                    gone_branches.append(b_name)
            else:
                untracked_branches.append(b_name)

    # Determine default branch (e.g. main, master, or origin/HEAD)
    target_default = default_branch
    if not target_default:
        p_head = run_git(repo_path, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=timeout)
        if p_head.returncode == 0 and p_head.stdout.strip():
            ref = p_head.stdout.strip()
            target_default = ref.split("/")[-1] if "/" in ref else ref

    if not target_default or target_default not in all_local_branches:
        for candidate in ("main", "master", "trunk", "development"):
            if candidate in all_local_branches:
                target_default = candidate
                break
    if not target_default:
        target_default = curr_branch or "main"

    merged_branches: List[str] = []
    if target_default in all_local_branches or f"origin/{target_default}" in tracking_branches.values():
        p_merged = run_git(repo_path, ["branch", "--merged", target_default], timeout=timeout)
        if p_merged.returncode == 0:
            for b_line in p_merged.stdout.splitlines():
                clean_b = b_line.replace("*", "").replace("+", "").strip()
                if (
                    clean_b
                    and clean_b != target_default
                    and clean_b not in checked_out_branches
                    and clean_b in all_local_branches
                ):
                    merged_branches.append(clean_b)

    untracked_branches = [b for b in untracked_branches if b != target_default and b != curr_branch]

    stale_remote_refs: List[str] = []
    if audit_stale_remotes:
        prune_audit = prune_repo_remotes(repo_path, dry_run=True, timeout=timeout, benchmark=benchmark, cache=cache)
        stale_remote_refs = prune_audit.get("stale_refs", [])

    is_clean = len(gone_branches) == 0 and len(merged_branches) == 0 and len(stale_remote_refs) == 0

    if benchmark:
        benchmark.record_disk_latency(time.perf_counter() - t_start)

    return {
        "repo": repo_path,
        "current_branch": curr_branch,
        "default_branch": target_default,
        "total_branches": len(all_local_branches),
        "local_branches": all_local_branches,
        "stale_remote_refs": stale_remote_refs,
        "gone_branches": gone_branches,
        "merged_branches": merged_branches,
        "untracked_branches": untracked_branches,
        "checked_out_branches": list(checked_out_branches),
        "is_clean": is_clean,
    }


def delete_local_branches(
    repo_path: str,
    branches: List[str],
    force: bool = False,
    dry_run: bool = False,
    timeout: int = 5,
    cache: Optional[BulkCache] = None,
) -> Dict[str, Any]:
    """
    Safely delete obsolete local branches.
    # INVARIANT: NEVER deletes the currently checked out branch or branches checked out in other worktrees.
    # INVARIANT: When dry_run is True, validates safety without altering refs.
    """
    wt_info = cache.get_worktree_info(repo_path, timeout=timeout) if cache else get_worktree_info(repo_path, timeout=timeout)
    checked_out = set(wt_info.get("checked_out_branches", {}).keys())

    p_curr = run_git(repo_path, ["branch", "--show-current"], timeout=timeout)
    curr_branch = p_curr.stdout.strip()
    if curr_branch:
        checked_out.add(curr_branch)

    deleted: List[str] = []
    would_delete: List[str] = []
    skipped: List[Dict[str, str]] = []
    errors: Dict[str, str] = {}

    for b in branches:
        if not b:
            continue
        if b in checked_out:
            skipped.append({"branch": b, "reason": "branch is currently checked out"})
            continue

        if dry_run:
            would_delete.append(b)
            continue

        del_flag = "-D" if force else "-d"
        p_del = run_git(repo_path, ["branch", del_flag, b], timeout=timeout)
        if p_del.returncode == 0:
            deleted.append(b)
        else:
            err_msg = p_del.stderr.strip() or p_del.stdout.strip()
            errors[b] = err_msg
            skipped.append({"branch": b, "reason": err_msg})

    return {
        "repo": repo_path,
        "deleted": deleted,
        "would_delete": would_delete,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
    }


# Re-exported from git_sync.engine.changelog
from git_sync.engine.changelog import (
    BREAKING_CHANGE_REGEX,
    CHANGELOG_CATEGORY_ORDER,
    CONVENTIONAL_CATEGORY_MAP,
    CONVENTIONAL_COMMIT_REGEX,
    ClassifiedCommit,
    classify_commit,
    format_changelog_summary,
    get_repo_commits,
    get_repo_pending_changelog,
    parse_commit_log,
    summarize_commit_categories,
)


def inspect_repo(
    repo_path: str,
    root_dir: str,
    do_fetch: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    cache: Optional[BulkCache] = None,
    remote: Optional[str] = None,
    benchmark: Optional[TelemetryBenchmark] = None,
    retries: int = DEFAULT_RETRIES,
    prune: bool = False,
    audit_hygiene: bool = False,
    changelog: bool = False,
) -> Dict[str, Any]:
    """Audit single repository sync state, working tree cleanliness, worktrees, and submodules."""
    t_start_disk = time.perf_counter()
    rel_path = os.path.relpath(repo_path, root_dir)
    group = rel_path.split(os.sep)[0] if os.sep in rel_path else ""

    # 0. Check worktree & submodule status
    wt_info = cache.get_worktree_info(repo_path, timeout=5) if cache else get_worktree_info(repo_path, timeout=5)
    is_worktree = wt_info["is_worktree"]
    worktree_main_path = wt_info["main_worktree"]
    worktrees = wt_info["worktrees"]
    checked_out_branches = wt_info["checked_out_branches"]
    stale_worktrees = wt_info.get("stale_worktrees", [])

    submodules = cache.get_submodules_status(repo_path, timeout=5) if cache else get_submodules_status(repo_path, timeout=5)

    info: Dict[str, Any] = {
        "name": os.path.basename(repo_path),
        "path": repo_path,
        "rel_path": rel_path,
        "group": group,
        "branch": "",
        "remote_url": "",
        "remotes": {},
        "target_remote": None,
        "multi_remote": False,
        "is_dirty": False,
        "dirty_files": [],
        "fetch_ok": False,
        "fetch_error": None,
        "diagnostic": None,
        "network_state": "healthy",
        "remediation": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "aux_branches_behind": {},
        "aux_branches_ahead": {},
        "status": "UNKNOWN",
        "action_required": "none",
        "is_worktree": is_worktree,
        "worktree_main_path": worktree_main_path,
        "worktrees": worktrees,
        "stale_worktrees": stale_worktrees,
        "submodules": submodules,
        "is_shallow": is_shallow_repository(repo_path),
        "has_lfs": False,
        "lfs": None,
        "sparse_checkout": {
            "enabled": False,
            "cone_mode": False,
            "patterns": [],
            "pattern_count": 0,
            "sparse_index": False,
            "out_of_cone_files": [],
            "warning": None,
        },
        "stale_refs": [],
        "pruned_refs": [],
        "gone_branches": [],
        "merged_branches": [],
        "branch_hygiene": None,
        "changelog": None,
    }

    # INVARIANT: Inspect local branch and working tree dirty status before remote checks to ensure complete local visibility even for unconfigured/NO_REMOTE repositories.
    # 1. Current branch
    p_branch = run_git(repo_path, ["branch", "--show-current"], timeout=5)
    info["branch"] = p_branch.stdout.strip() or "HEAD (detached)"

    # 2. Dirty status
    is_dirty, dirty_files = check_repo_dirty(repo_path, worktrees=worktrees, timeout=5, cache=cache)
    if is_dirty:
        info["is_dirty"] = True
        info["dirty_files"] = dirty_files

    # 2b. Sparse-checkout status and cone diagnostics
    sc_info = cache.get_sparse_checkout_info(repo_path, dirty_files=dirty_files, timeout=5) if cache else get_sparse_checkout_info(repo_path, dirty_files=dirty_files, timeout=5)
    info["sparse_checkout"] = sc_info

    # 2c. Git LFS tracking and storage diagnostics
    lfs_info = cache.get_lfs_info(repo_path, timeout=5) if cache else get_lfs_info(repo_path, timeout=5)
    info["has_lfs"] = lfs_info.get("has_lfs", False)
    info["lfs"] = lfs_info

    # 3. Remotes
    remotes = cache.get_remotes(repo_path, timeout=5) if cache else get_repo_remotes(repo_path, timeout=5)
    if not remotes and is_worktree and worktree_main_path:
        remotes = cache.get_remotes(worktree_main_path, timeout=5) if cache else get_repo_remotes(worktree_main_path, timeout=5)

    info["remotes"] = remotes
    info["multi_remote"] = len(remotes) > 1

    if not remotes:
        if benchmark:
            benchmark.record_disk_latency(time.perf_counter() - t_start_disk)
            benchmark.record_failure("no_remote")
        info["status"] = "NO_REMOTE"
        info["network_state"] = "unconfigured"
        info["action_required"] = "configure_remote"
        info["remediation"] = f"git -C '{repo_path}' remote add origin <url>"
        return info

    if remote:
        if remote not in remotes:
            if benchmark:
                benchmark.record_disk_latency(time.perf_counter() - t_start_disk)
                benchmark.record_failure("no_remote")
            info["status"] = "NO_REMOTE"
            info["network_state"] = "unconfigured"
            info["action_required"] = f"configure_remote_{remote}"
            info["fetch_error"] = f"Requested remote '{remote}' not configured in repository"
            info["remediation"] = f"git -C '{repo_path}' remote add {remote} <url>"
            return info
        target_remote = remote
    elif "origin" in remotes:
        target_remote = "origin"
    else:
        target_remote = next(iter(remotes.keys()))

    info["target_remote"] = target_remote
    info["remote_url"] = remotes.get(target_remote, "")

    # 4. Fetch with multi-remote coordination and retry mechanics
    if do_fetch:
        if benchmark:
            benchmark.record_disk_latency(time.perf_counter() - t_start_disk)
        t_net = time.perf_counter()
        prune_opts = ["--prune"] if prune else []
        try:
            if remote:
                p_fetch = run_git_with_retry(repo_path, ["fetch"] + prune_opts + [remote], timeout=timeout, retries=retries, benchmark=benchmark)
                if p_fetch.returncode == 0:
                    info["fetch_ok"] = True
                else:
                    info["fetch_ok"] = False
                    info["fetch_error"] = p_fetch.stderr.strip().splitlines()[-1] if p_fetch.stderr else "Fetch failed"
            elif len(remotes) > 1:
                # Coordinate multi-remote fetch across all configured remotes
                p_fetch = run_git_with_retry(repo_path, ["fetch", "--all"] + prune_opts, timeout=timeout, retries=retries, benchmark=benchmark)
                if p_fetch.returncode == 0:
                    info["fetch_ok"] = True
                else:
                    # Fallback to fetching target primary remote if secondary auxiliary remote errored
                    p_target = run_git_with_retry(repo_path, ["fetch"] + prune_opts + [target_remote], timeout=timeout, retries=retries, benchmark=benchmark)
                    if p_target.returncode == 0:
                        info["fetch_ok"] = True
                        err_line = p_fetch.stderr.strip().splitlines()[-1] if p_fetch.stderr else "Auxiliary remote error"
                        info["fetch_error"] = f"Warning: auxiliary remote fetch failed ({err_line})"
                    else:
                        info["fetch_ok"] = False
                        info["fetch_error"] = p_target.stderr.strip().splitlines()[-1] if p_target.stderr else "Fetch failed"
            else:
                p_fetch = run_git_with_retry(repo_path, ["fetch"] + prune_opts, timeout=timeout, retries=retries, benchmark=benchmark)
                if p_fetch.returncode == 0:
                    info["fetch_ok"] = True
                else:
                    info["fetch_ok"] = False
                    info["fetch_error"] = p_fetch.stderr.strip().splitlines()[-1] if p_fetch.stderr else "Fetch failed"

            # Parse pruned remote refs if prune was enabled
            if prune and info.get("fetch_ok"):
                raw_fetch_out = (p_fetch.stderr or "") + "\n" + (p_fetch.stdout or "")
                pruned_found = re.findall(r"\s*-\s*\[deleted\]\s+\(none\)\s+->\s+(\S+)", raw_fetch_out)
                if pruned_found:
                    info["pruned_refs"] = list(dict.fromkeys(pruned_found))
        except subprocess.TimeoutExpired:
            info["fetch_ok"] = False
            info["fetch_error"] = f"Timeout ({timeout}s)"
        except Exception as e:
            info["fetch_ok"] = False
            info["fetch_error"] = str(e)
        finally:
            if benchmark:
                benchmark.record_network_latency(time.perf_counter() - t_net)
        t_start_disk = time.perf_counter()
    else:
        info["fetch_ok"] = True

    if not info["fetch_ok"]:
        err_text = info.get("fetch_error") or ""
        diag = _dispatch_fn("diagnose_git_error", diagnose_git_error)(err_text, info.get("remote_url"))
        info["diagnostic"] = diag
        if diag:
            info["action_required"] = diag["action_required"]
            cat = diag.get("category", "")
            if cat in ("NETWORK_DNS_FAILURE", "NETWORK_TIMEOUT", "NETWORK_UNREACHABLE"):
                info["network_state"] = "offline"
                info["remediation"] = "git-sync status --no-fetch"
            elif cat in ("HTTP_AUTH_REQUIRED", "HTTP_AUTH_FAILED"):
                info["network_state"] = "auth_error"
                info["remediation"] = "gh auth login" if shutil.which("gh") else "git config --global credential.helper store"
            elif cat in ("SSH_PERMISSION_DENIED", "SSH_NO_AGENT", "SSH_AGENT_REFUSED"):
                info["network_state"] = "auth_error"
                info["remediation"] = "ssh-add ~/.ssh/id_ed25519"
            elif cat == "REMOTE_NOT_FOUND":
                info["network_state"] = "not_found"
                info["remediation"] = f"git -C '{repo_path}' remote set-url {target_remote} <url>"
            else:
                info["network_state"] = "error"
                info["remediation"] = diag.get("suggestion")
        else:
            info["action_required"] = "check_credentials_or_network"
            info["network_state"] = "error"
            info["remediation"] = "Verify git remote configuration and network reachability."

        if benchmark:
            benchmark.record_disk_latency(time.perf_counter() - t_start_disk)
            fail_type = diag["category"].lower() if diag else "fetch_error"
            benchmark.record_failure(fail_type)
        info["status"] = "FETCH_ERROR"
        return info

    # 5. Upstream status for current branch
    p_up = run_git(repo_path, ["rev-parse", "--abbrev-ref", "@{u}"], timeout=5)
    upstream = p_up.stdout.strip()
    if p_up.returncode == 0 and upstream and "fatal:" not in upstream:
        info["upstream"] = upstream
        try:
            p_ahead = run_git(repo_path, ["rev-list", "--count", f"{upstream}..HEAD"], timeout=5)
            p_behind = run_git(repo_path, ["rev-list", "--count", f"HEAD..{upstream}"], timeout=5)
            info["ahead"] = int(p_ahead.stdout.strip() or 0)
            info["behind"] = int(p_behind.stdout.strip() or 0)
        except Exception:
            pass
    else:
        info["upstream"] = None

    # 6. Check auxiliary tracking branches & branch hygiene
    # INVARIANT: Skip branches checked out in other worktrees to avoid duplicate audit and refused fetches
    p_branches = run_git(repo_path, ["for-each-ref", "--format=%(refname:short)|%(upstream:short)|%(upstream:track)", "refs/heads/"], timeout=5)
    repo_abs = os.path.abspath(repo_path)
    for b_line in p_branches.stdout.splitlines():
        parts = b_line.split("|")
        if len(parts) >= 3:
            b_name, b_up, b_track = parts[0], parts[1], parts[2]
            if "[gone]" in b_track:
                info["gone_branches"].append(b_name)
            if b_up and b_name != info["branch"]:
                if b_name in checked_out_branches and checked_out_branches[b_name] != repo_abs:
                    continue
                m_behind = re.search(r"behind (\d+)", b_track)
                if m_behind:
                    info["aux_branches_behind"][b_name] = int(m_behind.group(1))
                m_ahead = re.search(r"ahead (\d+)", b_track)
                if m_ahead:
                    info["aux_branches_ahead"][b_name] = int(m_ahead.group(1))

    if audit_hygiene:
        hygiene = audit_branch_hygiene(
            repo_path,
            audit_stale_remotes=False,
            timeout=5,
            cache=cache,
        )
        info["merged_branches"] = hygiene["merged_branches"]
        info["branch_hygiene"] = hygiene

    if benchmark:
        benchmark.record_hygiene(
            repo_rel=rel_path,
            stale_remote_refs=info.get("stale_refs", []),
            pruned_remote_refs=info.get("pruned_refs", []),
            gone_branches=info.get("gone_branches", []),
            merged_branches=info.get("merged_branches", []),
        )

    # Evaluate Overall Status
    if info["ahead"] > 0 and info["behind"] > 0:
        info["status"] = "DIVERGED"
        info["action_required"] = "merge_or_rebase"
        info["remediation"] = f"git-sync resolve {rel_path} --strategy <rebase|merge>"
    elif info["behind"] > 0:
        info["status"] = "PULL_PENDING"
        info["action_required"] = "pull"
        info["remediation"] = f"git-sync pull {rel_path}"
    elif info["ahead"] > 0:
        info["status"] = "PUSH_PENDING"
        info["action_required"] = "push"
        info["remediation"] = f"git-sync push {rel_path}"
    elif info["aux_branches_behind"]:
        info["status"] = "AUX_PULL_PENDING"
        info["action_required"] = "pull_aux"
        info["remediation"] = f"git-sync pull {rel_path} --sync-branches"
    elif info["aux_branches_ahead"]:
        info["status"] = "AUX_PUSH_PENDING"
        info["action_required"] = "push_aux"
        info["remediation"] = f"git-sync push {rel_path} --all"
    elif not info["upstream"]:
        info["status"] = "NO_UPSTREAM"
        info["action_required"] = "push_upstream"
        curr_b = info.get("branch") or "main"
        target_r = info.get("target_remote") or "origin"
        info["remediation"] = f"git -C '{repo_path}' push -u {target_r} {curr_b}"
    else:
        info["status"] = "UP_TO_DATE"
        info["action_required"] = "none"
        info["remediation"] = None

    if changelog:
        if info["upstream"] and (info["ahead"] > 0 or info["behind"] > 0):
            info["changelog"] = get_repo_pending_changelog(
                repo_path,
                upstream=info["upstream"],
                ahead=info["ahead"],
                behind=info["behind"],
                timeout=timeout,
            )
        else:
            info["changelog"] = {
                "incoming": [],
                "outgoing": [],
                "incoming_categories": {},
                "outgoing_categories": {},
                "incoming_breaking": 0,
                "outgoing_breaking": 0,
                "has_breaking": False,
            }

    if benchmark:
        benchmark.record_disk_latency(time.perf_counter() - t_start_disk)

    return info


def pull_repo(
    repo_path: str,
    remote: Optional[str] = None,
    ff_only: bool = True,
    sync_branches: bool = True,
    strategy: Optional[str] = None,
    autostash: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    cache: Optional[BulkCache] = None,
    dry_run: bool = False,
    benchmark: Optional[TelemetryBenchmark] = None,
    retries: int = DEFAULT_RETRIES,
    recover_stale_lock: bool = False,
    prune: bool = False,
    submodules: bool = False,
    unshallow: bool = False,
    reapply_sparse: bool = False,
    lfs: bool = False,
    no_lfs: bool = False,
) -> Dict[str, Any]:
    """
    Pull updates for a single repository.
    # INVARIANT: Atomic fast-forward for auxiliary branches uses `fetch . origin/b:b` without working tree switch.
    # INVARIANT: Skip branches checked out in other worktrees because Git refuses direct fetch into active worktree branches.
    # INVARIANT: If --ff-only is refused and a resolution strategy is provided, execute guided resolution.
    # INVARIANT: If autostash is True, dirty uncommitted changes (staged, unstaged, untracked) are safely stashed
    #            before pull and restored afterwards, preserving worktree integrity without data loss.
    # INVARIANT: If unstash encounters a conflict, the stash is preserved in stash@{0} and the conflict is isolated.
    # INVARIANT: When dry_run is True, no working tree modifications, stashes, or ref updates occur.
    # INVARIANT: When reapply_sparse is True or sparse-checkout is configured, cone patterns are safely reapplied and isolated.
    # INVARIANT: When no_lfs is True, GIT_LFS_SKIP_SMUDGE=1 skips downloading large binary payloads for bandwidth savings.
    """
    curr_branch = run_git(repo_path, ["branch", "--show-current"]).stdout.strip()

    # INVARIANT: Resolve worktree topology early to isolate active worktree branches and report worktree state.
    wt_info = cache.get_worktree_info(repo_path, timeout=5) if cache else get_worktree_info(repo_path, timeout=5)
    is_worktree = wt_info.get("is_worktree", False)
    worktree_main_path = wt_info.get("main_worktree")
    checked_out_branches = wt_info.get("checked_out_branches", {})

    sparse_info = cache.get_sparse_checkout_info(repo_path, timeout=5) if cache else get_sparse_checkout_info(repo_path, timeout=5)
    lfs_info = cache.get_lfs_info(repo_path, timeout=5) if cache else get_lfs_info(repo_path, timeout=5)
    repo_has_lfs = lfs_info.get("has_lfs", False)
    pull_extra_env: Optional[Dict[str, str]] = {"GIT_LFS_SKIP_SMUDGE": "1"} if no_lfs else None

    if dry_run:
        is_dirty, dirty_files = check_repo_dirty(repo_path, timeout=timeout, cache=cache)
        autostash_info: Dict[str, Any] = {
            "stashed": False,
            "restored": False,
            "conflict": False,
            "conflict_files": [],
            "stash_ref": None,
            "error": None,
            "would_stash": is_dirty,
            "dirty_files": dirty_files if is_dirty else [],
        }

        prune_res: Dict[str, Any] = {"stale_refs": [], "pruned_refs": [], "would_prune_refs": []}
        if prune:
            prune_res = prune_repo_remotes(
                repo_path,
                remote=remote,
                dry_run=True,
                timeout=timeout,
                benchmark=benchmark,
                retries=retries,
                cache=cache,
            )

        repo_is_shallow = is_shallow_repository(repo_path)
        submodules_res: Optional[Dict[str, Any]] = None
        if submodules or unshallow:
            submodules_res = _dispatch_fn("update_submodules", update_submodules)(
                repo_path,
                recursive=True,
                init=True,
                timeout=timeout,
                dry_run=True,
                unshallow=unshallow,
                cache=cache,
            )

        p_up = run_git(repo_path, ["rev-parse", "--abbrev-ref", "@{u}"], timeout=5)
        upstream = p_up.stdout.strip()
        if p_up.returncode != 0 or not upstream or "fatal:" in upstream:
            if benchmark:
                benchmark.record_failure("missing_upstream")
            return {
                "repo": repo_path,
                "branch": curr_branch,
                "remote": remote,
                "success": False,
                "stdout": "",
                "stderr": "No remote upstream tracking branch configured.",
                "aux_branches": {},
                "worktree_isolated": {},
                "is_worktree": is_worktree,
                "worktree_main_path": worktree_main_path,
                "autostash": autostash_info,
                "dry_run": True,
                "ahead": 0,
                "behind": 0,
                "is_shallow": repo_is_shallow,
                "unshallow": unshallow,
                "sparse_checkout": sparse_info,
                "reapply_sparse": reapply_sparse,
                "lfs": {
                    "has_lfs": repo_has_lfs,
                    "skipped": no_lfs,
                    "fetched": lfs,
                    "objects_count": lfs_info.get("objects_count", 0),
                },
                "stale_refs": prune_res.get("stale_refs", []),
                "pruned_refs": prune_res.get("pruned_refs", []),
                "would_prune_refs": prune_res.get("would_prune_refs", []),
                "submodules": submodules_res,
            }

        p_ahead = run_git(repo_path, ["rev-list", "--count", f"{upstream}..HEAD"], timeout=5)
        p_behind = run_git(repo_path, ["rev-list", "--count", f"HEAD..{upstream}"], timeout=5)
        try:
            ahead_count = int(p_ahead.stdout.strip() or 0)
            behind_count = int(p_behind.stdout.strip() or 0)
        except ValueError:
            ahead_count = 0
            behind_count = 0

        resolved_info: Optional[Dict[str, Any]] = None
        if ahead_count == 0 and behind_count > 0:
            success = True
            stdout = f"[DRY-RUN] Fast-forward update possible ({behind_count} commit(s) behind {upstream})"
            stderr = ""
        elif ahead_count == 0 and behind_count == 0:
            success = True
            stdout = f"[DRY-RUN] Already up-to-date with {upstream}"
            stderr = ""
        else:
            if strategy and strategy.lower() in ("rebase", "merge"):
                resolve_res = resolve_divergence(repo_path, strategy=strategy, branch=curr_branch, timeout=timeout, dry_run=True)
                resolved_info = resolve_res
                success = resolve_res["success"]
                stdout = resolve_res.get("stdout") or resolve_res.get("message") or ""
                stderr = resolve_res.get("stderr", "")
            else:
                success = not ff_only
                stdout = ""
                stderr = f"[DRY-RUN] Cannot fast-forward: branch diverged ({ahead_count} ahead, {behind_count} behind {upstream})"

        aux_results: Dict[str, bool] = {}
        worktree_isolated: Dict[str, str] = {}
        if sync_branches and success:
            repo_abs = os.path.abspath(repo_path)

            p_branches = run_git(repo_path, ["for-each-ref", "--format=%(refname:short)|%(upstream:short)|%(upstream:track)", "refs/heads/"])
            for b_line in p_branches.stdout.splitlines():
                parts = b_line.split("|")
                if len(parts) >= 3:
                    b_name, b_up, b_track = parts[0], parts[1], parts[2]
                    if b_up and b_name != curr_branch and "behind" in b_track:
                        if b_name in checked_out_branches and os.path.abspath(checked_out_branches[b_name]) != repo_abs:
                            # INVARIANT: Isolate active branches checked out across secondary worktrees
                            worktree_isolated[b_name] = checked_out_branches[b_name]
                            continue
                        aux_results[b_name] = True

        if not success and benchmark:
            benchmark.record_failure("pull_error")

        if reapply_sparse and sparse_info.get("enabled"):
            sc_pat_cnt = sparse_info.get("pattern_count", 0)
            prefix = f"[DRY-RUN] Reapplying sparse-checkout cone filters ({sc_pat_cnt} pattern(s))\n"
            stdout = (prefix + stdout).strip() if stdout else prefix.strip()

        lfs_res: Dict[str, Any] = {
            "has_lfs": repo_has_lfs,
            "skipped": no_lfs,
            "fetched": lfs,
            "objects_count": lfs_info.get("objects_count", 0),
        }
        if repo_has_lfs:
            if no_lfs:
                lfs_prefix = "[DRY-RUN] Bandwidth control active: skipping Git LFS payload download (GIT_LFS_SKIP_SMUDGE=1)\n"
                stdout = (lfs_prefix + stdout).strip() if stdout else lfs_prefix.strip()
            elif lfs:
                lfs_prefix = "[DRY-RUN] Bandwidth control active: would pull Git LFS objects via 'git lfs pull'\n"
                stdout = (lfs_prefix + stdout).strip() if stdout else lfs_prefix.strip()

        res_dict: Dict[str, Any] = {
            "repo": repo_path,
            "branch": curr_branch,
            "remote": remote,
            "success": success,
            "stdout": stdout,
            "stderr": stderr,
            "aux_branches": aux_results,
            "worktree_isolated": worktree_isolated,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
            "autostash": autostash_info,
            "dry_run": True,
            "ahead": ahead_count,
            "behind": behind_count,
            "is_shallow": repo_is_shallow,
            "unshallow": unshallow,
            "sparse_checkout": sparse_info,
            "reapply_sparse": reapply_sparse,
            "lfs": lfs_res,
            "stale_refs": prune_res.get("stale_refs", []),
            "pruned_refs": prune_res.get("pruned_refs", []),
            "would_prune_refs": prune_res.get("would_prune_refs", []),
            "submodules": submodules_res,
        }
        if resolved_info:
            res_dict["resolved"] = resolved_info
        return res_dict

    prune_res: Dict[str, Any] = {"stale_refs": [], "pruned_refs": [], "would_prune_refs": []}
    if prune:
        prune_res = prune_repo_remotes(
            repo_path,
            remote=remote,
            dry_run=False,
            timeout=timeout,
            benchmark=benchmark,
            retries=retries,
            cache=cache,
        )

    stashed = False
    stash_tag: Optional[str] = None
    autostash_info: Dict[str, Any] = {
        "stashed": False,
        "restored": False,
        "conflict": False,
        "conflict_files": [],
        "stash_ref": None,
        "error": None,
    }

    if autostash:
        is_dirty, dirty_files = check_repo_dirty(repo_path, timeout=timeout, cache=cache)
        if is_dirty:
            stash_ok, tag, stash_err = stash_changes(repo_path, timeout=timeout)
            if not stash_ok and recover_stale_lock:
                stash_diag = diagnose_git_error(stash_err or "", None)
                if stash_diag and stash_diag.get("category") == "INDEX_LOCKED":
                    rec_ok, _ = recover_index_lock(repo_path, force=False, max_age_seconds=300.0)
                    if rec_ok:
                        stash_ok, tag, stash_err = stash_changes(repo_path, timeout=timeout)

            if stash_err == "NO_CHANGES":
                stashed = False
            elif not stash_ok:
                diag = diagnose_git_error(stash_err or "", None)
                if benchmark:
                    benchmark.record_failure("autostash_error")
                return {
                    "repo": repo_path,
                    "branch": curr_branch,
                    "remote": remote,
                    "success": False,
                    "stdout": "",
                    "stderr": f"Failed to autostash dirty changes: {stash_err}",
                    "aux_branches": {},
                    "worktree_isolated": {},
                    "is_worktree": is_worktree,
                    "worktree_main_path": worktree_main_path,
                    "autostash": {
                        "stashed": False,
                        "restored": False,
                        "conflict": False,
                        "conflict_files": [],
                        "stash_ref": None,
                        "error": stash_err,
                    },
                    "dry_run": False,
                    "diagnostic": diag,
                }
            else:
                stashed = True
                stash_tag = tag
                autostash_info["stashed"] = True

    repo_is_shallow = is_shallow_repository(repo_path)
    if unshallow and repo_is_shallow:
        fetch_unsh = ["fetch", "--unshallow"]
        if remote:
            fetch_unsh.append(remote)
        p_unsh = run_git_with_retry(repo_path, fetch_unsh, timeout=timeout, benchmark=benchmark)
        if p_unsh.returncode != 0:
            unsh_err = p_unsh.stderr.strip()
            stderr = f"{stderr}\n{unsh_err}".strip() if stderr else unsh_err

    cmd = ["pull"]
    if ff_only:
        cmd.append("--ff-only")
    if remote:
        cmd.extend([remote, curr_branch])

    t_pull = time.perf_counter()
    proc = run_git_with_retry(repo_path, cmd, timeout=timeout, benchmark=benchmark, extra_env=pull_extra_env)
    if benchmark:
        benchmark.record_network_latency(time.perf_counter() - t_pull)

    success = (proc.returncode == 0)
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    resolved_info: Optional[Dict[str, Any]] = None
    diagnostic: Optional[Dict[str, str]] = None

    if not success:
        diagnostic = diagnose_git_error(stderr, None)
        if recover_stale_lock and diagnostic and diagnostic.get("category") == "INDEX_LOCKED":
            rec_ok, _ = recover_index_lock(repo_path, force=False, max_age_seconds=300.0)
            if rec_ok:
                proc = run_git_with_retry(repo_path, cmd, timeout=timeout, benchmark=benchmark, extra_env=pull_extra_env)
                success = (proc.returncode == 0)
                stdout = proc.stdout.strip()
                stderr = proc.stderr.strip()
                diagnostic = None if success else diagnose_git_error(stderr, None)

        if not success and strategy and strategy.lower() in ("rebase", "merge"):
            resolve_res = resolve_divergence(repo_path, strategy=strategy, branch=curr_branch, timeout=timeout)
            resolved_info = resolve_res
            if resolve_res["success"]:
                success = True
                stdout = resolve_res.get("stdout", stdout)
                stderr = ""
                diagnostic = None
            else:
                stderr = resolve_res.get("stderr", stderr)

    aux_results: Dict[str, bool] = {}
    worktree_isolated: Dict[str, str] = {}
    if sync_branches and success:
        repo_abs = os.path.abspath(repo_path)

        p_branches = run_git(repo_path, ["for-each-ref", "--format=%(refname:short)|%(upstream:short)|%(upstream:track)", "refs/heads/"])
        for b_line in p_branches.stdout.splitlines():
            parts = b_line.split("|")
            if len(parts) >= 3:
                b_name, b_up, b_track = parts[0], parts[1], parts[2]
                if b_up and b_name != curr_branch and "behind" in b_track:
                    if b_name in checked_out_branches and os.path.abspath(checked_out_branches[b_name]) != repo_abs:
                        # INVARIANT: Isolate active branches checked out across secondary worktrees
                        # to prevent collision and git error 128 (refusing to fetch into checked out branch).
                        worktree_isolated[b_name] = checked_out_branches[b_name]
                        continue
                    ff_p = run_git(repo_path, ["fetch", ".", f"{b_up}:{b_name}"], extra_env=pull_extra_env)
                    if ff_p.returncode != 0 and (
                        "Refusing to fetch into current branch" in ff_p.stderr
                        or "Cannot force update" in ff_p.stderr
                        or "refusing to fetch into checked out branch" in ff_p.stderr.lower()
                    ):
                        worktree_isolated[b_name] = checked_out_branches.get(b_name, "linked worktree")
                    else:
                        aux_results[b_name] = (ff_p.returncode == 0)

    if stashed:
        restore_res = restore_stash(repo_path, timeout=timeout)
        autostash_info["restored"] = restore_res["success"]
        autostash_info["conflict"] = restore_res.get("conflict", False)
        autostash_info["conflict_files"] = restore_res.get("conflict_files", [])
        autostash_info["stash_ref"] = restore_res.get("stash_ref")
        autostash_info["error"] = restore_res.get("error")

        if not restore_res["success"]:
            success = False
            restore_err = restore_res.get("error") or "Failed to restore autostash"
            if stderr:
                stderr = f"{stderr}\n{restore_err}"
            else:
                stderr = restore_err

    if not success and benchmark:
        fail_type = diagnostic["category"].lower() if diagnostic else "pull_error"
        benchmark.record_failure(fail_type)

    if success and cache:
        cache.invalidate(repo_path)

    submodules_res: Optional[Dict[str, Any]] = None
    if (submodules or unshallow) and success:
        submodules_res = _dispatch_fn("update_submodules", update_submodules)(
            repo_path,
            recursive=True,
            init=True,
            timeout=timeout,
            dry_run=False,
            unshallow=unshallow,
            cache=cache,
        )
        if submodules_res and not submodules_res.get("success", True):
            success = False
            sub_err = submodules_res.get("stderr") or "Submodule update failed"
            stderr = f"{stderr}\n{sub_err}".strip() if stderr else sub_err

    sparse_reapply_res: Optional[Dict[str, Any]] = None
    if reapply_sparse and sparse_info.get("enabled") and success:
        sparse_reapply_res = _dispatch_fn("reapply_sparse_checkout", reapply_sparse_checkout)(
            repo_path,
            cone=sparse_info.get("cone_mode", True),
            timeout=timeout,
        )
        if not sparse_reapply_res.get("success"):
            reapply_err = sparse_reapply_res.get("stderr") or "Failed to reapply sparse-checkout"
            stderr = f"{stderr}\n{reapply_err}".strip() if stderr else reapply_err

    lfs_pull_res: Optional[Dict[str, Any]] = None
    if success and lfs and not no_lfs and repo_has_lfs:
        lfs_pull_res = _dispatch_fn("pull_lfs_objects", pull_lfs_objects)(repo_path, timeout=timeout)
        if not lfs_pull_res.get("success"):
            lfs_err = lfs_pull_res.get("stderr") or "Git LFS pull failed"
            stderr = f"{stderr}\n{lfs_err}".strip() if stderr else lfs_err

    lfs_res: Dict[str, Any] = {
        "has_lfs": repo_has_lfs,
        "skipped": no_lfs,
        "fetched": (lfs and not no_lfs),
        "pull_result": lfs_pull_res,
        "objects_count": lfs_info.get("objects_count", 0),
    }

    result_dict: Dict[str, Any] = {
        "repo": repo_path,
        "branch": curr_branch,
        "remote": remote,
        "success": success,
        "stdout": stdout,
        "stderr": stderr,
        "aux_branches": aux_results,
        "worktree_isolated": worktree_isolated,
        "is_worktree": is_worktree,
        "worktree_main_path": worktree_main_path,
        "autostash": autostash_info,
        "dry_run": False,
        "diagnostic": diagnostic,
        "is_shallow": repo_is_shallow,
        "unshallow": unshallow,
        "sparse_checkout": sparse_info,
        "reapply_sparse": reapply_sparse,
        "sparse_reapply": sparse_reapply_res,
        "lfs": lfs_res,
        "stale_refs": prune_res.get("stale_refs", []),
        "pruned_refs": prune_res.get("pruned_refs", []),
        "would_prune_refs": prune_res.get("would_prune_refs", []),
        "submodules": submodules_res,
    }
    if benchmark and prune:
        benchmark.record_hygiene(
            repo_rel=os.path.basename(repo_path),
            stale_remote_refs=prune_res.get("stale_refs", []),
            pruned_remote_refs=prune_res.get("pruned_refs", []),
        )
    if resolved_info:
        result_dict["resolved"] = resolved_info
    return result_dict


def is_branch_protected(branch: str, patterns: Optional[List[str]] = None) -> bool:
    """
    Determine if a branch name matches protected branch configuration patterns.
    # INVARIANT: Standard protected branch patterns default to main, master, release/*, production, and prod.
    """
    if not branch:
        return False
    effective_patterns = DEFAULT_PROTECTED_BRANCHES if patterns is None else patterns
    for pat in effective_patterns:
        if fnmatch.fnmatch(branch, pat):
            return True
    return False


def preflight_push_check(
    repo_path: str,
    branch: Optional[str] = None,
    remote: Optional[str] = None,
    force: bool = False,
    protected_branches: Optional[List[str]] = None,
    block_protected: bool = False,
    allow_protected: bool = False,
    cache: Optional[BulkCache] = None,
    timeout: int = 5,
) -> Dict[str, Any]:
    """
    Proactive pre-flight telemetry and safety analysis prior to executing Git push.
    # INVARIANT: Evaluates branch protection, force-push defense, divergence, and uncommitted tree status.
    # INVARIANT: Force push to protected branches is unconditionally rejected to safeguard remote history.
    """
    t_start = time.perf_counter()

    curr_branch = run_git(repo_path, ["branch", "--show-current"], timeout=timeout).stdout.strip()
    target_branch = branch if branch else curr_branch

    wt_info = cache.get_worktree_info(repo_path, timeout=timeout) if cache else get_worktree_info(repo_path, timeout=timeout)
    is_worktree = wt_info.get("is_worktree", False)
    worktree_main_path = wt_info.get("main_worktree")

    patterns = list(DEFAULT_PROTECTED_BRANCHES if protected_branches is None else protected_branches)
    is_protected = is_branch_protected(target_branch, patterns) if target_branch else False

    # Check remotes
    remotes_map = cache.get_remotes(repo_path, timeout=timeout) if cache else get_repo_remotes(repo_path, timeout=timeout)
    if not remotes_map and is_worktree and worktree_main_path:
        remotes_map = cache.get_remotes(worktree_main_path, timeout=timeout) if cache else get_repo_remotes(worktree_main_path, timeout=timeout)

    target_remote: Optional[str] = None
    if remotes_map:
        if remote:
            target_remote = remote if remote in remotes_map else None
        else:
            target_remote = "origin" if "origin" in remotes_map else next(iter(remotes_map.keys()))

    # Check working tree dirty status
    is_dirty = check_repo_dirty(repo_path, timeout=timeout)

    # Detached HEAD check
    if not target_branch:
        latency = round(time.perf_counter() - t_start, 4)
        diagnostic = {
            "category": "DETACHED_HEAD",
            "message": "Cannot push in detached HEAD state.",
            "suggestion": "Checkout a named branch before pushing.",
            "action_required": "checkout_branch",
        }
        return {
            "repo": repo_path,
            "branch": "HEAD (detached)",
            "remote": target_remote,
            "is_protected": False,
            "protected_patterns": patterns,
            "force_requested": force,
            "force_defended": False,
            "block_protected": block_protected,
            "allow_protected": allow_protected,
            "upstream": None,
            "upstream_configured": False,
            "ahead": 0,
            "behind": 0,
            "diverged": False,
            "is_dirty": is_dirty,
            "passed": False,
            "diagnostic": diagnostic,
            "safety_checks": {
                "branch_state": "FAIL",
                "protected_branch_guard": "PASS",
                "force_push_guard": "PASS",
                "divergence_guard": "PASS",
            },
            "latency_seconds": latency,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    # No remote check
    if not remotes_map:
        latency = round(time.perf_counter() - t_start, 4)
        diagnostic = {
            "category": "NO_REMOTE",
            "message": "No remote configured for repository.",
            "suggestion": "Add a remote repository via 'git remote add origin <url>'.",
            "action_required": "configure_remote",
        }
        return {
            "repo": repo_path,
            "branch": target_branch,
            "remote": None,
            "is_protected": is_protected,
            "protected_patterns": patterns,
            "force_requested": force,
            "force_defended": False,
            "block_protected": block_protected,
            "allow_protected": allow_protected,
            "upstream": None,
            "upstream_configured": False,
            "ahead": 0,
            "behind": 0,
            "diverged": False,
            "is_dirty": is_dirty,
            "passed": False,
            "diagnostic": diagnostic,
            "safety_checks": {
                "branch_state": "PASS",
                "protected_branch_guard": "PASS",
                "force_push_guard": "PASS",
                "divergence_guard": "PASS",
            },
            "latency_seconds": latency,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    # Invalid remote requested
    if remote and target_remote is None:
        latency = round(time.perf_counter() - t_start, 4)
        diagnostic = {
            "category": "INVALID_REMOTE",
            "message": f"Remote '{remote}' not configured for repository. Available remotes: {', '.join(sorted(remotes_map.keys()))}.",
            "suggestion": f"Specify a valid remote name or add '{remote}' with 'git remote add {remote} <url>'.",
            "action_required": "configure_remote",
        }
        return {
            "repo": repo_path,
            "branch": target_branch,
            "remote": remote,
            "is_protected": is_protected,
            "protected_patterns": patterns,
            "force_requested": force,
            "force_defended": False,
            "block_protected": block_protected,
            "allow_protected": allow_protected,
            "upstream": None,
            "upstream_configured": False,
            "ahead": 0,
            "behind": 0,
            "diverged": False,
            "is_dirty": is_dirty,
            "passed": False,
            "diagnostic": diagnostic,
            "safety_checks": {
                "branch_state": "PASS",
                "protected_branch_guard": "PASS",
                "force_push_guard": "PASS",
                "divergence_guard": "PASS",
            },
            "latency_seconds": latency,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    # Safety Guard 1: Protected Branch Force-Push Defense
    # Force pushing to any protected branch is strictly prohibited to prevent accidental remote override
    if is_protected and force:
        latency = round(time.perf_counter() - t_start, 4)
        diagnostic = {
            "category": "PROTECTED_BRANCH_FORCE_PUSH",
            "message": f"Force-push to protected branch '{target_branch}' is strictly prohibited to prevent accidental remote override.",
            "suggestion": "Protected branches cannot be overwritten with force-push. Push with standard fast-forward or use a dedicated feature branch.",
            "action_required": "avoid_force_push",
        }
        return {
            "repo": repo_path,
            "branch": target_branch,
            "remote": target_remote,
            "is_protected": True,
            "protected_patterns": patterns,
            "force_requested": True,
            "force_defended": True,
            "block_protected": block_protected,
            "allow_protected": allow_protected,
            "upstream": None,
            "upstream_configured": False,
            "ahead": 0,
            "behind": 0,
            "diverged": False,
            "is_dirty": is_dirty,
            "passed": False,
            "diagnostic": diagnostic,
            "safety_checks": {
                "branch_state": "PASS",
                "protected_branch_guard": "BLOCKED",
                "force_push_guard": "BLOCKED",
                "divergence_guard": "PASS",
            },
            "latency_seconds": latency,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    # Safety Guard 2: Protected Branch Direct Push Safeguard
    # If direct push to protected branch is blocked by policy and not explicitly allowed
    if is_protected and block_protected and not allow_protected:
        latency = round(time.perf_counter() - t_start, 4)
        diagnostic = {
            "category": "PROTECTED_BRANCH_DIRECT_PUSH",
            "message": f"Direct push to protected branch '{target_branch}' is blocked by workspace safety policy.",
            "suggestion": "Submit a pull request or re-run with --allow-protected to override.",
            "action_required": "allow_protected_or_pr",
        }
        return {
            "repo": repo_path,
            "branch": target_branch,
            "remote": target_remote,
            "is_protected": True,
            "protected_patterns": patterns,
            "force_requested": force,
            "force_defended": False,
            "block_protected": True,
            "allow_protected": False,
            "upstream": None,
            "upstream_configured": False,
            "ahead": 0,
            "behind": 0,
            "diverged": False,
            "is_dirty": is_dirty,
            "passed": False,
            "diagnostic": diagnostic,
            "safety_checks": {
                "branch_state": "PASS",
                "protected_branch_guard": "BLOCKED",
                "force_push_guard": "PASS",
                "divergence_guard": "PASS",
            },
            "latency_seconds": latency,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    # Upstream and divergence inspection
    ahead_count = 0
    behind_count = 0
    upstream: Optional[str] = None
    upstream_configured = False

    p_up = run_git(repo_path, ["rev-parse", "--abbrev-ref", f"{target_branch}@{{u}}"], timeout=timeout)
    if p_up.returncode != 0 and target_branch == curr_branch:
        p_up = run_git(repo_path, ["rev-parse", "--abbrev-ref", "@{u}"], timeout=timeout)

    if p_up.returncode == 0 and p_up.stdout.strip() and "fatal:" not in p_up.stdout:
        upstream = p_up.stdout.strip()
        upstream_configured = True
        p_ahead = run_git(repo_path, ["rev-list", "--count", f"{upstream}..{target_branch}"], timeout=timeout)
        p_behind = run_git(repo_path, ["rev-list", "--count", f"{target_branch}..{upstream}"], timeout=timeout)
        try:
            ahead_count = int(p_ahead.stdout.strip() or 0)
            behind_count = int(p_behind.stdout.strip() or 0)
        except ValueError:
            ahead_count = 0
            behind_count = 0
    else:
        # No upstream configured: check remote tracking branch existence
        p_remote_ref = run_git(repo_path, ["rev-parse", "--verify", f"refs/remotes/{target_remote}/{target_branch}"], timeout=timeout)
        if p_remote_ref.returncode == 0:
            p_ahead = run_git(repo_path, ["rev-list", "--count", f"{target_remote}/{target_branch}..{target_branch}"], timeout=timeout)
            p_behind = run_git(repo_path, ["rev-list", "--count", f"{target_branch}..{target_remote}/{target_branch}"], timeout=timeout)
            try:
                ahead_count = int(p_ahead.stdout.strip() or 0)
                behind_count = int(p_behind.stdout.strip() or 0)
            except ValueError:
                ahead_count = 0
                behind_count = 0
        else:
            p_ahead = run_git(repo_path, ["rev-list", "--count", target_branch], timeout=timeout)
            try:
                ahead_count = int(p_ahead.stdout.strip() or 0)
            except ValueError:
                ahead_count = 0
            behind_count = 0

    diverged = (ahead_count > 0 and behind_count > 0)

    # Safety Guard 3: Divergence Guard (non-force push cannot overwrite divergent upstream)
    if diverged and not force:
        latency = round(time.perf_counter() - t_start, 4)
        diagnostic = {
            "category": "DIVERGED",
            "message": f"Branch '{target_branch}' has diverged from upstream '{upstream}' ({ahead_count} ahead, {behind_count} behind). Rebase or merge before pushing.",
            "suggestion": "Rebase or merge before pushing.",
            "action_required": "rebase_or_merge",
        }
        return {
            "repo": repo_path,
            "branch": target_branch,
            "remote": target_remote,
            "is_protected": is_protected,
            "protected_patterns": patterns,
            "force_requested": force,
            "force_defended": False,
            "block_protected": block_protected,
            "allow_protected": allow_protected,
            "upstream": upstream,
            "upstream_configured": upstream_configured,
            "ahead": ahead_count,
            "behind": behind_count,
            "diverged": True,
            "is_dirty": is_dirty,
            "passed": False,
            "diagnostic": diagnostic,
            "safety_checks": {
                "branch_state": "PASS",
                "protected_branch_guard": "PASS",
                "force_push_guard": "PASS",
                "divergence_guard": "FAIL",
            },
            "latency_seconds": latency,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    # Safety Guard 4: Behind Upstream Guard
    if behind_count > 0 and ahead_count == 0 and not force:
        latency = round(time.perf_counter() - t_start, 4)
        diagnostic = {
            "category": "BEHIND_UPSTREAM",
            "message": f"Branch '{target_branch}' is behind upstream '{upstream}' by {behind_count} commit(s). Pull changes before pushing.",
            "suggestion": "Pull changes before pushing.",
            "action_required": "pull_before_push",
        }
        return {
            "repo": repo_path,
            "branch": target_branch,
            "remote": target_remote,
            "is_protected": is_protected,
            "protected_patterns": patterns,
            "force_requested": force,
            "force_defended": False,
            "block_protected": block_protected,
            "allow_protected": allow_protected,
            "upstream": upstream,
            "upstream_configured": upstream_configured,
            "ahead": 0,
            "behind": behind_count,
            "diverged": False,
            "is_dirty": is_dirty,
            "passed": False,
            "diagnostic": diagnostic,
            "safety_checks": {
                "branch_state": "PASS",
                "protected_branch_guard": "PASS",
                "force_push_guard": "PASS",
                "divergence_guard": "FAIL",
            },
            "latency_seconds": latency,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    latency = round(time.perf_counter() - t_start, 4)
    return {
        "repo": repo_path,
        "branch": target_branch,
        "remote": target_remote,
        "is_protected": is_protected,
        "protected_patterns": patterns,
        "force_requested": force,
        "force_defended": False,
        "block_protected": block_protected,
        "allow_protected": allow_protected,
        "upstream": upstream,
        "upstream_configured": upstream_configured,
        "ahead": ahead_count,
        "behind": behind_count,
        "diverged": diverged,
        "is_dirty": is_dirty,
        "passed": True,
        "diagnostic": None,
        "safety_checks": {
            "branch_state": "PASS",
            "protected_branch_guard": "PASS",
            "force_push_guard": "PASS",
            "divergence_guard": "PASS",
        },
        "latency_seconds": latency,
        "is_worktree": is_worktree,
        "worktree_main_path": worktree_main_path,
    }


def push_repo(
    repo_path: str,
    branch: Optional[str] = None,
    remote: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    protected_branches: Optional[List[str]] = None,
    block_protected: bool = False,
    allow_protected: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    cache: Optional[BulkCache] = None,
    benchmark: Optional[TelemetryBenchmark] = None,
    retries: int = DEFAULT_RETRIES,
) -> Dict[str, Any]:
    """
    Push committed changes for a repository with pre-flight telemetry and branch protection safeguards.
    # INVARIANT: Force push to protected branches is strictly prohibited to prevent data loss across satellite repositories.
    # INVARIANT: Diverged branches (ahead > 0 and behind > 0) are blocked from pushing to prevent remote rejection.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 guarantees git never blocks on interactive auth/passphrase prompts.
    """
    preflight = preflight_push_check(
        repo_path,
        branch=branch,
        remote=remote,
        force=force,
        protected_branches=protected_branches,
        block_protected=block_protected,
        allow_protected=allow_protected,
        cache=cache,
        timeout=min(timeout, 5),
    )

    if benchmark:
        benchmark.record_disk_latency(preflight["latency_seconds"])
        benchmark.record_stage("push_preflight", preflight["latency_seconds"])

    target_branch = preflight["branch"]
    target_remote = preflight["remote"]
    is_worktree = preflight["is_worktree"]
    worktree_main_path = preflight["worktree_main_path"]

    if not preflight["passed"]:
        diag = preflight.get("diagnostic") or {}
        cat = diag.get("category", "PUSH_PREFLIGHT_FAILED")
        if benchmark:
            benchmark.record_failure(cat.lower())
            if cat in ("PROTECTED_BRANCH_FORCE_PUSH", "PROTECTED_BRANCH_DIRECT_PUSH"):
                benchmark.alerts.append({
                    "category": cat,
                    "repo": repo_path,
                    "branch": target_branch,
                    "message": diag.get("message", "Push blocked by protected branch defense."),
                })
        return {
            "repo": repo_path,
            "branch": target_branch,
            "remote": target_remote or remote,
            "success": False,
            "stdout": "",
            "stderr": diag.get("message", "Pre-flight safety check failed."),
            "dry_run": dry_run,
            "ahead": preflight.get("ahead", 0),
            "diagnostic": diag,
            "preflight": preflight,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    cmd = ["push"]
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force-with-lease")
    cmd.extend([target_remote, target_branch])

    t_push = time.perf_counter()
    proc = run_git_with_retry(repo_path, cmd, timeout=timeout, benchmark=benchmark)
    if benchmark:
        benchmark.record_network_latency(time.perf_counter() - t_push)

    success = (proc.returncode == 0)
    diagnostic: Optional[Dict[str, str]] = None

    if not success:
        diagnostic = diagnose_git_error(proc.stderr.strip(), None)
        if benchmark:
            fail_type = diagnostic["category"].lower() if diagnostic else "push_error"
            benchmark.record_failure(fail_type)

    if success and not dry_run and cache:
        cache.invalidate(repo_path)

    return {
        "repo": repo_path,
        "branch": target_branch,
        "remote": target_remote,
        "success": success,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "dry_run": dry_run,
        "ahead": preflight.get("ahead", 0),
        "diagnostic": diagnostic,
        "preflight": preflight,
        "is_worktree": is_worktree,
        "worktree_main_path": worktree_main_path,
    }


def find_push_pending_repos(
    all_repos: List[str],
    root_dir: str,
    do_fetch: bool = False,
    workers: int | str = DEFAULT_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
    cache: Optional[BulkCache] = None,
    remote: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Find all repositories with pending commits to push (ahead > 0 or aux_branches_ahead)."""
    ctrl = AdaptiveConcurrencyController()
    eff_workers = ctrl.determine_workers(len(all_repos), is_fetch=do_fetch, configured_workers=workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=eff_workers) as executor:
        inspections = list(executor.map(lambda r: inspect_repo(r, root_dir, do_fetch=do_fetch, timeout=timeout, cache=cache, remote=remote), all_repos))
    return [r for r in inspections if r.get("ahead", 0) > 0 or r.get("aux_branches_ahead")]


def find_diverged_repos(
    all_repos: List[str],
    root_dir: str,
    do_fetch: bool = False,
    workers: int | str = DEFAULT_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
    cache: Optional[BulkCache] = None,
    remote: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Find all repositories with divergent branches (ahead > 0 and behind > 0)."""
    ctrl = AdaptiveConcurrencyController()
    eff_workers = ctrl.determine_workers(len(all_repos), is_fetch=do_fetch, configured_workers=workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=eff_workers) as executor:
        inspections = list(executor.map(lambda r: inspect_repo(r, root_dir, do_fetch=do_fetch, timeout=timeout, cache=cache, remote=remote), all_repos))
    return [r for r in inspections if r.get("status") == "DIVERGED"]


# Re-exported from git_sync.engine.diff
from git_sync.engine.diff import (
    get_divergence_details,
    get_divergence_diff,
    split_diff_files,
    truncate_diff,
)


def resolve_divergence(
    repo_path: str,
    strategy: str,
    branch: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Execute resolution strategy for diverged repository branch.
    Supported strategies:
      - 'abort': No-op, preserves current state.
      - 'rebase': Rebase local commits onto remote upstream. Aborts rebase automatically on conflict.
      - 'merge': Merge remote upstream into local branch. Aborts merge automatically on conflict.
      - 'stash': Stash uncommitted changes.
      - 'diff': Output diff between local branch and upstream.
    # INVARIANT: Conflicts encountered during automated rebase or merge immediately trigger --abort
    # to prevent leaving dirty or locked Git rebase-apply/rebase-merge states.
    # INVARIANT: When dry_run is True, no rebases, merges, or working tree mutations are performed.
    """
    curr_branch = branch or run_git(repo_path, ["branch", "--show-current"], timeout=5).stdout.strip()
    strategy_norm = strategy.strip().lower()

    if strategy_norm in ("abort", "skip"):
        return {
            "repo": repo_path,
            "branch": curr_branch,
            "strategy": "abort",
            "success": True,
            "stdout": "",
            "stderr": "",
            "message": "[DRY-RUN] Resolution would be aborted." if dry_run else "Resolution aborted.",
            "dry_run": dry_run,
        }

    if strategy_norm == "diff":
        diff_text = get_divergence_diff(repo_path, branch=curr_branch, timeout=timeout)
        return {
            "repo": repo_path,
            "branch": curr_branch,
            "strategy": "diff",
            "success": True,
            "stdout": diff_text,
            "stderr": "",
            "message": "Divergence diff computed.",
            "dry_run": dry_run,
        }

    if strategy_norm == "stash":
        if dry_run:
            return {
                "repo": repo_path,
                "branch": curr_branch,
                "strategy": "stash",
                "success": True,
                "stdout": "",
                "stderr": "",
                "message": "[DRY-RUN] Would stash uncommitted local changes.",
                "dry_run": True,
            }
        p_stash = run_git(repo_path, ["stash", "push", "-u", "-m", "git-sync-autostash"], timeout=timeout)
        success = (p_stash.returncode == 0)
        return {
            "repo": repo_path,
            "branch": curr_branch,
            "strategy": "stash",
            "success": success,
            "stdout": p_stash.stdout.strip(),
            "stderr": p_stash.stderr.strip() if not success else "",
            "message": "Local changes stashed successfully." if success else "Failed to stash local changes.",
            "dry_run": False,
        }

    p_up = run_git(repo_path, ["rev-parse", "--abbrev-ref", f"{curr_branch}@{{u}}"], timeout=5)
    upstream = p_up.stdout.strip()
    if p_up.returncode != 0 or not upstream:
        return {
            "repo": repo_path,
            "branch": curr_branch,
            "strategy": strategy_norm,
            "success": False,
            "stdout": "",
            "stderr": "No remote upstream tracking branch configured.",
            "message": "Failed: missing upstream tracking branch.",
            "dry_run": dry_run,
        }

    if strategy_norm == "rebase":
        if dry_run:
            p_cnt = run_git(repo_path, ["rev-list", "--count", f"{upstream}..{curr_branch}"], timeout=5)
            cnt = p_cnt.stdout.strip() or "0"
            return {
                "repo": repo_path,
                "branch": curr_branch,
                "strategy": "rebase",
                "success": True,
                "stdout": "",
                "stderr": "",
                "message": f"[DRY-RUN] Would rebase {cnt} commit(s) of '{curr_branch}' onto '{upstream}'.",
                "dry_run": True,
            }
        p_rebase = run_git(repo_path, ["rebase", upstream], timeout=timeout)
        if p_rebase.returncode == 0:
            return {
                "repo": repo_path,
                "branch": curr_branch,
                "strategy": "rebase",
                "success": True,
                "stdout": p_rebase.stdout.strip(),
                "stderr": "",
                "message": f"Successfully rebased '{curr_branch}' onto '{upstream}'.",
                "dry_run": False,
            }
        # INVARIANT: Abort rebase on conflict to keep repo clean
        run_git(repo_path, ["rebase", "--abort"], timeout=timeout)
        err = p_rebase.stderr.strip() or p_rebase.stdout.strip()
        return {
            "repo": repo_path,
            "branch": curr_branch,
            "strategy": "rebase",
            "success": False,
            "stdout": p_rebase.stdout.strip(),
            "stderr": err,
            "message": "Rebase failed due to conflicts and was automatically aborted to preserve repository state.",
            "dry_run": False,
        }

    if strategy_norm == "merge":
        if dry_run:
            p_cnt = run_git(repo_path, ["rev-list", "--count", f"{curr_branch}..{upstream}"], timeout=5)
            cnt = p_cnt.stdout.strip() or "0"
            return {
                "repo": repo_path,
                "branch": curr_branch,
                "strategy": "merge",
                "success": True,
                "stdout": "",
                "stderr": "",
                "message": f"[DRY-RUN] Would merge {cnt} commit(s) from '{upstream}' into '{curr_branch}'.",
                "dry_run": True,
            }
        p_merge = run_git(repo_path, ["merge", "--no-edit", upstream], timeout=timeout)
        if p_merge.returncode == 0:
            return {
                "repo": repo_path,
                "branch": curr_branch,
                "strategy": "merge",
                "success": True,
                "stdout": p_merge.stdout.strip(),
                "stderr": "",
                "message": f"Successfully merged '{upstream}' into '{curr_branch}'.",
                "dry_run": False,
            }
        # INVARIANT: Abort merge on conflict to keep repo clean
        run_git(repo_path, ["merge", "--abort"], timeout=timeout)
        err = p_merge.stderr.strip() or p_merge.stdout.strip()
        return {
            "repo": repo_path,
            "branch": curr_branch,
            "strategy": "merge",
            "success": False,
            "stdout": p_merge.stdout.strip(),
            "stderr": err,
            "message": "Merge failed due to conflicts and was automatically aborted to preserve repository state.",
            "dry_run": False,
        }

    return {
        "repo": repo_path,
        "branch": curr_branch,
        "strategy": strategy_norm,
        "success": False,
        "stdout": "",
        "stderr": f"Unknown resolution strategy '{strategy}'. Supported strategies: abort, rebase, merge, stash, diff.",
        "message": f"Unknown strategy '{strategy}'.",
        "dry_run": dry_run,
    }


__all__ = [
    "CHANGELOG_CATEGORY_ORDER",
    "CONVENTIONAL_CATEGORY_MAP",
    "ClassifiedCommit",
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_CHARS_PER_TOKEN",
    "DEFAULT_COMPACT_DIFF_TOKENS",
    "DEFAULT_DISK_LATENCY_THRESHOLD",
    "DEFAULT_IGNORE_DIRS",
    "DEFAULT_MAX_BACKOFF",
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_MIN_WORKERS",
    "DEFAULT_NETWORK_LATENCY_THRESHOLD",
    "DEFAULT_PROTECTED_BRANCHES",
    "DEFAULT_RETRIES",
    "DEFAULT_ROOT",
    "DEFAULT_TIMEOUT",
    "DEFAULT_WORKERS",
    "GIT_ENV",
    "GRADE_A_THRESHOLD_SECONDS",
    "GRADE_B_THRESHOLD_SECONDS",
    "GRADE_C_THRESHOLD_SECONDS",
    "TOOL_DIR_PREFIXES",
    "audit_branch_hygiene",
    "audit_multi_remote_matrix",
    "audit_ssh_multiplexing",
    "audit_worktree_health_batch",
    "benchmark_ssh_multiplexing",
    "check_credentials_sanity",
    "check_repo_dirty",
    "classify_commit",
    "close_ssh_control_socket",
    "compute_latency_grade",
    "delete_local_branches",
    "diagnose_git_error",
    "discover_repos",
    "find_diverged_repos",
    "find_locked_repos",
    "find_push_pending_repos",
    "find_repo_locks",
    "find_ssh_control_sockets",
    "format_bytes_human",
    "format_changelog_summary",
    "get_divergence_details",
    "get_divergence_diff",
    "get_git_env",
    "get_lfs_info",
    "get_repo_commits",
    "get_repo_git_dir",
    "get_repo_pending_changelog",
    "get_repo_remotes",
    "get_sparse_checkout_info",
    "get_submodules_status",
    "get_worktree_info",
    "inspect_credential_helpers",
    "inspect_github_cli",
    "inspect_git_lfs",
    "inspect_repo",
    "inspect_ssh_config",
    "inspect_ssh_control_socket",
    "inspect_token_env",
    "is_branch_protected",
    "is_lfs_pointer_file",
    "is_path_in_cone",
    "is_shallow_repository",
    "is_transient_network_error",
    "match_repo",
    "parse_commit_log",
    "preflight_push_check",
    "probe_remote_reachability",
    "prune_repo_remotes",
    "prune_repo_worktrees",
    "prune_stale_ssh_control_sockets",
    "pull_lfs_objects",
    "pull_repo",
    "push_repo",
    "reapply_sparse_checkout",
    "recover_index_lock",
    "recover_repo_locks",
    "resolve_default_root",
    "resolve_divergence",
    "restore_stash",
    "run_git",
    "run_git_with_retry",
    "split_diff_files",
    "stash_changes",
    "summarize_commit_categories",
    "truncate_diff",
    "unshallow_submodule",
    "update_submodules",
]
