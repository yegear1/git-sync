"""
Git credential helper triage, token environment validation, remote reachability probing,
and proactive credentials sanity diagnostics.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from git_sync.engine.concurrency import DEFAULT_WORKERS

GRADE_A_THRESHOLD_SECONDS: float = 0.50
GRADE_B_THRESHOLD_SECONDS: float = 1.50
GRADE_C_THRESHOLD_SECONDS: float = 3.00


def _dispatch_fn(name: str, fallback_fn: Any = None) -> Any:
    """
    Look up symbol dynamically from git_sync.core or git_sync.engine.git to respect unit test mocks.
    """
    for mod_name in (
        "git_sync.engine.git",
        "git_sync.core",
        "git_sync.engine.credentials",
        "git_sync.engine.ssh",
        "git_sync.engine.changelog",
        "git_sync.engine.diff",
        "git_sync.engine.topology",
    ):
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, name):
            val = getattr(mod, name)
            if hasattr(val, "mock") or hasattr(val, "assert_called") or "Mock" in type(val).__name__:
                return val

    mod = sys.modules.get("git_sync.engine.git") or sys.modules.get("git_sync.core")
    if mod and hasattr(mod, name):
        return getattr(mod, name)
    if fallback_fn is not None:
        return fallback_fn
    cur_mod = sys.modules.get(__name__)
    if cur_mod and hasattr(cur_mod, name):
        return getattr(cur_mod, name)
    raise AttributeError(f"Function {name} not found in dispatch modules")


def _get_git_env() -> Dict[str, str]:
    mod = sys.modules.get("git_sync.engine.git") or sys.modules.get("git_sync.core")
    if mod and hasattr(mod, "get_git_env"):
        return mod.get_git_env()
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def compute_latency_grade(rtt_seconds: Optional[float], ok: bool = True) -> str:
    """
    Compute latency performance grade (A/B/C/F) for remote Git handshake round-trip times.
    # INVARIANT: Failed or timed out probes unconditionally receive grade 'F'.

    Grade Scale:
      - A: < 0.50s (Fast / multiplexed socket / local fiber)
      - B: 0.50s - 1.50s (Good / standard unmultiplexed remote handshake)
      - C: 1.50s - 3.00s (Degraded / DNS latency or network congestion)
      - F: >= 3.00s or probe failure / timeout (Failing SLA / severe bottleneck)
    """
    if not ok or rtt_seconds is None or rtt_seconds < 0:
        return "F"
    if rtt_seconds < GRADE_A_THRESHOLD_SECONDS:
        return "A"
    if rtt_seconds < GRADE_B_THRESHOLD_SECONDS:
        return "B"
    if rtt_seconds < GRADE_C_THRESHOLD_SECONDS:
        return "C"
    return "F"


def inspect_github_cli(timeout: int = 3) -> Dict[str, Any]:
    """
    Inspect local GitHub CLI (gh) installation, authentication state, and token expiry status.
    # INVARIANT: Uses short execution timeout to prevent blocking during doctor checks.
    # INVARIANT: Never exposes raw authentication tokens in returned structures.
    """
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return {
            "installed": False,
            "path": None,
            "logged_in": False,
            "token_expired": False,
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
            env=_get_git_env(),
        )
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")

        is_expired = False
        expired_patterns = [
            "token has expired",
            "token is expired",
            "invalid token",
            "authentication failed",
            "bad credentials",
            "oauth token expired",
            "refresh token",
        ]
        if any(p in combined.lower() for p in expired_patterns):
            is_expired = True

        if proc.returncode == 0 and not is_expired:
            account: Optional[str] = None
            host: Optional[str] = None
            protocol: Optional[str] = None
            scopes: List[str] = []

            m_account = re.search(r"account\s+([a-zA-Z0-9_\-]+)", combined, re.IGNORECASE)
            if m_account:
                account = m_account.group(1)

            m_host = re.search(r"Logged in to\s+([a-zA-Z0-9_.-]+)", combined, re.IGNORECASE)
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
                "token_expired": False,
                "active_account": account,
                "host": host,
                "protocol": protocol,
                "token_scopes": scopes,
                "error": None,
                "suggestion": None,
            }
        else:
            err_line = combined.strip().splitlines()[-1] if combined.strip() else "Not logged in"
            suggestion = (
                "Refresh or re-authenticate your expired GitHub CLI token using 'gh auth refresh' or 'gh auth login'."
                if is_expired
                else "Authenticate with GitHub using 'gh auth login'."
            )
            return {
                "installed": True,
                "path": gh_bin,
                "logged_in": False,
                "token_expired": is_expired,
                "active_account": None,
                "host": None,
                "protocol": None,
                "token_scopes": [],
                "error": err_line,
                "suggestion": suggestion,
            }
    except subprocess.TimeoutExpired:
        return {
            "installed": True,
            "path": gh_bin,
            "logged_in": False,
            "token_expired": False,
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
            "token_expired": False,
            "active_account": None,
            "host": None,
            "protocol": None,
            "token_scopes": [],
            "error": str(exc),
            "suggestion": "Run 'gh auth status' manually to diagnose.",
        }


def inspect_token_env(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Inspect environment variables for Git authentication tokens (GITHUB_TOKEN, GH_TOKEN, GITLAB_TOKEN)
    and validate format without leaking secret token values.
    # INVARIANT: Raw token values MUST NEVER be returned or logged.
    """
    eff_env = env if env is not None else os.environ
    token_vars = ["GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN"]
    present_vars: List[str] = []
    primary_val: Optional[str] = None

    for var in token_vars:
        val = eff_env.get(var)
        if val is not None and val != "":
            present_vars.append(var)
            if primary_val is None:
                primary_val = val.strip()

    if not present_vars or primary_val is None:
        return {
            "has_token": False,
            "variables": [],
            "token_format": None,
            "valid_format": False,
            "warning": None,
            "suggestion": None,
        }

    low = primary_val.lower()
    placeholders = {"todo", "changeme", "your_token", "<token>", "null", "none", "expired", "revoked", "xxx", "test"}
    if low in placeholders or len(primary_val) < 8 or " " in primary_val:
        return {
            "has_token": True,
            "variables": present_vars,
            "token_format": "invalid_or_placeholder",
            "valid_format": False,
            "warning": f"Environment token ({', '.join(present_vars)}) contains a placeholder, expired marker, or malformed value.",
            "suggestion": f"Export a valid Personal Access Token in {present_vars[0]} or unset it to use your Git credential helper.",
        }

    if primary_val.startswith("github_pat_"):
        fmt = "github_fine_grained_pat"
    elif primary_val.startswith("ghp_"):
        fmt = "github_classic_pat"
    elif primary_val.startswith("gho_"):
        fmt = "github_oauth_token"
    elif primary_val.startswith("ghu_"):
        fmt = "github_user_to_server_token"
    elif primary_val.startswith("ghs_"):
        fmt = "github_server_to_server_token"
    elif primary_val.startswith("ghr_"):
        fmt = "github_refresh_token"
    elif primary_val.startswith("glpat-"):
        fmt = "gitlab_pat"
    else:
        fmt = "generic_token"

    return {
        "has_token": True,
        "variables": present_vars,
        "token_format": fmt,
        "valid_format": True,
        "warning": None,
        "suggestion": None,
    }


def _resolve_credential_helper_binary(
    helper_type: str,
    raw_spec: str,
) -> Optional[str]:
    """
    Resolve the executable path for an OS Git credential helper (libsecret, osxkeychain, wincred, manager).
    # INVARIANT: Checks explicit file paths, PATH, and standard Git exec-path directories without spawning subshells.
    """
    clean_spec = raw_spec.lstrip("!").strip()
    if clean_spec:
        first_token = clean_spec.split()[0]
        if os.path.isabs(first_token) and os.path.isfile(first_token) and os.access(first_token, os.X_OK):
            return first_token
        w = shutil.which(first_token)
        if w:
            return w

    direct = shutil.which(f"git-credential-{helper_type}")
    if direct:
        return direct

    # Query git --exec-path for libexec helper binaries
    try:
        proc = subprocess.run(
            ["git", "--exec-path"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            env=_get_git_env(),
        )
        if proc.returncode == 0:
            exec_dir = Path(proc.stdout.strip())
            candidate = exec_dir / f"git-credential-{helper_type}"
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                return str(candidate)
    except Exception:
        pass

    common_paths = [
        Path("/usr/lib/git-core") / f"git-credential-{helper_type}",
        Path("/usr/libexec/git-core") / f"git-credential-{helper_type}",
        Path("/usr/local/git/bin") / f"git-credential-{helper_type}",
        Path("/opt/homebrew/bin") / f"git-credential-{helper_type}",
    ]
    for cp in common_paths:
        if cp.is_file() and os.access(str(cp), os.X_OK):
            return str(cp)

    return None


def inspect_credential_helpers(
    helpers: Optional[List[str]] = None,
    timeout: int = 3,
    gh_info: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
    platform: Optional[str] = None,
    home_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Perform fine-grained triage of configured Git credential helpers (libsecret, osxkeychain,
    wincred, manager, store, cache, gh) and detect expired cache sockets, missing helper
    binaries, headless keyring lockouts, or insecure credential stores.

    # INVARIANT: Never exposes plaintext passwords or tokens from credential stores.
    # INVARIANT: Non-blocking socket and inode checks guarantee zero hang risk.
    """
    eff_env = env if env is not None else os.environ
    eff_platform = (platform or sys.platform).lower()
    eff_home = Path(home_dir) if home_dir is not None else Path.home()

    raw_list: List[str] = []
    if helpers is not None:
        raw_list = list(helpers)
    else:
        try:
            p_helper = subprocess.run(
                ["git", "config", "--get-all", "credential.helper"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                env=eff_env if eff_env is not None else _get_git_env(),
            )
            if p_helper.returncode == 0:
                raw_list = [h.strip() for h in p_helper.stdout.splitlines()]
        except Exception:
            raw_list = []

    token_report = inspect_token_env(env=eff_env)

    warnings: List[str] = []
    errors: List[str] = []
    suggestions: List[str] = []

    if token_report.get("warning"):
        warnings.append(token_report["warning"])
    if token_report.get("suggestion"):
        suggestions.append(token_report["suggestion"])

    # Compute effective helper chain (an empty string "" in credential.helper resets prior entries)
    effective_raw: List[str] = []
    chain_reset = False
    for raw_item in raw_list:
        stripped = raw_item.strip()
        if stripped == "":
            effective_raw = []
            chain_reset = True
        else:
            effective_raw.append(stripped)

    helper_reports: List[Dict[str, Any]] = []
    expired_caches: List[str] = []
    missing_binaries: List[str] = []
    insecure_stores: List[str] = []

    for h_raw in effective_raw:
        h_low = h_raw.lower()
        entry: Dict[str, Any] = {
            "raw": h_raw,
            "type": "custom",
            "status": "HEALTHY",
            "ok": True,
        }

        # 1. Git credential-cache
        if h_low == "cache" or h_low.startswith("cache ") or "git-credential-cache" in h_low:
            entry["type"] = "cache"
            m_timeout = re.search(r"--timeout(?:=|\s+)(\d+)", h_raw)
            cache_timeout = int(m_timeout.group(1)) if m_timeout else 900
            entry["timeout_seconds"] = cache_timeout

            m_sock = re.search(r"--socket(?:=|\s+)([^\s\"']+)", h_raw)
            if m_sock:
                sock_candidates = [Path(os.path.expanduser(m_sock.group(1)))]
            else:
                sock_candidates = []
                xdg_cache = eff_env.get("XDG_CACHE_HOME")
                if xdg_cache:
                    sock_candidates.append(Path(xdg_cache) / "git" / "credential" / "socket")
                sock_candidates.append(eff_home / ".cache" / "git" / "credential" / "socket")
                sock_candidates.append(eff_home / ".git-credential-cache" / "socket")

            existing_sock: Optional[Path] = None
            for cand in sock_candidates:
                if cand.exists():
                    existing_sock = cand
                    break

            if existing_sock is not None:
                entry["socket_path"] = str(existing_sock)
                try:
                    st = existing_sock.stat()
                    age_s = round(max(0.0, time.time() - st.st_mtime), 2)
                    entry["age_seconds"] = age_s
                    is_sock = stat.S_ISSOCK(st.st_mode)
                except Exception:
                    age_s = 0.0
                    is_sock = False

                sock_alive = False
                if is_sock:
                    s_probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s_probe.settimeout(0.2)
                    try:
                        s_probe.connect(str(existing_sock))
                        sock_alive = True
                    except Exception:
                        sock_alive = False
                    finally:
                        s_probe.close()

                if not is_sock or not sock_alive or age_s > cache_timeout:
                    entry["socket_status"] = "EXPIRED"
                    entry["status"] = "EXPIRED"
                    entry["ok"] = False
                    expired_caches.append(str(existing_sock))
                    w_msg = (
                        f"Git credential-cache socket '{existing_sock}' has expired or daemon is defunct "
                        f"(age: {age_s:.0f}s, timeout: {cache_timeout}s)."
                    )
                    s_msg = (
                        "Re-authenticate or increase credential cache TTL via "
                        "'git config --global credential.helper \"cache --timeout=3600\"'."
                    )
                    if w_msg not in warnings:
                        warnings.append(w_msg)
                    if s_msg not in suggestions:
                        suggestions.append(s_msg)
                else:
                    entry["socket_status"] = "ACTIVE"
            else:
                entry["socket_path"] = str(sock_candidates[0]) if sock_candidates else None
                entry["socket_status"] = "IDLE"

            if cache_timeout < 300:
                if entry["status"] == "HEALTHY":
                    entry["status"] = "WARNING"
                w_short = (
                    f"Git credential-cache timeout is very short ({cache_timeout}s < 300s), "
                    "which may cause frequent token expirations during bulk operations."
                )
                s_short = "Increase credential cache timeout via 'git config --global credential.helper \"cache --timeout=3600\"'."
                if w_short not in warnings:
                    warnings.append(w_short)
                if s_short not in suggestions:
                    suggestions.append(s_short)

        # 2. Git credential-store
        elif h_low == "store" or h_low.startswith("store ") or "git-credential-store" in h_low:
            entry["type"] = "store"
            m_file = re.search(r"--file(?:=|\s+)([^\s\"']+)", h_raw)
            if m_file:
                store_candidates = [Path(os.path.expanduser(m_file.group(1)))]
            else:
                xdg_cfg = eff_env.get("XDG_CONFIG_HOME")
                cfg_base = Path(xdg_cfg) if xdg_cfg else (eff_home / ".config")
                store_candidates = [
                    eff_home / ".git-credentials",
                    cfg_base / "git" / "credentials",
                ]

            existing_store: Optional[Path] = None
            for cand in store_candidates:
                if cand.is_file():
                    existing_store = cand
                    break

            if existing_store is not None:
                entry["store_file"] = str(existing_store)
                try:
                    st = existing_store.stat()
                    mode = st.st_mode & 0o777
                    entry["file_mode"] = oct(mode)
                    if eff_platform != "win32" and (mode & 0o077) != 0:
                        entry["insecure_permissions"] = True
                        entry["status"] = "INSECURE_PERMISSIONS"
                        insecure_stores.append(str(existing_store))
                        w_perm = (
                            f"Plaintext credential store '{existing_store}' has overly permissive file mode "
                            f"({oct(mode)}); credentials are readable by other local users."
                        )
                        s_perm = f"Restrict credential store permissions via 'chmod 600 {existing_store}'."
                        if w_perm not in warnings:
                            warnings.append(w_perm)
                        if s_perm not in suggestions:
                            suggestions.append(s_perm)
                    else:
                        entry["insecure_permissions"] = False
                except Exception:
                    entry["insecure_permissions"] = False

                hosts: List[str] = []
                entries_count = 0
                has_invalid_token = False
                try:
                    lines = existing_store.read_text(encoding="utf-8", errors="replace").splitlines()
                    for raw_line in lines:
                        line = raw_line.strip()
                        if not line or line.startswith("#"):
                            continue
                        entries_count += 1
                        m_cred = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^:@/]*):([^@/]*)@([^/\s]+)", line)
                        if m_cred:
                            _user, secret, host_part = m_cred.group(1), m_cred.group(2), m_cred.group(3)
                            if host_part and host_part not in hosts:
                                hosts.append(host_part)
                            sec_low = secret.strip().lower()
                            if not sec_low or sec_low in ("todo", "changeme", "your_token", "<token>", "expired", "revoked", "null", "none"):
                                has_invalid_token = True
                except Exception:
                    pass

                entry["entries_count"] = entries_count
                entry["hosts"] = hosts
                if has_invalid_token:
                    entry["status"] = "EXPIRED"
                    entry["ok"] = False
                    w_inv = f"Credential store '{existing_store}' contains an empty, placeholder, or expired token entry."
                    s_inv = f"Update expired or placeholder credentials in '{existing_store}' or re-authenticate via 'gh auth login'."
                    if w_inv not in warnings:
                        warnings.append(w_inv)
                    if s_inv not in suggestions:
                        suggestions.append(s_inv)
                elif entries_count == 0:
                    entry["status"] = "EMPTY_STORE"
                    w_empty = f"Git credential store '{existing_store}' is empty (0 saved credentials)."
                    s_empty = f"Populate credentials in '{existing_store}' or authenticate via 'gh auth login'."
                    if w_empty not in warnings:
                        warnings.append(w_empty)
                    if s_empty not in suggestions:
                        suggestions.append(s_empty)
            else:
                target_f = str(store_candidates[0])
                entry["store_file"] = target_f
                entry["entries_count"] = 0
                entry["hosts"] = []
                entry["status"] = "EMPTY_STORE"
                w_missing = f"Git credential.helper 'store' is configured, but '{target_f}' does not exist."
                s_missing = "Authenticate once interactively to populate ~/.git-credentials or run 'gh auth login'."
                if w_missing not in warnings:
                    warnings.append(w_missing)
                if s_missing not in suggestions:
                    suggestions.append(s_missing)

        # 3. GitHub CLI credential helper (!gh auth git-credential or gh)
        elif "gh auth git-credential" in h_low or h_low == "gh" or h_low.endswith("/gh"):
            entry["type"] = "gh"
            eff_gh = gh_info if gh_info is not None else _dispatch_fn("inspect_github_cli", inspect_github_cli)(timeout=timeout)
            if not eff_gh.get("installed"):
                entry["status"] = "MISSING_BINARY"
                entry["ok"] = False
                missing_binaries.append("gh")
                w_gh = "Git credential.helper references GitHub CLI ('gh'), but 'gh' binary was not found on PATH."
                s_gh = "Install GitHub CLI (https://cli.github.com) or update credential.helper config."
                if w_gh not in warnings:
                    warnings.append(w_gh)
                if s_gh not in suggestions:
                    suggestions.append(s_gh)
            elif eff_gh.get("token_expired"):
                entry["status"] = "EXPIRED"
                entry["ok"] = False
                w_gh_exp = "GitHub CLI ('gh') credential helper is configured, but its token has expired or been revoked."
                s_gh_exp = "Refresh your expired GitHub CLI token via 'gh auth refresh' or 'gh auth login'."
                if w_gh_exp not in warnings:
                    warnings.append(w_gh_exp)
                if s_gh_exp not in suggestions:
                    suggestions.append(s_gh_exp)
            elif not eff_gh.get("logged_in"):
                entry["status"] = "UNAUTHENTICATED"
                entry["ok"] = False
                w_gh_unauth = "GitHub CLI ('gh') credential helper is configured, but 'gh' is not authenticated."
                s_gh_unauth = "Authenticate GitHub CLI with 'gh auth login'."
                if w_gh_unauth not in warnings:
                    warnings.append(w_gh_unauth)
                if s_gh_unauth not in suggestions:
                    suggestions.append(s_gh_unauth)

        # 4. OS Keychain Helpers (libsecret, osxkeychain, wincred, manager)
        elif any(k in h_low for k in ("libsecret", "osxkeychain", "wincred", "manager", "gcm")):
            if "libsecret" in h_low:
                h_type = "libsecret"
            elif "osxkeychain" in h_low:
                h_type = "osxkeychain"
            elif "wincred" in h_low:
                h_type = "wincred"
            else:
                h_type = "manager"
            entry["type"] = h_type

            platform_ok = True
            if h_type == "osxkeychain" and "darwin" not in eff_platform:
                platform_ok = False
            elif h_type == "wincred" and not any(p in eff_platform for p in ("win", "cygwin", "msys")):
                is_wsl = False
                try:
                    if hasattr(os, "uname") and "microsoft" in os.uname().release.lower():
                        is_wsl = True
                except Exception:
                    pass
                if not is_wsl:
                    platform_ok = False
            elif h_type == "libsecret" and ("darwin" in eff_platform or "win" in eff_platform):
                platform_ok = False

            if not platform_ok:
                entry["status"] = "UNSUPPORTED_PLATFORM"
                entry["ok"] = False
                w_plat = f"OS credential helper '{h_type}' is not supported on current platform ({eff_platform})."
                s_plat = "Switch to a platform-native credential helper (e.g. 'gh auth setup-git', 'cache', or OS native keychain)."
                if w_plat not in warnings:
                    warnings.append(w_plat)
                if s_plat not in suggestions:
                    suggestions.append(s_plat)
            else:
                bin_path = _resolve_credential_helper_binary(h_type, h_raw)
                entry["binary_path"] = bin_path
                if not bin_path:
                    entry["status"] = "MISSING_BINARY"
                    entry["ok"] = False
                    missing_binaries.append(f"git-credential-{h_type}")
                    w_bin = f"OS credential helper '{h_type}' is configured, but 'git-credential-{h_type}' executable was not found."
                    s_bin = (
                        f"Install 'git-credential-{h_type}' or switch credential helper via "
                        "'gh auth setup-git' / 'git config --global credential.helper cache'."
                    )
                    if w_bin not in warnings:
                        warnings.append(w_bin)
                    if s_bin not in suggestions:
                        suggestions.append(s_bin)
                elif h_type == "libsecret" and "linux" in eff_platform:
                    dbus_addr = eff_env.get("DBUS_SESSION_BUS_ADDRESS")
                    entry["dbus_session"] = bool(dbus_addr)
                    if not dbus_addr:
                        entry["status"] = "KEYCHAIN_LOCKED_OR_HEADLESS"
                        w_dbus = (
                            "Credential helper 'libsecret' is configured in a headless session without "
                            "DBUS_SESSION_BUS_ADDRESS; GNOME Keyring / Secret Service may be locked or unreachable."
                        )
                        s_dbus = (
                            "Ensure D-Bus user session and gnome-keyring-daemon are unlocked, or use "
                            "'gh auth setup-git' / 'credential.helper cache' for headless environments."
                        )
                        if w_dbus not in warnings:
                            warnings.append(w_dbus)
                        if s_dbus not in suggestions:
                            suggestions.append(s_dbus)

        # 5. Custom helper script or binary
        else:
            entry["type"] = "custom"
            bin_path = _resolve_credential_helper_binary(h_raw, h_raw)
            entry["binary_path"] = bin_path
            if bin_path is None and (h_raw.startswith("/") or h_raw.startswith("~") or h_raw.startswith("!")):
                entry["status"] = "MISSING_BINARY"
                entry["ok"] = False
                missing_binaries.append(h_raw)
                w_cust = f"Custom Git credential helper '{h_raw}' executable was not found or is not executable."
                s_cust = "Verify the path in 'git config --list --show-origin | grep credential.helper'."
                if w_cust not in warnings:
                    warnings.append(w_cust)
                if s_cust not in suggestions:
                    suggestions.append(s_cust)

        helper_reports.append(entry)

    return {
        "configured": len(effective_raw) > 0,
        "chain_reset": chain_reset,
        "raw_helpers": [h for h in raw_list if h],
        "active_helpers": effective_raw,
        "helpers": helper_reports,
        "expired_caches": expired_caches,
        "missing_binaries": missing_binaries,
        "insecure_stores": insecure_stores,
        "token_env": token_report,
        "warnings": warnings,
        "errors": errors,
        "suggestions": suggestions,
    }


def probe_remote_reachability(
    remote_url: str,
    sample_repo: Optional[str] = None,
    timeout: int = 5,
) -> Dict[str, Any]:
    """
    Probe single remote reachability, measuring handshake RTT and computing latency grade.
    # INVARIANT: Uses GIT_TERMINAL_PROMPT=0 to guarantee probing never hangs in automated environments.
    """
    from git_sync.engine.git import run_git, diagnose_git_error

    is_ssh = "@" in remote_url or remote_url.startswith("ssh://")
    protocol = "ssh" if is_ssh else "https"
    host = ""
    if "@" in remote_url and ":" in remote_url:
        host = remote_url.split("@", 1)[1].split(":", 1)[0]
    elif "://" in remote_url:
        host = remote_url.split("://", 1)[1].split("/", 1)[0]

    t0 = time.perf_counter()
    try:
        proc = run_git(sample_repo or ".", ["ls-remote", "--heads", remote_url], timeout=timeout)
        rtt = round(time.perf_counter() - t0, 3)
        if proc.returncode == 0:
            return {
                "ok": True,
                "protocol": protocol,
                "host": host,
                "latency_seconds": rtt,
                "grade": compute_latency_grade(rtt, ok=True),
                "error": None,
                "diagnostic": None,
            }
        else:
            err_text = proc.stderr.strip()
            if is_ssh and err_text and (
                "control socket" in err_text.lower()
                or ("controlpath" in err_text.lower() and "connection refused" in err_text.lower())
            ):
                try:
                    from git_sync.engine.ssh import prune_stale_ssh_control_sockets
                    _dispatch_fn("prune_stale_ssh_control_sockets", prune_stale_ssh_control_sockets)()
                    proc_retry = run_git(sample_repo or ".", ["ls-remote", "--heads", remote_url], timeout=timeout)
                    if proc_retry.returncode == 0:
                        rtt = round(time.perf_counter() - t0, 3)
                        return {
                            "ok": True,
                            "protocol": protocol,
                            "host": host,
                            "latency_seconds": rtt,
                            "grade": compute_latency_grade(rtt, ok=True),
                            "error": None,
                            "diagnostic": None,
                            "socket_recovered": True,
                        }
                    else:
                        proc = proc_retry
                        err_text = proc.stderr.strip()
                except Exception:
                    pass
            diag = diagnose_git_error(err_text, remote_url)
            return {
                "ok": False,
                "protocol": protocol,
                "host": host,
                "latency_seconds": rtt,
                "grade": "F",
                "error": err_text.splitlines()[-1] if err_text else "Authentication probe failed",
                "diagnostic": diag,
            }
    except subprocess.TimeoutExpired:
        rtt = round(time.perf_counter() - t0, 3)
        diag = diagnose_git_error(f"Timeout ({timeout}s)", remote_url)
        return {
            "ok": False,
            "protocol": protocol,
            "host": host,
            "latency_seconds": rtt,
            "grade": "F",
            "error": f"Timeout ({timeout}s) during ls-remote probe",
            "diagnostic": diag,
        }
    except Exception as exc:
        rtt = round(time.perf_counter() - t0, 3)
        return {
            "ok": False,
            "protocol": protocol,
            "host": host,
            "latency_seconds": rtt,
            "grade": "F",
            "error": str(exc),
            "diagnostic": None,
        }


def check_credentials_sanity(
    root_dir: str,
    repos: Optional[List[str]] = None,
    group: Optional[str] = None,
    timeout: int = 5,
    test_remotes: bool = True,
    max_remotes_to_test: int = 15,
    workers: int | str = DEFAULT_WORKERS,
) -> Dict[str, Any]:
    """
    Perform a proactive sanity check of Git credentials, SSH agent, key files,
    multi-remote reachability matrix, and worktree health across repositories in root_dir.
    # INVARIANT: Uses GIT_TERMINAL_PROMPT=0 to guarantee probing never hangs.
    # INVARIANT: Groups and tests unique remotes to avoid redundant network probes.
    """
    from git_sync.engine.git import (
        audit_multi_remote_matrix,
        audit_worktree_health_batch,
        discover_repos,
        find_locked_repos,
        get_lfs_info,
        inspect_git_lfs,
    )
    from git_sync.engine.ssh import inspect_ssh_config, prune_stale_ssh_control_sockets

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
            env=_get_git_env(),
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
    raw_helper_lines: List[str] = []
    credential_helpers: List[str] = []
    try:
        p_helper = subprocess.run(
            ["git", "config", "--get-all", "credential.helper"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=_get_git_env(),
        )
        if p_helper.returncode == 0:
            raw_helper_lines = [h.strip() for h in p_helper.stdout.splitlines()]
            credential_helpers = [h for h in raw_helper_lines if h]
    except Exception:
        pass

    has_token_env = bool(
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITLAB_TOKEN")
        or os.environ.get("BITBUCKET_TOKEN")
    )

    # 3.5 GitHub CLI (gh) Status & Fine-Grained Credential Helper Triage
    gh_info = _dispatch_fn("inspect_github_cli", inspect_github_cli)(timeout=timeout)
    cred_diag = _dispatch_fn("inspect_credential_helpers", inspect_credential_helpers)(
        helpers=raw_helper_lines,
        timeout=timeout,
        gh_info=gh_info,
    )

    target_repos = repos
    if target_repos is None:
        target_repos = _dispatch_fn("discover_repos", discover_repos)(root_dir)
        if group:
            target_repos = [r for r in target_repos if os.path.relpath(r, root_dir).startswith(group)]

    # 4. Multi-Remote Matrix & Reachability Probing
    multi_remote_matrix: Dict[str, Any] = {}
    remotes_tested: Dict[str, Dict[str, Any]] = {}
    if test_remotes:
        multi_matrix_fn = _dispatch_fn("audit_multi_remote_matrix", audit_multi_remote_matrix)
        multi_remote_matrix = multi_matrix_fn(
            root_dir=root_dir,
            repos=target_repos,
            group=group,
            timeout=timeout,
            workers=workers,
            max_remotes_to_test=max_remotes_to_test,
        )
        remotes_tested = multi_remote_matrix.get("probed_remotes", {})

    # 5. Worktree Health Audit
    wt_batch_fn = _dispatch_fn("audit_worktree_health_batch", audit_worktree_health_batch)
    worktree_health = wt_batch_fn(
        root_dir=root_dir,
        repos=target_repos,
        group=group,
        timeout=timeout,
        workers=workers,
    )

    warnings: List[str] = []
    errors: List[str] = []
    suggestions: List[str] = []

    has_ssh_remotes = any("@" in u or u.startswith("ssh://") for u in remotes_tested)
    has_http_remotes = any(u.startswith("http://") or u.startswith("https://") for u in remotes_tested)

    ssh_mux_host = "github.com"
    for u in remotes_tested:
        if "@" in u and ":" in u:
            ssh_mux_host = u.split("@", 1)[1].split(":", 1)[0]
            break
        elif "://" in u and ("@" in u or u.startswith("ssh://")):
            ssh_mux_host = u.split("://", 1)[1].split("/", 1)[0]
            break

    ssh_mux_info = _dispatch_fn("inspect_ssh_config", inspect_ssh_config)(host=ssh_mux_host)
    socket_report = _dispatch_fn("prune_stale_ssh_control_sockets", prune_stale_ssh_control_sockets)(
        socket_dir=ssh_mux_info.get("socket_dir")
    )
    ssh_mux_info["sockets"] = socket_report
    ssh_mux_info["stale_sockets_pruned"] = socket_report.get("pruned", [])
    ssh_mux_info["active_sockets"] = [s["path"] for s in socket_report.get("active", [])]

    if socket_report.get("pruned"):
        suggestions.append(
            f"Pruned {len(socket_report['pruned'])} stale SSH ControlMaster socket(s) to maintain socket health."
        )

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
        if gh_info.get("token_expired"):
            warnings.append("GitHub CLI (gh) authentication token has expired or is invalid.")
            suggestions.append("Refresh your expired GitHub CLI token with 'gh auth refresh' or 'gh auth login'.")
        else:
            warnings.append("GitHub CLI (gh) is installed but not authenticated.")
            suggestions.append("Authenticate GitHub CLI with 'gh auth login'.")

    for c_warn in cred_diag.get("warnings", []):
        if c_warn not in warnings:
            warnings.append(c_warn)
    for c_sug in cred_diag.get("suggestions", []):
        if c_sug not in suggestions:
            suggestions.append(c_sug)

    if has_http_remotes and not credential_helpers and not has_token_env and not gh_logged_in:
        warnings.append("HTTP remotes detected without configured Git credential helper, GITHUB_TOKEN, or authenticated GitHub CLI (gh).")
        suggestions.append("Configure credential helper via 'git config --global credential.helper store' or authenticate via 'gh auth login'.")

    for r_url, r_data in remotes_tested.items():
        host = r_data.get("host") or r_url
        grade = r_data.get("grade", "F")
        latency = r_data.get("latency_seconds")
        protocol = r_data.get("protocol", "https")

        if not r_data.get("ok"):
            diag = r_data.get("diagnostic")
            if diag:
                err_msg = f"{host or r_url}: {diag['message']}"
                if err_msg not in errors:
                    errors.append(err_msg)
                if diag.get("suggestion") and diag["suggestion"] not in suggestions:
                    suggestions.append(diag["suggestion"])
                if diag.get("category") in ("HTTP_AUTH_FAILED", "TOKEN_EXPIRED") and credential_helpers:
                    evict_sug = (
                        f"Evict stale cached credentials for '{host}' via "
                        f"'printf \"protocol=https\\nhost={host}\\n\\n\" | git credential reject' and re-authenticate."
                    )
                    if evict_sug not in suggestions:
                        suggestions.append(evict_sug)
            else:
                err_label = r_data.get("error") or "Failed"
                err_msg = f"{host or r_url}: {err_label}"
                if err_msg not in errors:
                    errors.append(err_msg)
        else:
            if grade in ("C", "F"):
                lat_str = f"{latency:.3f}s" if latency is not None else "unknown"
                if protocol == "ssh":
                    if not ssh_mux_info.get("is_enabled"):
                        warn_msg = (
                            f"Remote '{host or r_url}' has degraded handshake latency ({lat_str}, Grade {grade}). "
                            "SSH multiplexing is disabled."
                        )
                        sug_msg = (
                            "Enable SSH multiplexing in ~/.ssh/config to speed up multi-repository fetch operations "
                            "(audit with 'git-sync doctor --ssh')."
                        )
                    else:
                        warn_msg = (
                            f"Remote '{host or r_url}' has degraded handshake latency ({lat_str}, Grade {grade}) "
                            "despite SSH multiplexing."
                        )
                        sug_msg = (
                            f"Verify DNS resolution speed for '{host}' or recreate SSH control sockets in "
                            f"'{ssh_mux_info.get('socket_dir', '~/.ssh')}'."
                        )
                else:
                    warn_msg = (
                        f"Remote '{host or r_url}' has degraded handshake latency ({lat_str}, Grade {grade}). "
                        "Slow DNS resolution or network route detected."
                    )
                    sug_msg = f"Check DNS lookup latency or inspect network routing to '{host}'."

                if warn_msg not in warnings:
                    warnings.append(warn_msg)
                if sug_msg not in suggestions:
                    suggestions.append(sug_msg)

    # 6. Worktree alerts
    for alert in worktree_health.get("alerts", []):
        if alert not in warnings:
            warnings.append(alert)
    if worktree_health.get("total_stale_worktrees", 0) > 0:
        sug = "Prune stale worktree metadata across repositories: 'git-sync worktree prune --all'."
        if sug not in suggestions:
            suggestions.append(sug)

    # 7. Lock file inspection
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

    # 8. Git LFS audit
    git_lfs = _dispatch_fn("inspect_git_lfs", inspect_git_lfs)(timeout=timeout)
    lfs_repos: List[str] = []
    for r_path in target_repos:
        l_info = _dispatch_fn("get_lfs_info", get_lfs_info)(r_path, timeout=timeout)
        if l_info.get("has_lfs"):
            lfs_repos.append(os.path.relpath(r_path, root_dir))

    if lfs_repos and not git_lfs.get("installed"):
        warnings.append(
            f"{len(lfs_repos)} repository(ies) use Git LFS, but the 'git-lfs' extension is not installed."
        )
        suggestions.append(
            "Install Git LFS to manage large tracked objects: 'sudo apt install git-lfs && git lfs install'"
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
        "git_lfs": git_lfs,
        "lfs_repos": lfs_repos,
        "credential_helpers": credential_helpers,
        "credential_helper_diagnostics": cred_diag,
        "has_token_env": has_token_env,
        "remotes": remotes_tested,
        "multi_remote_matrix": multi_remote_matrix,
        "worktree_health": worktree_health,
        "locked_repos": locked_repos,
        "warnings": warnings,
        "errors": errors,
        "suggestions": suggestions,
    }


__all__ = [
    "GRADE_A_THRESHOLD_SECONDS",
    "GRADE_B_THRESHOLD_SECONDS",
    "GRADE_C_THRESHOLD_SECONDS",
    "check_credentials_sanity",
    "compute_latency_grade",
    "inspect_credential_helpers",
    "inspect_github_cli",
    "inspect_token_env",
    "probe_remote_reachability",
]
