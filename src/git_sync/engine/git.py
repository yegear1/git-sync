"""
Core business logic and Git automation engine for git-sync.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
import fnmatch
import os
import re
import shutil
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
DEFAULT_COMPACT_DIFF_TOKENS = 1000
DEFAULT_CHARS_PER_TOKEN = 4

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

# INVARIANT: GIT_TERMINAL_PROMPT=0 guarantees git never blocks on interactive auth/passphrase prompts
GIT_ENV = os.environ.copy()
GIT_ENV["GIT_TERMINAL_PROMPT"] = "0"
GIT_ENV["PAGER"] = "cat"


def _real_run_git(repo_path: str, args: List[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", repo_path] + args
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=GIT_ENV,
    )


def run_git(repo_path: str, args: List[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Execute a git command within a repository directory with safe timeouts and environment."""
    return _run_git(repo_path, args, timeout=timeout)


def _run_git(repo_path: str, args: List[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    core = sys.modules.get("git_sync.core")
    if core is not None:
        target = getattr(core, "run_git", None)
        if target is not None and target is not run_git:
            return target(repo_path, args, timeout=timeout)
    return _real_run_git(repo_path, args, timeout=timeout)


def _dispatch_fn(name: str, fallback: Any) -> Any:
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

    # 2. HTTP/HTTPS Authentication errors
    if "terminal prompts disabled" in txt or "could not read username" in txt or "could not read password" in txt:
        return {
            "category": "HTTP_AUTH_REQUIRED",
            "message": "HTTP authentication prompt was disabled (GIT_TERMINAL_PROMPT=0) and no saved credentials were found.",
            "suggestion": "Configure a git credential helper ('git config --global credential.helper store') or provide a Personal Access Token via GITHUB_TOKEN or credentials store.",
            "action_required": "configure_http_credentials",
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
            proc = run_git(repo_path, args, timeout=timeout)
            if proc.returncode == 0:
                if attempt > 0 and benchmark:
                    benchmark.record_retry(recovered=True)
                return proc

            last_proc = proc
            if attempt < retries and is_transient_network_error(None, proc.stderr):
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


def get_repo_git_dir(repo_path: str) -> Path:
    """
    Resolve the actual Git directory (.git directory or worktree/submodule gitdir).
    # INVARIANT: Supports standard repositories (.git/), worktrees, and submodules (.git file).
    """
    p = Path(repo_path) / ".git"
    if p.is_dir():
        return p
    if p.is_file():
        try:
            content = p.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                gitdir_rel = content.split(":", 1)[1].strip()
                gitdir = Path(repo_path) / gitdir_rel
                if gitdir.exists():
                    return gitdir.resolve()
        except Exception:
            pass
    return p


def find_repo_locks(repo_path: str, stale_threshold_seconds: float = 300.0) -> List[Dict[str, Any]]:
    """
    Find all active or stale Git lock files in a repository (.git/*.lock or worktree gitdir).
    # INVARIANT: Safely inspects lock files without altering repository state.
    """
    git_dir = get_repo_git_dir(repo_path)
    if not git_dir.is_dir():
        return []

    locks: List[Dict[str, Any]] = []
    now = time.time()

    candidate_locks = [
        "index.lock",
        "FETCH_HEAD.lock",
        "shallow.lock",
        "HEAD.lock",
        "config.lock",
        "packed-refs.lock",
        "commit-graph.lock",
    ]

    for name in candidate_locks:
        lock_path = git_dir / name
        if lock_path.exists():
            try:
                mtime = os.path.getmtime(lock_path)
                age = max(0.0, now - mtime)
                locks.append({
                    "repo": repo_path,
                    "lock_file": str(lock_path),
                    "lock_name": name,
                    "mtime": mtime,
                    "age_seconds": round(age, 2),
                    "stale": age >= stale_threshold_seconds,
                })
            except OSError:
                pass

    refs_heads = git_dir / "refs" / "heads"
    if refs_heads.is_dir():
        try:
            for ref_lock in refs_heads.glob("*.lock"):
                if ref_lock.is_file():
                    try:
                        mtime = os.path.getmtime(ref_lock)
                        age = max(0.0, now - mtime)
                        locks.append({
                            "repo": repo_path,
                            "lock_file": str(ref_lock),
                            "lock_name": f"refs/heads/{ref_lock.name}",
                            "mtime": mtime,
                            "age_seconds": round(age, 2),
                            "stale": age >= stale_threshold_seconds,
                        })
                    except OSError:
                        pass
        except Exception:
            pass

    return locks


def find_locked_repos(
    root_dir: str,
    repos: Optional[List[str]] = None,
    group: Optional[str] = None,
    timeout: int = 5,
    stale_threshold_seconds: float = 300.0,
) -> List[Dict[str, Any]]:
    """
    Scan repositories in root_dir for active or stale Git lock files (.git/*.lock).
    # INVARIANT: Identifies stale locks left by crashed or interrupted Git subshells.
    """
    target_repos = repos
    if target_repos is None:
        target_repos = _dispatch_fn("discover_repos", discover_repos)(root_dir)
        if group:
            target_repos = [r for r in target_repos if os.path.relpath(r, root_dir).startswith(group)]

    locked: List[Dict[str, Any]] = []
    for r in target_repos:
        repo_locks = find_repo_locks(r, stale_threshold_seconds=stale_threshold_seconds)
        if repo_locks:
            for lk in repo_locks:
                lk["rel_path"] = os.path.relpath(r, root_dir)
                locked.append(lk)
    return locked


def recover_index_lock(
    repo_path: str,
    force: bool = False,
    max_age_seconds: float = 300.0,
) -> Tuple[bool, str]:
    """
    Safely recover from stale or crashed Git index locks (.git/index.lock).
    # INVARIANT: Preserves fresh locks (< max_age_seconds) unless force=True to prevent race conditions with running Git commands.
    """
    git_dir = get_repo_git_dir(repo_path)
    lock_file = git_dir / "index.lock"
    if not lock_file.exists():
        return True, "No index lock found"

    try:
        mtime = os.path.getmtime(lock_file)
        age = max(0.0, time.time() - mtime)
        if not force and age < max_age_seconds:
            return False, f"Index lock is active (age: {age:.1f}s < threshold {max_age_seconds:.1f}s)"

        lock_file.unlink()
        return True, f"Successfully removed {'stale ' if age >= max_age_seconds else ''}index lock '{lock_file.name}' (age: {age:.1f}s)"
    except Exception as exc:
        return False, f"Failed to remove index lock '{lock_file.name}': {exc}"


def recover_repo_locks(
    repo_path: str,
    force: bool = False,
    max_age_seconds: float = 300.0,
) -> Tuple[bool, List[str]]:
    """
    Remove all stale or forced Git lock files in a repository.
    # INVARIANT: Prevents stale lock contention while respecting recent active operations.
    """
    locks = find_repo_locks(repo_path, stale_threshold_seconds=max_age_seconds)
    if not locks:
        return True, ["No locks found"]

    cleared: List[str] = []
    errors: List[str] = []
    for lk in locks:
        lock_path = Path(lk["lock_file"])
        age = lk["age_seconds"]
        if not force and not lk["stale"]:
            errors.append(f"Skipping active lock '{lk['lock_name']}' (age: {age:.1f}s)")
            continue
        try:
            lock_path.unlink()
            cleared.append(f"Removed lock '{lk['lock_name']}' (age: {age:.1f}s)")
        except Exception as exc:
            errors.append(f"Failed to remove '{lk['lock_name']}': {exc}")

    success = len(errors) == 0
    return success, cleared if success else (cleared + errors)


def _parse_ssh_config_content(
    content: str, host: str = "github.com"
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse SSH configuration text and extract ControlMaster, ControlPath, and ControlPersist for target host.
    Follows OpenSSH first-match-wins semantics across matching Host blocks.
    # INVARIANT: Options prior to the first Host block apply globally to all hosts.
    """
    control_master: Optional[str] = None
    control_path: Optional[str] = None
    control_persist: Optional[str] = None

    current_matches = True
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()

        parts = line.split(None, 1)
        if not parts:
            continue
        key = parts[0]
        val = parts[1].strip() if len(parts) > 1 else ""
        if "=" in key:
            key, val = [p.strip() for p in key.split("=", 1)]

        key_lower = key.lower()
        if key_lower == "host":
            patterns = val.split()
            current_matches = any(fnmatch.fnmatch(host.lower(), p.lower()) for p in patterns)
            continue
        elif key_lower == "match":
            m_parts = val.split()
            if len(m_parts) >= 2 and m_parts[0].lower() == "host":
                patterns = m_parts[1:]
                current_matches = any(fnmatch.fnmatch(host.lower(), p.lower()) for p in patterns)
            else:
                current_matches = False
            continue

        if current_matches:
            val_clean = val.strip("\"'")
            if key_lower == "controlmaster" and control_master is None:
                control_master = val_clean
            elif key_lower == "controlpath" and control_path is None:
                control_path = val_clean
            elif key_lower == "controlpersist" and control_persist is None:
                control_persist = val_clean

    return control_master, control_path, control_persist


def inspect_ssh_config(
    config_path: Optional[Path | str] = None,
    host: str = "github.com",
    query_ssh_g: bool = True,
    timeout: int = 2,
) -> Dict[str, Any]:
    """
    Inspect ~/.ssh/config and evaluate OpenSSH effective configuration for socket
    reuse directives (ControlMaster, ControlPath, ControlPersist).

    # INVARIANT: Directives are case-insensitive and follow first-match-wins semantics per host block.
    # INVARIANT: Validates that ControlPath directory exists to prevent runtime connection errors.
    """
    resolved_path = Path(config_path) if config_path else (Path.home() / ".ssh" / "config")
    exists = resolved_path.is_file()

    control_master: Optional[str] = None
    control_path: Optional[str] = None
    control_persist: Optional[str] = None

    if exists:
        try:
            content = resolved_path.read_text(encoding="utf-8", errors="replace")
            cm, cp, cpersist = _parse_ssh_config_content(content, host=host)
            control_master = cm
            control_path = cp
            control_persist = cpersist
        except Exception:
            pass

    # If config file was not explicitly passed and ssh binary is present, query effective config via ssh -G
    if config_path is None and query_ssh_g:
        try:
            proc = subprocess.run(
                ["ssh", "-G", host],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
                env=GIT_ENV,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) == 2:
                        k = parts[0].lower()
                        v = parts[1].strip()
                        if k == "controlmaster" and control_master is None:
                            if v.lower() != "false":
                                control_master = v
                        elif k == "controlpath" and control_path is None:
                            if v.lower() != "none":
                                control_path = v
                        elif k == "controlpersist" and control_persist is None:
                            if v.lower() != "no":
                                control_persist = v
        except Exception:
            pass

    is_enabled = bool(
        control_master and control_master.lower() in ("auto", "yes", "autoask")
    )

    socket_dir: Optional[str] = None
    socket_dir_exists = False
    if control_path and control_path.lower() != "none":
        expanded = os.path.expanduser(control_path)
        socket_dir = os.path.dirname(expanded)
        socket_dir_exists = os.path.isdir(socket_dir) if socket_dir else True

    recommendation: Optional[str] = None
    if not is_enabled:
        recommendation = (
            "Enable SSH multiplexing in ~/.ssh/config to speed up multi-repository Git operations:\n"
            f"  Host {host}\n"
            "      ControlMaster auto\n"
            "      ControlPath ~/.ssh/control-%C\n"
            "      ControlPersist 10m"
        )
    elif not control_path or control_path.lower() == "none":
        recommendation = (
            "ControlMaster is enabled, but ControlPath is not set. Add 'ControlPath ~/.ssh/control-%C' to ~/.ssh/config."
        )
    elif not socket_dir_exists:
        recommendation = (
            f"ControlPath directory '{socket_dir}' does not exist. Create it with 'mkdir -p {socket_dir} && chmod 700 {socket_dir}'."
        )

    return {
        "config_path": str(resolved_path),
        "exists": exists,
        "host": host,
        "control_master": control_master,
        "control_path": control_path,
        "control_persist": control_persist,
        "is_enabled": is_enabled,
        "socket_dir": socket_dir,
        "socket_dir_exists": socket_dir_exists,
        "recommendation": recommendation,
    }


def benchmark_ssh_multiplexing(
    remote_url: str,
    host: str = "github.com",
    sample_repo: Optional[str] = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    """
    Measure remote fetch handshake latency with and without SSH socket multiplexing.

    # INVARIANT: Uses GIT_TERMINAL_PROMPT=0 and BatchMode=yes to guarantee probing never hangs.
    # INVARIANT: Ensures temporary ControlMaster sockets and background processes are cleanly terminated.
    """
    is_ssh = "@" in remote_url or remote_url.startswith("ssh://")
    if not is_ssh:
        return {
            "host": host,
            "remote_url": remote_url,
            "supported": False,
            "error": f"Remote URL '{remote_url}' is not an SSH URL.",
            "diagnostic": None,
            "without_multiplexing_seconds": None,
            "with_multiplexing_initial_seconds": None,
            "with_multiplexing_reused_seconds": None,
            "speedup_factor": None,
            "savings_pct": None,
        }

    # 1. Benchmark without multiplexing (ControlMaster=no)
    env_no_mux = dict(os.environ)
    env_no_mux["GIT_TERMINAL_PROMPT"] = "0"
    env_no_mux["GIT_SSH_COMMAND"] = "ssh -o ControlMaster=no -o ControlPath=none -o BatchMode=yes -o ConnectTimeout=5"

    try:
        t0 = time.perf_counter()
        proc_no_mux = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=env_no_mux,
            cwd=sample_repo,
        )
        t1 = time.perf_counter()
        without_multiplexing_s = round(t1 - t0, 4)

        if proc_no_mux.returncode != 0:
            err_text = proc_no_mux.stderr.strip()
            diag = _dispatch_fn("diagnose_git_error", diagnose_git_error)(err_text, remote_url)
            return {
                "host": host,
                "remote_url": remote_url,
                "supported": False,
                "error": err_text.splitlines()[-1] if err_text else "SSH connection failed",
                "diagnostic": diag,
                "without_multiplexing_seconds": without_multiplexing_s,
                "with_multiplexing_initial_seconds": None,
                "with_multiplexing_reused_seconds": None,
                "speedup_factor": None,
                "savings_pct": None,
            }
    except subprocess.TimeoutExpired:
        diag = _dispatch_fn("diagnose_git_error", diagnose_git_error)(f"Timeout ({timeout}s)", remote_url)
        return {
            "host": host,
            "remote_url": remote_url,
            "supported": False,
            "error": f"Timeout ({timeout}s) during unmultiplexed SSH probe",
            "diagnostic": diag,
            "without_multiplexing_seconds": None,
            "with_multiplexing_initial_seconds": None,
            "with_multiplexing_reused_seconds": None,
            "speedup_factor": None,
            "savings_pct": None,
        }
    except Exception as exc:
        return {
            "host": host,
            "remote_url": remote_url,
            "supported": False,
            "error": str(exc),
            "diagnostic": None,
            "without_multiplexing_seconds": None,
            "with_multiplexing_initial_seconds": None,
            "with_multiplexing_reused_seconds": None,
            "speedup_factor": None,
            "savings_pct": None,
        }

    # 2. Benchmark with multiplexing (ControlMaster=auto with temporary socket)
    with tempfile.TemporaryDirectory(prefix="git_sync_ssh_mux_") as tmp_dir:
        sock_path = os.path.join(tmp_dir, "cm-%C")
        env_mux = dict(os.environ)
        env_mux["GIT_TERMINAL_PROMPT"] = "0"
        env_mux["GIT_SSH_COMMAND"] = (
            f"ssh -o ControlMaster=auto -o ControlPath={sock_path} -o ControlPersist=60s -o BatchMode=yes -o ConnectTimeout=5"
        )

        mux_initial_s: Optional[float] = None
        mux_reused_s: Optional[float] = None

        try:
            # Probe 1: Handshake + Master socket creation
            t2 = time.perf_counter()
            proc_mux1 = subprocess.run(
                ["git", "ls-remote", "--heads", remote_url],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                env=env_mux,
                cwd=sample_repo,
            )
            t3 = time.perf_counter()
            mux_initial_s = round(t3 - t2, 4)

            if proc_mux1.returncode != 0:
                err_text = proc_mux1.stderr.strip()
                diag = _dispatch_fn("diagnose_git_error", diagnose_git_error)(err_text, remote_url)
                return {
                    "host": host,
                    "remote_url": remote_url,
                    "supported": False,
                    "error": err_text.splitlines()[-1] if err_text else "Multiplexed SSH probe failed",
                    "diagnostic": diag,
                    "without_multiplexing_seconds": without_multiplexing_s,
                    "with_multiplexing_initial_seconds": mux_initial_s,
                    "with_multiplexing_reused_seconds": None,
                    "speedup_factor": None,
                    "savings_pct": None,
                }

            # Probe 2: Warm socket reuse
            t4 = time.perf_counter()
            proc_mux2 = subprocess.run(
                ["git", "ls-remote", "--heads", remote_url],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                env=env_mux,
                cwd=sample_repo,
            )
            t5 = time.perf_counter()
            mux_reused_s = round(t5 - t4, 4)

            if proc_mux2.returncode != 0:
                err_text = proc_mux2.stderr.strip()
                diag = _dispatch_fn("diagnose_git_error", diagnose_git_error)(err_text, remote_url)
                return {
                    "host": host,
                    "remote_url": remote_url,
                    "supported": False,
                    "error": err_text.splitlines()[-1] if err_text else "Multiplexed socket reuse probe failed",
                    "diagnostic": diag,
                    "without_multiplexing_seconds": without_multiplexing_s,
                    "with_multiplexing_initial_seconds": mux_initial_s,
                    "with_multiplexing_reused_seconds": mux_reused_s,
                    "speedup_factor": None,
                    "savings_pct": None,
                }
        except subprocess.TimeoutExpired:
            diag = _dispatch_fn("diagnose_git_error", diagnose_git_error)(f"Timeout ({timeout}s)", remote_url)
            return {
                "host": host,
                "remote_url": remote_url,
                "supported": False,
                "error": f"Timeout ({timeout}s) during multiplexed SSH probe",
                "diagnostic": diag,
                "without_multiplexing_seconds": without_multiplexing_s,
                "with_multiplexing_initial_seconds": mux_initial_s,
                "with_multiplexing_reused_seconds": None,
                "speedup_factor": None,
                "savings_pct": None,
            }
        except Exception as exc:
            return {
                "host": host,
                "remote_url": remote_url,
                "supported": False,
                "error": str(exc),
                "diagnostic": None,
                "without_multiplexing_seconds": without_multiplexing_s,
                "with_multiplexing_initial_seconds": mux_initial_s,
                "with_multiplexing_reused_seconds": None,
                "speedup_factor": None,
                "savings_pct": None,
            }
        finally:
            # INVARIANT: Terminate master process to release sockets and file descriptors
            try:
                subprocess.run(
                    ["ssh", "-O", "exit", "-o", f"ControlPath={sock_path}", host],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
            except Exception:
                pass

    speedup: Optional[float] = None
    savings: Optional[float] = None
    if mux_reused_s is not None and without_multiplexing_s is not None:
        if mux_reused_s > 0:
            speedup = round(without_multiplexing_s / mux_reused_s, 2)
            savings = round((1.0 - (mux_reused_s / without_multiplexing_s)) * 100.0, 1) if without_multiplexing_s > 0 else 0.0
        else:
            speedup = round(without_multiplexing_s / 0.0001, 2) if without_multiplexing_s > 0 else 1.0
            savings = 100.0 if without_multiplexing_s > 0 else 0.0

    return {
        "host": host,
        "remote_url": remote_url,
        "supported": True,
        "error": None,
        "diagnostic": None,
        "without_multiplexing_seconds": without_multiplexing_s,
        "with_multiplexing_initial_seconds": mux_initial_s,
        "with_multiplexing_reused_seconds": mux_reused_s,
        "speedup_factor": speedup,
        "savings_pct": savings,
    }


def audit_ssh_multiplexing(
    root_dir: Optional[str] = None,
    repos: Optional[List[str]] = None,
    group: Optional[str] = None,
    timeout: int = 10,
    config_path: Optional[Path | str] = None,
    run_benchmark: bool = True,
    target_host: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute full SSH multiplexing diagnostic audit and latency benchmark.
    # INVARIANT: Probing strictly respects GIT_TERMINAL_PROMPT=0 and never blocks.
    """
    effective_root = root_dir or DEFAULT_ROOT
    target_repos = repos
    if target_repos is None:
        target_repos = _dispatch_fn("discover_repos", discover_repos)(effective_root)
        if group:
            target_repos = [r for r in target_repos if os.path.relpath(r, effective_root).startswith(group)]

    # Collect unique SSH remotes
    ssh_remotes: Dict[str, Tuple[str, str]] = {}  # host -> (remote_url, sample_repo)
    for r_path in target_repos:
        r_map = _dispatch_fn("get_repo_remotes", get_repo_remotes)(r_path, timeout=timeout)
        for _, r_url in r_map.items():
            if r_url and ("@" in r_url or r_url.startswith("ssh://")):
                host = ""
                if "@" in r_url and ":" in r_url:
                    host = r_url.split("@", 1)[1].split(":", 1)[0]
                elif "://" in r_url:
                    host = r_url.split("://", 1)[1].split("/", 1)[0]
                host = host or "github.com"
                if host not in ssh_remotes:
                    ssh_remotes[host] = (r_url, r_path)

    primary_host = target_host or (list(ssh_remotes.keys())[0] if ssh_remotes else "github.com")
    config_info = _dispatch_fn("inspect_ssh_config", inspect_ssh_config)(
        config_path=config_path, host=primary_host
    )

    benchmark_result: Optional[Dict[str, Any]] = None
    if run_benchmark:
        if ssh_remotes:
            sample_url, sample_repo = (
                ssh_remotes[primary_host]
                if primary_host in ssh_remotes
                else next(iter(ssh_remotes.values()))
            )
            benchmark_result = _dispatch_fn("benchmark_ssh_multiplexing", benchmark_ssh_multiplexing)(
                remote_url=sample_url,
                host=primary_host,
                sample_repo=sample_repo,
                timeout=timeout,
            )
        else:
            benchmark_result = {
                "host": primary_host,
                "remote_url": None,
                "supported": False,
                "error": "No SSH remotes detected in workspace repositories to benchmark against.",
                "diagnostic": None,
                "without_multiplexing_seconds": None,
                "with_multiplexing_initial_seconds": None,
                "with_multiplexing_reused_seconds": None,
                "speedup_factor": None,
                "savings_pct": None,
            }

    warnings: List[str] = []
    suggestions: List[str] = []

    if config_info.get("recommendation"):
        suggestions.append(config_info["recommendation"])

    if config_info.get("is_enabled") and not config_info.get("socket_dir_exists"):
        warnings.append(
            f"SSH ControlPath directory '{config_info.get('socket_dir')}' does not exist; OpenSSH connections will fail."
        )

    if benchmark_result and not benchmark_result.get("supported"):
        err = benchmark_result.get("error")
        if err:
            warnings.append(f"SSH benchmark probe fallback: {err}")

    ok = config_info.get("is_enabled", False) and config_info.get("socket_dir_exists", False)
    if benchmark_result and benchmark_result.get("supported"):
        ok = True

    return {
        "ok": ok,
        "config": config_info,
        "primary_host": primary_host,
        "ssh_remotes_found": len(ssh_remotes),
        "benchmark": benchmark_result,
        "warnings": warnings,
        "suggestions": suggestions,
    }


def inspect_github_cli(timeout: int = 3) -> Dict[str, Any]:
    """
    Inspect local GitHub CLI (gh) installation and authentication state.
    # INVARIANT: Uses short execution timeout to prevent blocking during doctor checks.
    """
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return {
            "installed": False,
            "path": None,
            "logged_in": False,
            "active_account": None,
            "host": None,
            "protocol": None,
            "token_scopes": [],
            "error": "GitHub CLI (gh) not found on PATH.",
            "suggestion": "Install GitHub CLI (https://cli.github.com) or configure standard Git credentials.",
        }

    try:
        proc = subprocess.run(
            [gh_bin, "auth", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=GIT_ENV,
        )
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode == 0:
            account: Optional[str] = None
            host = "github.com"
            protocol = "https"
            scopes: List[str] = []

            m_account = re.search(r"account\s+([A-Za-z0-9_-]+)", combined, re.IGNORECASE)
            if m_account:
                account = m_account.group(1)

            m_host = re.search(r"to\s+([A-Za-z0-9_.-]+)\s+account", combined, re.IGNORECASE)
            if m_host:
                host = m_host.group(1)

            m_proto = re.search(r"protocol:\s*(\S+)", combined, re.IGNORECASE)
            if m_proto:
                protocol = m_proto.group(1).lower()

            for line in combined.splitlines():
                if "token scopes" in line.lower() or "scopes:" in line.lower():
                    scopes = re.findall(r"'([^']+)'", line)
                    if scopes:
                        break

            return {
                "installed": True,
                "path": gh_bin,
                "logged_in": True,
                "active_account": account,
                "host": host,
                "protocol": protocol,
                "token_scopes": scopes,
                "error": None,
                "suggestion": None,
            }
        else:
            err_line = combined.strip().splitlines()[-1] if combined.strip() else "Not logged in"
            return {
                "installed": True,
                "path": gh_bin,
                "logged_in": False,
                "active_account": None,
                "host": None,
                "protocol": None,
                "token_scopes": [],
                "error": err_line,
                "suggestion": "Authenticate with GitHub using 'gh auth login'.",
            }
    except subprocess.TimeoutExpired:
        return {
            "installed": True,
            "path": gh_bin,
            "logged_in": False,
            "active_account": None,
            "host": None,
            "protocol": None,
            "token_scopes": [],
            "error": f"Timeout ({timeout}s) querying gh auth status",
            "suggestion": "Check GitHub CLI responsiveness or network connection.",
        }
    except Exception as exc:
        return {
            "installed": True,
            "path": gh_bin,
            "logged_in": False,
            "active_account": None,
            "host": None,
            "protocol": None,
            "token_scopes": [],
            "error": str(exc),
            "suggestion": "Run 'gh auth status' manually to diagnose.",
        }


def check_credentials_sanity(
    root_dir: str,
    repos: Optional[List[str]] = None,
    group: Optional[str] = None,
    timeout: int = 5,
    test_remotes: bool = True,
    max_remotes_to_test: int = 15,
) -> Dict[str, Any]:
    """
    Perform a proactive sanity check of Git credentials, SSH agent, key files,
    and remote connectivity across repositories in root_dir.
    # INVARIANT: Uses GIT_TERMINAL_PROMPT=0 to guarantee probing never hangs.
    # INVARIANT: Groups and tests unique remotes to avoid redundant network probes.
    """
    # 1. SSH Agent & Identities
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK")
    agent_status = "NO_AGENT"
    identities: List[str] = []

    try:
        p_ssh_add = subprocess.run(
            ["ssh-add", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=GIT_ENV,
        )
        if p_ssh_add.returncode == 0:
            agent_status = "ACTIVE"
            for line in p_ssh_add.stdout.splitlines():
                line = line.strip()
                if line:
                    identities.append(line)
        elif p_ssh_add.returncode == 1:
            agent_status = "NO_IDENTITIES"
        else:
            agent_status = "NO_AGENT"
    except Exception:
        agent_status = "NO_AGENT"

    # 2. Local SSH Key Files
    ssh_keys_found: List[str] = []
    ssh_dir = Path.home() / ".ssh"
    if ssh_dir.is_dir():
        for key_name in ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa"):
            key_path = ssh_dir / key_name
            if key_path.is_file():
                ssh_keys_found.append(key_name)

    # 3. Credential Helpers & Tokens
    credential_helpers: List[str] = []
    try:
        p_helper = subprocess.run(
            ["git", "config", "--get-all", "credential.helper"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=GIT_ENV,
        )
        if p_helper.returncode == 0:
            credential_helpers = [h.strip() for h in p_helper.stdout.splitlines() if h.strip()]
    except Exception:
        pass

    has_token_env = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))

    # 3.5 GitHub CLI (gh) Status
    gh_info = _dispatch_fn("inspect_github_cli", inspect_github_cli)(timeout=timeout)

    # 4. Remote Connectivity Probing
    remotes_tested: Dict[str, Dict[str, Any]] = {}
    target_repos = repos
    if target_repos is None:
        target_repos = _dispatch_fn("discover_repos", discover_repos)(root_dir)
        if group:
            target_repos = [r for r in target_repos if os.path.relpath(r, root_dir).startswith(group)]

    # Collect unique remote URLs across repos
    unique_remotes: Dict[str, str] = {}  # url -> sample_repo_path
    for r_path in target_repos:
        r_map = _dispatch_fn("get_repo_remotes", get_repo_remotes)(r_path, timeout=timeout)
        for _, r_url in r_map.items():
            if r_url and r_url not in unique_remotes:
                unique_remotes[r_url] = r_path
                if len(unique_remotes) >= max_remotes_to_test:
                    break
        if len(unique_remotes) >= max_remotes_to_test:
            break

    warnings: List[str] = []
    errors: List[str] = []
    suggestions: List[str] = []

    has_ssh_remotes = any("@" in u or u.startswith("ssh://") for u in unique_remotes)
    has_http_remotes = any(u.startswith("http://") or u.startswith("https://") for u in unique_remotes)

    ssh_mux_host = "github.com"
    for u in unique_remotes:
        if "@" in u and ":" in u:
            ssh_mux_host = u.split("@", 1)[1].split(":", 1)[0]
            break
        elif "://" in u and ("@" in u or u.startswith("ssh://")):
            ssh_mux_host = u.split("://", 1)[1].split("/", 1)[0]
            break

    ssh_mux_info = _dispatch_fn("inspect_ssh_config", inspect_ssh_config)(host=ssh_mux_host)

    if has_ssh_remotes:
        if agent_status == "NO_AGENT":
            warnings.append("SSH agent is not active or SSH_AUTH_SOCK is unset.")
            suggestions.append("Start SSH agent with 'eval $(ssh-agent -s)' and load keys via 'ssh-add'.")
        elif agent_status == "NO_IDENTITIES":
            warnings.append("SSH agent is running but has no identities loaded.")
            if ssh_keys_found:
                suggestions.append(f"Load identity into agent: 'ssh-add ~/.ssh/{ssh_keys_found[0]}'.")
            else:
                suggestions.append("Generate and add an SSH key: 'ssh-keygen -t ed25519 && ssh-add ~/.ssh/id_ed25519'.")

        if not ssh_mux_info.get("is_enabled"):
            suggestions.append(
                "Enable SSH multiplexing in ~/.ssh/config to speed up multi-repository fetch operations (audit with 'git-sync doctor --ssh')."
            )
        elif not ssh_mux_info.get("socket_dir_exists"):
            warnings.append(
                f"SSH ControlPath directory '{ssh_mux_info.get('socket_dir')}' does not exist; socket reuse may fail."
            )
            suggestions.append(
                f"Create socket directory: 'mkdir -p {ssh_mux_info.get('socket_dir')}'"
            )

    gh_logged_in = bool(gh_info.get("installed") and gh_info.get("logged_in"))
    if gh_info.get("installed") and not gh_logged_in:
        warnings.append("GitHub CLI (gh) is installed but not authenticated.")
        suggestions.append("Authenticate GitHub CLI with 'gh auth login'.")

    if has_http_remotes and not credential_helpers and not has_token_env and not gh_logged_in:
        warnings.append("HTTP remotes detected without configured Git credential helper, GITHUB_TOKEN, or authenticated GitHub CLI (gh).")
        suggestions.append("Configure credential helper via 'git config --global credential.helper store' or authenticate via 'gh auth login'.")

    if test_remotes:
        for r_url, sample_repo in unique_remotes.items():
            is_ssh = "@" in r_url or r_url.startswith("ssh://")
            protocol = "ssh" if is_ssh else "https"
            host = ""
            if "@" in r_url and ":" in r_url:
                host = r_url.split("@", 1)[1].split(":", 1)[0]
            elif "://" in r_url:
                host = r_url.split("://", 1)[1].split("/", 1)[0]

            try:
                proc = run_git(sample_repo, ["ls-remote", "--heads", r_url], timeout=timeout)
                if proc.returncode == 0:
                    remotes_tested[r_url] = {
                        "ok": True,
                        "protocol": protocol,
                        "host": host,
                        "error": None,
                        "diagnostic": None,
                    }
                else:
                    err_text = proc.stderr.strip()
                    diag = diagnose_git_error(err_text, r_url)
                    remotes_tested[r_url] = {
                        "ok": False,
                        "protocol": protocol,
                        "host": host,
                        "error": err_text.splitlines()[-1] if err_text else "Authentication probe failed",
                        "diagnostic": diag,
                    }
                    if diag:
                        err_msg = f"{host or r_url}: {diag['message']}"
                        if err_msg not in errors:
                            errors.append(err_msg)
                        if diag["suggestion"] not in suggestions:
                            suggestions.append(diag["suggestion"])
                    else:
                        err_msg = f"{host or r_url}: {err_text.splitlines()[-1] if err_text else 'Failed'}"
                        if err_msg not in errors:
                            errors.append(err_msg)
            except subprocess.TimeoutExpired:
                diag = diagnose_git_error(f"Timeout ({timeout}s)", r_url)
                remotes_tested[r_url] = {
                    "ok": False,
                    "protocol": protocol,
                    "host": host,
                    "error": f"Timeout ({timeout}s)",
                    "diagnostic": diag,
                }
                err_msg = f"{host or r_url}: Network timeout probing remote"
                if err_msg not in errors:
                    errors.append(err_msg)
            except Exception as exc:
                remotes_tested[r_url] = {
                    "ok": False,
                    "protocol": protocol,
                    "host": host,
                    "error": str(exc),
                    "diagnostic": None,
                }
                errors.append(f"{host or r_url}: {exc}")

    # 5. Lock file inspection
    locked_repos = find_locked_repos(root_dir, repos=target_repos, timeout=timeout)
    if locked_repos:
        stale_count = sum(1 for lk in locked_repos if lk.get("stale"))
        active_count = len(locked_repos) - stale_count
        warnings.append(
            f"Found {len(locked_repos)} repository lock file(s) ({stale_count} stale, {active_count} active)."
        )
        suggestions.append(
            "Clear stale lock files using 'git-sync unlock --all' or remove stale .git/*.lock files."
        )

    overall_ok = len(errors) == 0

    return {
        "ok": overall_ok,
        "ssh_agent": {
            "status": agent_status,
            "socket": ssh_auth_sock,
            "identities_count": len(identities),
            "identities": identities,
        },
        "ssh_keys": ssh_keys_found,
        "ssh_multiplexing": ssh_mux_info,
        "github_cli": gh_info,
        "credential_helpers": credential_helpers,
        "has_token_env": has_token_env,
        "remotes": remotes_tested,
        "locked_repos": locked_repos,
        "warnings": warnings,
        "errors": errors,
        "suggestions": suggestions,
    }


def get_worktree_info(repo_path: str, timeout: int = 5) -> Dict[str, Any]:
    """
    Query worktree topology for the given repository or linked worktree.
    Returns:
        is_worktree: bool (True if this is a secondary linked worktree)
        main_worktree: Optional[str] (path to main worktree)
        worktrees: List[Dict[str, Any]] (list of all worktrees in this repo family)
        checked_out_branches: Dict[str, str] (mapping of branch_name -> worktree_path)
    """
    proc = run_git(repo_path, ["worktree", "list", "--porcelain"], timeout=timeout)
    if proc.returncode != 0:
        return {
            "is_worktree": False,
            "main_worktree": None,
            "worktrees": [],
            "checked_out_branches": {},
        }

    worktrees: List[Dict[str, Any]] = []
    current_entry: Dict[str, Any] = {}

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            if current_entry:
                worktrees.append(current_entry)
                current_entry = {}
            continue

        if line.startswith("worktree "):
            if current_entry:
                worktrees.append(current_entry)
                current_entry = {}
            wt_path = line[len("worktree "):].strip()
            current_entry = {
                "path": os.path.abspath(wt_path),
                "head": "",
                "branch": "",
                "is_main": (len(worktrees) == 0),
            }
        elif line.startswith("HEAD ") and current_entry:
            current_entry["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch ") and current_entry:
            ref = line[len("branch "):].strip()
            current_entry["branch"] = ref.replace("refs/heads/", "")
        elif line == "detached" and current_entry:
            current_entry["branch"] = "HEAD (detached)"

    if current_entry:
        worktrees.append(current_entry)

    norm_repo = os.path.abspath(repo_path)
    main_wt = worktrees[0]["path"] if worktrees else None
    is_secondary = (main_wt is not None and norm_repo != main_wt)

    checked_out: Dict[str, str] = {}
    for wt in worktrees:
        if wt.get("branch") and wt["branch"] != "HEAD (detached)":
            checked_out[wt["branch"]] = wt["path"]

    return {
        "is_worktree": is_secondary,
        "main_worktree": main_wt if is_secondary else None,
        "worktrees": worktrees,
        "checked_out_branches": checked_out,
    }




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




def get_submodules_status(repo_path: str, timeout: int = 5) -> List[Dict[str, Any]]:
    """
    Query submodule status for repo_path.
    Returns a list of dicts:
        path: str (submodule relative path)
        commit: str (commit SHA)
        describe: str (branch/tag description, e.g. heads/main)
        status: str (UP_TO_DATE, MODIFIED, UNINITIALIZED, CONFLICT)
    # INVARIANT: If .gitmodules file does not exist, repository has no submodules; skip subprocess entirely.
    """
    if not os.path.isfile(os.path.join(repo_path, ".gitmodules")):
        return []

    proc = run_git(repo_path, ["submodule", "status"], timeout=timeout)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    submodules: List[Dict[str, Any]] = []
    prefix_map = {
        " ": "UP_TO_DATE",
        "+": "MODIFIED",
        "-": "UNINITIALIZED",
        "U": "CONFLICT",
    }
    for line in proc.stdout.splitlines():
        if not line:
            continue
        prefix = line[0]
        rest = line[1:].strip()
        parts = rest.split(None, 2)
        if not parts:
            continue
        commit = parts[0]
        sub_path = parts[1] if len(parts) > 1 else ""
        describe = ""
        if len(parts) > 2:
            desc_raw = parts[2].strip()
            if desc_raw.startswith("(") and desc_raw.endswith(")"):
                describe = desc_raw[1:-1]
            else:
                describe = desc_raw

        status_str = prefix_map.get(prefix, "UNKNOWN")
        submodules.append({
            "path": sub_path,
            "commit": commit,
            "describe": describe,
            "status": status_str,
        })

    return submodules


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


@dataclass
class ClassifiedCommit:
    hash: str
    subject: str
    body: str = ""
    commit_type: str = "other"
    scope: Optional[str] = None
    description: str = ""
    is_breaking: bool = False
    category: str = "Changed"
    breaking_note: Optional[str] = None

    @property
    def type(self) -> str:
        return self.commit_type

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hash": self.hash,
            "subject": self.subject,
            "body": self.body,
            "type": self.commit_type,
            "scope": self.scope,
            "description": self.description,
            "is_breaking": self.is_breaking,
            "category": self.category,
            "breaking_note": self.breaking_note,
        }


CONVENTIONAL_COMMIT_REGEX = re.compile(
    r"^(?P<type>[a-zA-Z0-9_\-]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<description>.*)$"
)
BREAKING_CHANGE_REGEX = re.compile(
    r"(?:^|\n)BREAKING[- ]CHANGE:\s*(.+)",
    re.MULTILINE | re.IGNORECASE,
)

CONVENTIONAL_CATEGORY_MAP = {
    "feat": "Added",
    "feature": "Added",
    "add": "Added",
    "fix": "Fixed",
    "bugfix": "Fixed",
    "hotfix": "Fixed",
    "perf": "Changed",
    "performance": "Changed",
    "refactor": "Changed",
    "style": "Changed",
    "docs": "Changed",
    "chore": "Changed",
    "build": "Changed",
    "ci": "Changed",
    "test": "Changed",
    "tests": "Changed",
    "revert": "Removed",
    "remove": "Removed",
    "delete": "Removed",
    "deprecate": "Removed",
    "deprecated": "Removed",
    "sec": "Security",
    "security": "Security",
    "vuln": "Security",
}

CHANGELOG_CATEGORY_ORDER = [
    "Breaking",
    "Security",
    "Added",
    "Fixed",
    "Changed",
    "Removed",
    "Other",
]


def classify_commit(hash_val: str, subject: str, body: str = "") -> ClassifiedCommit:
    """
    Deterministically classify a Git commit message using Conventional Commits specification
    into Keep-a-Changelog buckets (Added, Changed, Fixed, Removed, Security, Breaking).
    # INVARIANT: Flags breaking changes from both '!' prefix and 'BREAKING CHANGE:' footer.
    """
    clean_subj = subject.strip()
    clean_body = body.strip()
    is_breaking = False
    breaking_note: Optional[str] = None

    m_body_break = BREAKING_CHANGE_REGEX.search(clean_body)
    if m_body_break:
        is_breaking = True
        breaking_note = m_body_break.group(1).strip()

    m = CONVENTIONAL_COMMIT_REGEX.match(clean_subj)
    if m:
        raw_type = m.group("type").lower()
        scope = m.group("scope")
        if m.group("breaking") == "!":
            is_breaking = True
        description = m.group("description").strip()

        base_cat = CONVENTIONAL_CATEGORY_MAP.get(raw_type, "Changed")
        category = "Breaking" if is_breaking else base_cat
        return ClassifiedCommit(
            hash=hash_val.strip(),
            subject=clean_subj,
            body=clean_body,
            commit_type=raw_type,
            scope=scope,
            description=description,
            is_breaking=is_breaking,
            category=category,
            breaking_note=breaking_note,
        )

    # Heuristic fallback for non-conventional commit messages
    lower_subj = clean_subj.lower()
    if lower_subj.startswith("break:") or "breaking change" in lower_subj:
        is_breaking = True

    if is_breaking:
        category = "Breaking"
        commit_type = "breaking"
    elif lower_subj.startswith(("add ", "added ", "new ", "create ", "feat")):
        category = "Added"
        commit_type = "feat"
    elif lower_subj.startswith(("fix ", "fixed ", "patch ", "bug ")):
        category = "Fixed"
        commit_type = "fix"
    elif lower_subj.startswith(("remove ", "removed ", "delete ", "revert ", "deprecate")):
        category = "Removed"
        commit_type = "revert"
    elif lower_subj.startswith(("sec ", "security ", "patch-sec")):
        category = "Security"
        commit_type = "sec"
    else:
        category = "Changed"
        commit_type = "other"

    return ClassifiedCommit(
        hash=hash_val.strip(),
        subject=clean_subj,
        body=clean_body,
        commit_type=commit_type,
        scope=None,
        description=clean_subj,
        is_breaking=is_breaking,
        category=category,
        breaking_note=breaking_note,
    )


def parse_commit_log(log_output: str) -> List[ClassifiedCommit]:
    """
    Parse git log output formatted with %h%x00%s%x00%b%x1f into a list of ClassifiedCommit objects.
    # INVARIANT: Uses ASCII unit separator 0x1F to delimit commits and null byte 0x00 to delimit fields.
    """
    if not log_output or not log_output.strip():
        return []

    commits: List[ClassifiedCommit] = []
    records = log_output.split("\x1f")
    for rec in records:
        rec_clean = rec.strip("\r\n")
        if not rec_clean:
            continue
        parts = rec_clean.split("\x00", 2)
        if len(parts) >= 2:
            h = parts[0].strip()
            s = parts[1].strip()
            b = parts[2].strip() if len(parts) > 2 else ""
            if h or s:
                commits.append(classify_commit(h, s, b))
        elif len(parts) == 1 and parts[0].strip():
            commits.append(classify_commit("", parts[0].strip(), ""))
    return commits


def summarize_commit_categories(commits: List[ClassifiedCommit]) -> Dict[str, int]:
    """Summarize commit counts per Keep-a-Changelog category in canonical order."""
    counts: Dict[str, int] = {}
    for c in commits:
        counts[c.category] = counts.get(c.category, 0) + 1
    sorted_counts: Dict[str, int] = {}
    for cat in CHANGELOG_CATEGORY_ORDER:
        if cat in counts:
            sorted_counts[cat] = counts[cat]
    for cat, cnt in counts.items():
        if cat not in sorted_counts:
            sorted_counts[cat] = cnt
    return sorted_counts


def format_changelog_summary(categories: Dict[str, int]) -> str:
    """Format category count dictionary into a concise human-readable summary string."""
    if not categories:
        return ""
    parts = []
    for cat in CHANGELOG_CATEGORY_ORDER:
        if cat in categories:
            parts.append(f"{cat}: {categories[cat]}")
    for cat, cnt in categories.items():
        if f"{cat}: {cnt}" not in parts:
            parts.append(f"{cat}: {cnt}")
    return ", ".join(parts)


def get_repo_commits(repo_path: str, revision_range: str, timeout: int = DEFAULT_TIMEOUT) -> List[ClassifiedCommit]:
    """Retrieve and classify commits in the specified revision range using git log formatting."""
    proc = run_git(repo_path, ["log", revision_range, "--format=%h%x00%s%x00%b%x1f"], timeout=timeout)
    if proc.returncode != 0:
        return []
    return parse_commit_log(proc.stdout)


def get_repo_pending_changelog(
    repo_path: str,
    upstream: Optional[str] = None,
    ahead: int = 0,
    behind: int = 0,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Retrieve and categorize incoming and outgoing commits between HEAD and upstream.
    Uses ahead/behind counts to avoid subshells when there are no pending changes.
    """
    incoming_commits: List[ClassifiedCommit] = []
    outgoing_commits: List[ClassifiedCommit] = []

    up_ref = upstream or "@{u}"

    if behind > 0:
        incoming_commits = get_repo_commits(repo_path, f"HEAD..{up_ref}", timeout=timeout)
    elif upstream is None and ahead == 0 and behind == 0:
        incoming_commits = get_repo_commits(repo_path, f"HEAD..{up_ref}", timeout=timeout)

    if ahead > 0:
        outgoing_commits = get_repo_commits(repo_path, f"{up_ref}..HEAD", timeout=timeout)
    elif upstream is None and ahead == 0 and behind == 0:
        outgoing_commits = get_repo_commits(repo_path, f"{up_ref}..HEAD", timeout=timeout)

    inc_cats = summarize_commit_categories(incoming_commits)
    out_cats = summarize_commit_categories(outgoing_commits)
    inc_breaking = sum(1 for c in incoming_commits if c.is_breaking)
    out_breaking = sum(1 for c in outgoing_commits if c.is_breaking)

    return {
        "incoming": [c.to_dict() for c in incoming_commits],
        "outgoing": [c.to_dict() for c in outgoing_commits],
        "incoming_categories": inc_cats,
        "outgoing_categories": out_cats,
        "incoming_breaking": inc_breaking,
        "outgoing_breaking": out_breaking,
        "has_breaking": (inc_breaking > 0 or out_breaking > 0),
    }


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
        "submodules": submodules,
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
    """
    curr_branch = run_git(repo_path, ["branch", "--show-current"]).stdout.strip()

    # INVARIANT: Resolve worktree topology early to isolate active worktree branches and report worktree state.
    wt_info = cache.get_worktree_info(repo_path, timeout=5) if cache else get_worktree_info(repo_path, timeout=5)
    is_worktree = wt_info.get("is_worktree", False)
    worktree_main_path = wt_info.get("main_worktree")
    checked_out_branches = wt_info.get("checked_out_branches", {})

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
                "stale_refs": prune_res.get("stale_refs", []),
                "pruned_refs": prune_res.get("pruned_refs", []),
                "would_prune_refs": prune_res.get("would_prune_refs", []),
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
            "stale_refs": prune_res.get("stale_refs", []),
            "pruned_refs": prune_res.get("pruned_refs", []),
            "would_prune_refs": prune_res.get("would_prune_refs", []),
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

            if not stash_ok:
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
            stashed = True
            stash_tag = tag
            autostash_info["stashed"] = True

    cmd = ["pull"]
    if ff_only:
        cmd.append("--ff-only")
    if remote:
        cmd.extend([remote, curr_branch])

    t_pull = time.perf_counter()
    proc = run_git_with_retry(repo_path, cmd, timeout=timeout, benchmark=benchmark)
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
                proc = run_git_with_retry(repo_path, cmd, timeout=timeout, benchmark=benchmark)
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
                    ff_p = run_git(repo_path, ["fetch", ".", f"{b_up}:{b_name}"])
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
        "stale_refs": prune_res.get("stale_refs", []),
        "pruned_refs": prune_res.get("pruned_refs", []),
        "would_prune_refs": prune_res.get("would_prune_refs", []),
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


def push_repo(
    repo_path: str,
    branch: Optional[str] = None,
    remote: Optional[str] = None,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    cache: Optional[BulkCache] = None,
    benchmark: Optional[TelemetryBenchmark] = None,
    retries: int = DEFAULT_RETRIES,
) -> Dict[str, Any]:
    """
    Push committed changes for a repository.
    # INVARIANT: Force push is strictly prohibited to prevent data loss across satellite repositories.
    # INVARIANT: Diverged branches (ahead > 0 and behind > 0) are blocked from pushing to prevent remote rejection.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 guarantees git never blocks on interactive auth/passphrase prompts.
    """
    curr_branch = run_git(repo_path, ["branch", "--show-current"]).stdout.strip()
    target_branch = branch if branch else curr_branch

    wt_info = cache.get_worktree_info(repo_path, timeout=5) if cache else get_worktree_info(repo_path, timeout=5)
    is_worktree = wt_info.get("is_worktree", False)
    worktree_main_path = wt_info.get("main_worktree")

    if not target_branch:
        if benchmark:
            benchmark.record_failure("detached_head")
        return {
            "repo": repo_path,
            "branch": "HEAD (detached)",
            "remote": remote,
            "success": False,
            "stdout": "",
            "stderr": "Cannot push in detached HEAD state.",
            "dry_run": dry_run,
            "ahead": 0,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    # Verify remotes exist (with worktree fallback to main worktree)
    remotes_map = cache.get_remotes(repo_path, timeout=5) if cache else get_repo_remotes(repo_path, timeout=5)
    if not remotes_map and is_worktree and worktree_main_path:
        remotes_map = cache.get_remotes(worktree_main_path, timeout=5) if cache else get_repo_remotes(worktree_main_path, timeout=5)
    if not remotes_map:
        if benchmark:
            benchmark.record_failure("no_remote")
        return {
            "repo": repo_path,
            "branch": target_branch,
            "remote": remote,
            "success": False,
            "stdout": "",
            "stderr": "No remote configured for repository.",
            "dry_run": dry_run,
            "ahead": 0,
            "is_worktree": is_worktree,
            "worktree_main_path": worktree_main_path,
        }

    if remote:
        if remote not in remotes_map:
            if benchmark:
                benchmark.record_failure("no_remote")
            return {
                "repo": repo_path,
                "branch": target_branch,
                "remote": remote,
                "success": False,
                "stdout": "",
                "stderr": f"Remote '{remote}' not configured for repository. Available remotes: {', '.join(sorted(remotes_map.keys()))}.",
                "dry_run": dry_run,
                "ahead": 0,
                "is_worktree": is_worktree,
                "worktree_main_path": worktree_main_path,
            }
        target_remote = remote
    else:
        target_remote = "origin" if "origin" in remotes_map else next(iter(remotes_map.keys()))

    # Check upstream divergence if target is current branch
    ahead_count = 0
    if target_branch == curr_branch:
        p_up = run_git(repo_path, ["rev-parse", "--abbrev-ref", "@{u}"], timeout=5)
        upstream = p_up.stdout.strip()
        if p_up.returncode == 0 and upstream and "fatal:" not in upstream:
            p_ahead = run_git(repo_path, ["rev-list", "--count", f"{upstream}..HEAD"], timeout=5)
            p_behind = run_git(repo_path, ["rev-list", "--count", f"HEAD..{upstream}"], timeout=5)
            try:
                ahead_count = int(p_ahead.stdout.strip() or 0)
                behind_count = int(p_behind.stdout.strip() or 0)
            except ValueError:
                ahead_count = 0
                behind_count = 0

            if ahead_count > 0 and behind_count > 0:
                if benchmark:
                    benchmark.record_failure("diverged")
                return {
                    "repo": repo_path,
                    "branch": target_branch,
                    "remote": target_remote,
                    "success": False,
                    "stdout": "",
                    "stderr": f"Branch '{target_branch}' has diverged from upstream '{upstream}' ({ahead_count} ahead, {behind_count} behind). Rebase or merge before pushing.",
                    "dry_run": dry_run,
                    "ahead": ahead_count,
                    "is_worktree": is_worktree,
                    "worktree_main_path": worktree_main_path,
                }
            if behind_count > 0 and ahead_count == 0:
                if benchmark:
                    benchmark.record_failure("behind_upstream")
                return {
                    "repo": repo_path,
                    "branch": target_branch,
                    "remote": target_remote,
                    "success": False,
                    "stdout": "",
                    "stderr": f"Branch '{target_branch}' is behind upstream '{upstream}' by {behind_count} commit(s). Pull changes before pushing.",
                    "dry_run": dry_run,
                    "ahead": 0,
                    "is_worktree": is_worktree,
                    "worktree_main_path": worktree_main_path,
                }

    cmd = ["push"]
    if dry_run:
        cmd.append("--dry-run")
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
        "ahead": ahead_count,
        "diagnostic": diagnostic,
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


def get_divergence_details(
    repo_path: str,
    branch: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Inspect divergence details between local branch and remote tracking upstream.
    Returns ahead/behind counts, commit logs (local and remote), merge-base, diffstat, and dirty status.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 prevents interactive prompts when querying git refs.
    """
    curr_branch = branch or run_git(repo_path, ["branch", "--show-current"], timeout=5).stdout.strip()
    if not curr_branch:
        curr_branch = "HEAD (detached)"

    # Dirty status check
    p_status = run_git(repo_path, ["status", "--porcelain"], timeout=5)
    dirty_lines = [line.strip() for line in p_status.stdout.splitlines() if line.strip()]

    # Upstream check
    p_up = run_git(repo_path, ["rev-parse", "--abbrev-ref", f"{curr_branch}@{{u}}"], timeout=5)
    upstream = p_up.stdout.strip()
    if p_up.returncode != 0 or not upstream or "fatal:" in upstream:
        return {
            "repo": repo_path,
            "branch": curr_branch,
            "upstream": None,
            "ahead": 0,
            "behind": 0,
            "is_diverged": False,
            "merge_base": None,
            "local_commits": [],
            "remote_commits": [],
            "diff_stat": "",
            "is_dirty": bool(dirty_lines),
            "dirty_files": dirty_lines,
            "error": "No upstream tracking branch configured.",
        }

    # Count ahead and behind
    p_ahead = run_git(repo_path, ["rev-list", "--count", f"{upstream}..{curr_branch}"], timeout=timeout)
    p_behind = run_git(repo_path, ["rev-list", "--count", f"{curr_branch}..{upstream}"], timeout=timeout)
    try:
        ahead = int(p_ahead.stdout.strip() or 0)
        behind = int(p_behind.stdout.strip() or 0)
    except ValueError:
        ahead = 0
        behind = 0

    is_diverged = (ahead > 0 and behind > 0)

    # Common merge base
    p_mb = run_git(repo_path, ["merge-base", curr_branch, upstream], timeout=timeout)
    merge_base = p_mb.stdout.strip() if p_mb.returncode == 0 else None

    # Local commits (ahead of upstream)
    local_commits: List[Dict[str, str]] = []
    if ahead > 0:
        p_log_local = run_git(
            repo_path,
            ["log", "--format=%h|%an|%s", f"{upstream}..{curr_branch}"],
            timeout=timeout,
        )
        for line in p_log_local.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) >= 3:
                local_commits.append({"hash": parts[0], "author": parts[1], "subject": parts[2]})
            else:
                local_commits.append({"hash": parts[0], "author": "", "subject": ""})

    # Remote commits (behind upstream)
    remote_commits: List[Dict[str, str]] = []
    if behind > 0:
        p_log_remote = run_git(
            repo_path,
            ["log", "--format=%h|%an|%s", f"{curr_branch}..{upstream}"],
            timeout=timeout,
        )
        for line in p_log_remote.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) >= 3:
                remote_commits.append({"hash": parts[0], "author": parts[1], "subject": parts[2]})
            else:
                remote_commits.append({"hash": parts[0], "author": "", "subject": ""})

    # Diff stat
    p_stat = run_git(repo_path, ["diff", "--stat", f"{curr_branch}...{upstream}"], timeout=timeout)
    diff_stat = p_stat.stdout.strip() if p_stat.returncode == 0 else ""

    return {
        "repo": repo_path,
        "branch": curr_branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "is_diverged": is_diverged,
        "merge_base": merge_base,
        "local_commits": local_commits,
        "remote_commits": remote_commits,
        "diff_stat": diff_stat,
        "is_dirty": bool(dirty_lines),
        "dirty_files": dirty_lines,
        "error": None,
    }


def split_diff_files(diff: str) -> List[str]:
    """
    Split a unified git diff into per-file diff chunks based on 'diff --git ' headers.
    Preserves all lines and chunk content.
    """
    lines = diff.splitlines(keepends=True)
    chunks: List[str] = []
    current: List[str] = []

    for line in lines:
        if line.startswith("diff --git ") and any(c.startswith("diff --git ") for c in current):
            chunks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("".join(current))
    return chunks


def truncate_diff(
    diff: str,
    max_tokens: Optional[int] = None,
    chars_per_token: int = DEFAULT_CHARS_PER_TOKEN,
) -> str:
    """
    Deterministically truncate git diff text based on a token budget (~4 chars/token).
    Preserves whole file chunks up to max_tokens, partially truncates oversized single-file
    diffs, and appends an explicit `[... truncated X more file(s), ~Y chars ...]` marker.

    # INVARIANT: Preserves whole file boundaries wherever possible to avoid emitting
    # broken syntactic chunks to agents or terminal consumers.
    # INVARIANT: If diff length <= max_tokens * chars_per_token, returns unmodified diff.
    """
    if not diff or max_tokens is None or max_tokens <= 0:
        return diff

    max_chars = max_tokens * chars_per_token
    if len(diff) <= max_chars:
        return diff

    chunks = split_diff_files(diff)
    if not chunks:
        return diff

    kept_chunks: List[str] = []
    current_len = 0
    for chunk in chunks:
        if current_len + len(chunk) <= max_chars:
            kept_chunks.append(chunk)
            current_len += len(chunk)
        else:
            break

    if kept_chunks:
        truncated_files = len(chunks) - len(kept_chunks)
        truncated_chars = len(diff) - current_len
        marker = f"[... truncated {truncated_files} more file(s), ~{truncated_chars} chars ...]"
        res = f"{''.join(kept_chunks).rstrip()}\n{marker}"
    else:
        # Oversized single-file diff or first chunk exceeds budget: partially truncate first chunk
        truncated_files = max(0, len(chunks) - 1)
        est_marker = f"\n[... truncated {truncated_files} more file(s), ~{len(diff)} chars ...]"
        target_len = max(0, max_chars - len(est_marker))
        chunk_0 = chunks[0]
        if target_len <= 0:
            partial = ""
        else:
            last_nl = chunk_0.rfind("\n", 0, target_len)
            if last_nl > 0:
                partial = chunk_0[:last_nl]
            else:
                partial = chunk_0[:target_len]

        truncated_chars = len(diff) - len(partial)
        marker = f"[... truncated {truncated_files} more file(s), ~{truncated_chars} chars ...]"
        if partial.strip():
            res = f"{partial.rstrip()}\n{marker}"
        else:
            res = marker

    if diff.endswith("\n"):
        res += "\n"
    return res


def get_divergence_diff(
    repo_path: str,
    branch: Optional[str] = None,
    stat_only: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    max_tokens: Optional[int] = None,
) -> str:
    """
    Generate diff text between local branch and remote upstream tracking branch.
    # INVARIANT: Uses symmetric three-dot diff HEAD...upstream to inspect changes introduced on remote since divergence.
    # INVARIANT: When max_tokens is provided and stat_only is False, applies deterministic
    # file-boundary aware diff truncation to protect LLM context windows and CLI outputs.
    """
    curr_branch = branch or run_git(repo_path, ["branch", "--show-current"], timeout=5).stdout.strip()
    if not curr_branch:
        curr_branch = "HEAD"

    p_up = run_git(repo_path, ["rev-parse", "--abbrev-ref", f"{curr_branch}@{{u}}"], timeout=5)
    upstream = p_up.stdout.strip()
    if p_up.returncode != 0 or not upstream:
        return ""

    cmd = ["diff"]
    if stat_only:
        cmd.append("--stat")
    cmd.append(f"{curr_branch}...{upstream}")

    p_diff = run_git(repo_path, cmd, timeout=timeout)
    diff_text = p_diff.stdout.strip()
    if max_tokens is not None and not stat_only and diff_text:
        diff_text = truncate_diff(diff_text, max_tokens=max_tokens)
    return diff_text


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
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_CHARS_PER_TOKEN",
    "DEFAULT_COMPACT_DIFF_TOKENS",
    "DEFAULT_DISK_LATENCY_THRESHOLD",
    "DEFAULT_IGNORE_DIRS",
    "DEFAULT_MAX_BACKOFF",
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_MIN_WORKERS",
    "DEFAULT_NETWORK_LATENCY_THRESHOLD",
    "DEFAULT_RETRIES",
    "DEFAULT_ROOT",
    "DEFAULT_TIMEOUT",
    "DEFAULT_WORKERS",
    "GIT_ENV",
    "TOOL_DIR_PREFIXES",
    "audit_branch_hygiene",
    "audit_ssh_multiplexing",
    "benchmark_ssh_multiplexing",
    "check_credentials_sanity",
    "check_repo_dirty",
    "delete_local_branches",
    "diagnose_git_error",
    "discover_repos",
    "find_diverged_repos",
    "find_locked_repos",
    "find_push_pending_repos",
    "find_repo_locks",
    "get_divergence_details",
    "get_divergence_diff",
    "get_repo_git_dir",
    "get_repo_remotes",
    "get_submodules_status",
    "get_worktree_info",
    "inspect_github_cli",
    "inspect_repo",
    "inspect_ssh_config",
    "is_transient_network_error",
    "match_repo",
    "prune_repo_remotes",
    "pull_repo",
    "push_repo",
    "recover_index_lock",
    "recover_repo_locks",
    "resolve_default_root",
    "resolve_divergence",
    "restore_stash",
    "run_git",
    "run_git_with_retry",
    "split_diff_files",
    "stash_changes",
    "truncate_diff",
]
