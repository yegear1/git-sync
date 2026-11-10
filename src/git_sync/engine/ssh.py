"""
OpenSSH ControlMaster multiplexing lifecycle, socket inspection, and latency benchmarking.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


def _get_git_env() -> dict[str, str]:
    from git_sync.engine.git import get_git_env
    return get_git_env()


def _diagnose_git_error(err_text: str, remote_url: str | None = None) -> dict[str, Any] | None:
    from git_sync.engine.git import diagnose_git_error
    return diagnose_git_error(err_text, remote_url)


def _parse_ssh_config_content(
    content: str, host: str = "github.com"
) -> tuple[str | None, str | None, str | None]:
    """
    Parse SSH configuration text and extract ControlMaster, ControlPath, and ControlPersist for target host.
    Follows OpenSSH first-match-wins semantics across matching Host blocks.
    # INVARIANT: Options prior to the first Host block apply globally to all hosts.
    """
    control_master: str | None = None
    control_path: str | None = None
    control_persist: str | None = None

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
    config_path: Path | str | None = None,
    host: str = "github.com",
    query_ssh_g: bool = True,
    timeout: int = 2,
) -> dict[str, Any]:
    """
    Inspect ~/.ssh/config and evaluate OpenSSH effective configuration for socket
    reuse directives (ControlMaster, ControlPath, ControlPersist).

    # INVARIANT: Directives are case-insensitive and follow first-match-wins semantics per host block.
    # INVARIANT: Validates that ControlPath directory exists to prevent runtime connection errors.
    """
    resolved_path = Path(config_path) if config_path else (Path.home() / ".ssh" / "config")
    exists = resolved_path.is_file()

    control_master: str | None = None
    control_path: str | None = None
    control_persist: str | None = None

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
                env=_get_git_env(),
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

    socket_dir: str | None = None
    socket_dir_exists = False
    if control_path and control_path.lower() != "none":
        expanded = os.path.expanduser(control_path)
        socket_dir = os.path.dirname(expanded)
        socket_dir_exists = os.path.isdir(socket_dir) if socket_dir else True

    recommendation: str | None = None
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


def inspect_ssh_control_socket(sock_path: str | Path) -> dict[str, Any]:
    """
    Inspect an SSH ControlMaster socket path and determine if it is ACTIVE, STALE, or CORRUPTED.

    # INVARIANT: Uses non-blocking AF_UNIX stream socket connect with timeout to determine if
    # an OpenSSH master process is actively listening on the socket inode.
    # INVARIANT: Defunct sockets (ECONNREFUSED) or corrupted stubs are identified without hanging.
    """
    p = Path(sock_path).expanduser()
    if not p.exists():
        return {
            "path": str(p),
            "status": "MISSING",
            "is_socket": False,
            "size": 0,
            "mtime": 0.0,
            "error": "File does not exist",
        }
    try:
        st = p.stat()
    except Exception as exc:
        return {
            "path": str(p),
            "status": "CORRUPTED",
            "is_socket": False,
            "size": 0,
            "mtime": 0.0,
            "error": str(exc),
        }

    is_socket = stat.S_ISSOCK(st.st_mode)
    if not is_socket:
        return {
            "path": str(p),
            "status": "CORRUPTED",
            "is_socket": False,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "error": "Not a valid UNIX domain socket inode (file stub)",
        }

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    status = "UNKNOWN"
    err: str | None = None
    try:
        sock.connect(str(p))
        status = "ACTIVE"
    except (ConnectionRefusedError, FileNotFoundError):
        status = "STALE"
        err = "Connection refused (no listening master process)"
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            status = "STALE"
            err = "Connection refused (no listening master process)"
        elif exc.errno in (errno.ENOENT,):
            status = "MISSING"
            err = "Socket path not found"
        else:
            status = "CORRUPTED"
            err = f"Socket error: {exc}"
    except Exception as exc:
        status = "CORRUPTED"
        err = str(exc)
    finally:
        sock.close()

    return {
        "path": str(p),
        "status": status,
        "is_socket": True,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "error": err,
    }


def find_ssh_control_sockets(
    socket_dir: str | Path | None = None,
    control_path_pattern: str | None = None,
    config_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """
    Find and inspect all SSH ControlMaster unix sockets in configured or default socket directories.

    # INVARIANT: Probes candidate files safely and whitelists socket naming patterns to prevent
    # inspecting or touching regular SSH keys, credentials, or config files.
    """
    dirs_to_check: list[Path] = []
    pattern_prefix: str | None = None

    if socket_dir:
        p = Path(socket_dir).expanduser()
        if p.is_dir():
            dirs_to_check.append(p)
    else:
        # Inquire ssh config for configured ControlPath directory
        cfg_info = inspect_ssh_config(
            config_path=config_path, query_ssh_g=False
        )
        cp = cfg_info.get("control_path")
        if cp and cp.lower() != "none":
            expanded_cp = os.path.expanduser(cp)
            d = os.path.dirname(expanded_cp)
            if d and os.path.isdir(d):
                dirs_to_check.append(Path(d))
            bname = os.path.basename(expanded_cp)
            if "%" in bname:
                pref = bname.split("%", 1)[0]
                if pref:
                    pattern_prefix = pref

    # Fallback / standard candidate directories
    ssh_home = Path.home() / ".ssh"
    std_sockets = ssh_home / "sockets"
    if std_sockets.is_dir() and std_sockets not in dirs_to_check:
        dirs_to_check.append(std_sockets)
    if ssh_home.is_dir() and ssh_home not in dirs_to_check:
        dirs_to_check.append(ssh_home)

    known_prefixes = ["control-", "control_", "cm-", "cm_", "ssh-", "socket-", "mux-", "master-"]
    if pattern_prefix and pattern_prefix not in known_prefixes:
        known_prefixes.insert(0, pattern_prefix)
    if control_path_pattern:
        b = os.path.basename(control_path_pattern)
        if "%" in b:
            pfx = b.split("%", 1)[0]
            if pfx and pfx not in known_prefixes:
                known_prefixes.insert(0, pfx)

    protected_files = {
        "config",
        "authorized_keys",
        "known_hosts",
        "known_hosts.old",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        "environment",
        "rc",
    }
    ignored_extensions = (".pub", ".pem", ".key", ".conf", ".config", ".txt", ".bak", ".old")

    found_sockets: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for d in dirs_to_check:
        try:
            entries = list(d.iterdir())
        except Exception:
            continue

        is_dedicated_sockets_dir = (
            d.name.lower() in ("sockets", "ssh_sockets") or
            (socket_dir is not None and Path(socket_dir).expanduser() == d)
        )

        for entry in entries:
            canonical = str(entry.resolve()) if entry.exists() else str(entry)
            if canonical in seen_paths:
                continue

            fname = entry.name
            fname_lower = fname.lower()

            if fname in protected_files or any(fname_lower.endswith(ext) for ext in ignored_extensions):
                continue

            try:
                st = entry.stat()
            except Exception:
                continue

            is_sock = stat.S_ISSOCK(st.st_mode)
            matches_pattern = (
                any(fname.startswith(pfx) for pfx in known_prefixes) or
                ("@" in fname and ":" in fname)
            )

            # In dedicated socket directories, accept any socket inode
            # In general directories (like ~/.ssh or /tmp), must match socket naming convention
            if is_sock:
                if is_dedicated_sockets_dir or matches_pattern:
                    seen_paths.add(canonical)
                    found_sockets.append(inspect_ssh_control_socket(entry))
            elif st.st_size == 0 and matches_pattern:
                # 0-byte corrupted stub file in socket directory
                seen_paths.add(canonical)
                found_sockets.append(inspect_ssh_control_socket(entry))

    return found_sockets


def close_ssh_control_socket(
    sock_path: str | Path,
    host: str = "github.com",
    timeout: int = 2,
) -> bool:
    """
    Gracefully terminate an active OpenSSH ControlMaster process and release its socket.
    # INVARIANT: Uses ssh -O exit with timeout to prevent blocking.
    """
    path_str = str(Path(sock_path).expanduser().resolve())
    try:
        subprocess.run(
            ["ssh", "-O", "exit", "-o", f"ControlPath={path_str}", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=_get_git_env(),
        )
    except Exception:
        pass

    if os.path.exists(path_str):
        info = inspect_ssh_control_socket(path_str)
        if info.get("status") != "ACTIVE":
            try:
                os.unlink(path_str)
            except Exception:
                pass

    return not os.path.exists(path_str)


def prune_stale_ssh_control_sockets(
    socket_dir: str | Path | None = None,
    config_path: Path | str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """
    Safely detect and prune defunct (stale) or corrupted SSH ControlMaster sockets.
    # INVARIANT: Preserves ACTIVE sockets unless force=True.
    # INVARIANT: Only removes verified unix domain sockets or 0-byte corrupted socket stubs.
    """
    sockets = find_ssh_control_sockets(socket_dir=socket_dir, config_path=config_path)

    active = [s for s in sockets if s.get("status") == "ACTIVE"]
    stale = [s for s in sockets if s.get("status") == "STALE"]
    corrupted = [s for s in sockets if s.get("status") == "CORRUPTED"]

    to_prune: list[dict[str, Any]] = list(stale) + list(corrupted)
    if force:
        for act in active:
            close_ssh_control_socket(act["path"])
            to_prune.append(act)

    pruned: list[str] = []
    errors: list[dict[str, str]] = []

    if not dry_run:
        for item in to_prune:
            p = item["path"]
            try:
                if os.path.exists(p):
                    os.unlink(p)
                pruned.append(p)
            except Exception as exc:
                errors.append({"path": p, "error": str(exc)})
    else:
        pruned = [item["path"] for item in to_prune]

    return {
        "scanned": len(sockets),
        "active": active,
        "stale": stale,
        "corrupted": corrupted,
        "pruned": pruned,
        "errors": errors,
        "dry_run": dry_run,
    }


def benchmark_ssh_multiplexing(
    remote_url: str,
    host: str = "github.com",
    sample_repo: str | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
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
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env_no_mux,
            cwd=sample_repo,
        )
        t1 = time.perf_counter()
        without_multiplexing_s = round(t1 - t0, 4)

        if proc_no_mux.returncode != 0:
            err_text = proc_no_mux.stderr.strip()
            diag = _diagnose_git_error(err_text, remote_url)
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
        diag = _diagnose_git_error(f"Timeout ({timeout}s)", remote_url)
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

        with_multiplexing_initial_s: float | None = None
        with_multiplexing_reused_s: float | None = None
        supported = False
        err_msg: str | None = None
        diag_mux: dict[str, Any] | None = None

        try:
            # First handshake (creates the master socket)
            t2 = time.perf_counter()
            proc_mux_init = subprocess.run(
                ["git", "ls-remote", "--heads", remote_url],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env_mux,
                cwd=sample_repo,
            )
            t3 = time.perf_counter()
            with_multiplexing_initial_s = round(t3 - t2, 4)

            if proc_mux_init.returncode == 0:
                # Second handshake (reuses the master socket via local unix IPC)
                t4 = time.perf_counter()
                proc_mux_reuse = subprocess.run(
                    ["git", "ls-remote", "--heads", remote_url],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env_mux,
                    cwd=sample_repo,
                )
                t5 = time.perf_counter()
                if proc_mux_reuse.returncode == 0:
                    with_multiplexing_reused_s = round(t5 - t4, 4)
                    supported = True
                else:
                    err_msg = proc_mux_reuse.stderr.strip()
            else:
                err_msg = proc_mux_init.stderr.strip()
                diag_mux = _diagnose_git_error(err_msg, remote_url)
        except subprocess.TimeoutExpired:
            err_msg = f"Timeout ({timeout}s) during multiplexed SSH probe"
            diag_mux = _diagnose_git_error(err_msg, remote_url)
        except Exception as exc:
            err_msg = str(exc)
        finally:
            # Clean up the temporary control master process and socket
            try:
                actual_socks = [
                    os.path.join(tmp_dir, f)
                    for f in os.listdir(tmp_dir)
                    if os.path.exists(os.path.join(tmp_dir, f))
                ]
                for s in actual_socks:
                    close_ssh_control_socket(s, host=host, timeout=1)
            except Exception:
                pass

    speedup: float | None = None
    savings: float | None = None
    if supported and without_multiplexing_s is not None and with_multiplexing_reused_s is not None:
        if with_multiplexing_reused_s > 0:
            speedup = round(without_multiplexing_s / with_multiplexing_reused_s, 2)
            savings = round((1.0 - (with_multiplexing_reused_s / without_multiplexing_s)) * 100.0, 1) if without_multiplexing_s > 0 else 0.0
        else:
            speedup = round(without_multiplexing_s / 0.0001, 2) if without_multiplexing_s > 0 else 1.0
            savings = 100.0 if without_multiplexing_s > 0 else 0.0

    return {
        "host": host,
        "remote_url": remote_url,
        "supported": supported,
        "error": err_msg.splitlines()[-1] if err_msg else None,
        "diagnostic": diag_mux,
        "without_multiplexing_seconds": without_multiplexing_s,
        "with_multiplexing_initial_seconds": with_multiplexing_initial_s,
        "with_multiplexing_reused_seconds": with_multiplexing_reused_s,
        "speedup_factor": speedup,
        "savings_pct": savings,
    }


def audit_ssh_multiplexing(
    root_dir: str | None = None,
    repos: list[str] | None = None,
    group: str | None = None,
    timeout: int = 10,
    config_path: Path | str | None = None,
    run_benchmark: bool = True,
    target_host: str | None = None,
) -> dict[str, Any]:
    """
    Execute full SSH multiplexing diagnostic audit and latency benchmark.
    # INVARIANT: Probing strictly respects GIT_TERMINAL_PROMPT=0 and never blocks.
    """
    from git_sync.engine.git import DEFAULT_ROOT
    effective_root = root_dir or DEFAULT_ROOT
    target_repos = repos
    if target_repos is None:
        from git_sync.engine.git import discover_repos
        target_repos = discover_repos(effective_root)
        if group:
            target_repos = [r for r in target_repos if os.path.relpath(r, effective_root).startswith(group)]

    # Collect unique SSH remotes
    ssh_remotes: dict[str, tuple[str, str]] = {}  # host -> (remote_url, sample_repo)
    from git_sync.engine.git import get_repo_remotes

    for r_path in target_repos:
        r_map = get_repo_remotes(r_path, timeout=timeout)
        for r_url in r_map.values():
            if r_url and ("@" in r_url or r_url.startswith("ssh://")):
                host = ""
                if "@" in r_url and ":" in r_url:
                    host = r_url.split("@", 1)[1].split(":", 1)[0]
                elif "://" in r_url:
                    host = r_url.split("://", 1)[1].split("/", 1)[0]
                host = host or "github.com"
                if host not in ssh_remotes:
                    ssh_remotes[host] = (r_url, r_path)

    primary_host = target_host or (next(iter(ssh_remotes.keys())) if ssh_remotes else "github.com")
    config_info = inspect_ssh_config(
        config_path=config_path, host=primary_host
    )
    socket_report = prune_stale_ssh_control_sockets(
        socket_dir=config_info.get("socket_dir"),
        config_path=config_path,
        dry_run=False,
    )

    benchmark_result: dict[str, Any] | None = None
    if run_benchmark:
        if ssh_remotes:
            sample_url, sample_repo = (
                ssh_remotes[primary_host]
                if primary_host in ssh_remotes
                else next(iter(ssh_remotes.values()))
            )
            benchmark_result = benchmark_ssh_multiplexing(
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

    warnings: list[str] = []
    suggestions: list[str] = []

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

    if socket_report and socket_report.get("pruned"):
        warnings.append(
            f"Pruned {len(socket_report['pruned'])} stale/defunct SSH ControlMaster socket(s)."
        )

    ok = config_info.get("is_enabled", False) and config_info.get("socket_dir_exists", False)
    if benchmark_result and benchmark_result.get("supported"):
        ok = True

    return {
        "ok": ok,
        "config": config_info,
        "primary_host": primary_host,
        "ssh_remotes_found": len(ssh_remotes),
        "benchmark": benchmark_result,
        "sockets": socket_report,
        "warnings": warnings,
        "suggestions": suggestions,
    }


__all__ = [
    "_parse_ssh_config_content",
    "audit_ssh_multiplexing",
    "benchmark_ssh_multiplexing",
    "close_ssh_control_socket",
    "find_ssh_control_sockets",
    "inspect_ssh_config",
    "inspect_ssh_control_socket",
    "prune_stale_ssh_control_sockets",
]
