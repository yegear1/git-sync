"""
Configuration loader and profile resolver for git-sync.
Handles persistent workspace configurations via .git-sync.json.
"""

from dataclasses import dataclass, field
import fnmatch
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from git_sync.core import (
    DEFAULT_DISK_LATENCY_THRESHOLD,
    DEFAULT_NETWORK_LATENCY_THRESHOLD,
    DEFAULT_ROOT,
    DEFAULT_TIMEOUT,
    DEFAULT_WORKERS,
    resolve_default_root,
)

CONFIG_FILENAME = ".git-sync.json"


@dataclass
class GitSyncConfig:
    """Strongly-typed workspace configuration container."""
    root: str = field(default_factory=resolve_default_root)
    workers: int | str = DEFAULT_WORKERS
    auto_tune: bool = False
    timeout: int = DEFAULT_TIMEOUT
    disk_latency_threshold: float = DEFAULT_DISK_LATENCY_THRESHOLD
    network_latency_threshold: float = DEFAULT_NETWORK_LATENCY_THRESHOLD
    exclude: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    pull_ff_only: bool = True
    pull_sync_branches: bool = True
    pull_all: bool = False
    pull_strategy: Optional[str] = None
    pull_autostash: bool = True
    pull_dry_run: bool = False
    pull_prune: bool = False
    push_dry_run: bool = False
    push_all: bool = False
    push_branch: Optional[str] = None
    status_no_fetch: bool = False
    status_pending: bool = False
    status_compact: bool = False
    status_prune: bool = False
    hygiene: bool = False
    prune: bool = False
    benchmark: bool = False
    use_cache: bool = True
    retries: int = 2
    preflight: bool = False
    remote: Optional[str] = None
    pull_remote: Optional[str] = None
    push_remote: Optional[str] = None
    active_profile: Optional[str] = None
    config_path: Optional[str] = None
    available_profiles: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to structured JSON-serializable dictionary."""
        return {
            "root": self.root,
            "workers": self.workers,
            "auto_tune": self.auto_tune,
            "timeout": self.timeout,
            "disk_latency_threshold": self.disk_latency_threshold,
            "network_latency_threshold": self.network_latency_threshold,
            "retries": self.retries,
            "preflight": self.preflight,
            "exclude": list(self.exclude),
            "groups": list(self.groups),
            "pull": {
                "ff_only": self.pull_ff_only,
                "sync_branches": self.pull_sync_branches,
                "all": self.pull_all,
                "strategy": self.pull_strategy,
                "autostash": self.pull_autostash,
                "dry_run": self.pull_dry_run,
                "prune": self.pull_prune,
                "remote": self.pull_remote,
            },
            "push": {
                "dry_run": self.push_dry_run,
                "all": self.push_all,
                "branch": self.push_branch,
                "remote": self.push_remote,
            },
            "status": {
                "no_fetch": self.status_no_fetch,
                "pending": self.status_pending,
                "compact": self.status_compact,
                "prune": self.status_prune,
                "hygiene": self.hygiene,
            },
            "prune": self.prune,
            "hygiene": self.hygiene,
            "benchmark": self.benchmark,
            "use_cache": self.use_cache,
            "remote": self.remote,
            "active_profile": self.active_profile,
            "config_path": self.config_path,
            "available_profiles": list(self.available_profiles),
        }


def find_config_file(custom_path: Optional[str] = None) -> Optional[str]:
    """
    Locate configuration file following standard precedence:
    1. Explicit custom path (must exist or raises FileNotFoundError)
    2. GIT_SYNC_CONFIG environment variable if set
    3. Current working directory (.git-sync.json)
    4. XDG Base Directory specification ($XDG_CONFIG_HOME/git-sync/config.json or ~/.config/git-sync/config.json)
    5. User home directory (~/.git-sync.json)
    Returns absolute path or None.
    """
    if custom_path:
        p = Path(custom_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Configuration file not found: {custom_path}")
        return str(p)

    env_cfg = os.environ.get("GIT_SYNC_CONFIG")
    if env_cfg:
        env_p = Path(env_cfg).expanduser().resolve()
        if env_p.is_file():
            return str(env_p)

    cwd_cfg = Path.cwd() / CONFIG_FILENAME
    if cwd_cfg.is_file():
        return str(cwd_cfg.resolve())

    # XDG Base Directory support
    xdg_env = os.environ.get("XDG_CONFIG_HOME")
    xdg_base = Path(xdg_env).expanduser().resolve() if (xdg_env and xdg_env.strip()) else (Path.home() / ".config")
    xdg_cfg = xdg_base / "git-sync" / "config.json"
    if xdg_cfg.is_file():
        return str(xdg_cfg.resolve())

    home_cfg = Path.home() / CONFIG_FILENAME
    if home_cfg.is_file():
        return str(home_cfg.resolve())

    return None


def load_config_file(path: str) -> Dict[str, Any]:
    """
    Load and parse a JSON configuration file.
    Raises ValueError on syntax or type invalidity, OSError on read failure.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in configuration file '{path}': {exc}") from exc
    except OSError as exc:
        raise OSError(f"Failed to read configuration file '{path}': {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Configuration root in '{path}' must be a JSON object, got {type(data).__name__}")

    return data


def resolve_config(
    data: Optional[Dict[str, Any]] = None,
    profile_name: Optional[str] = None,
    config_path: Optional[str] = None,
) -> GitSyncConfig:
    """
    Merge raw configuration dictionary, apply workspace profile inheritance, and validate types.
    # INVARIANT: Top-level settings act as the base fallback; profile-specific settings override them.
    """
    cfg_data = data or {}
    if not isinstance(cfg_data, dict):
        raise ValueError(f"Configuration data must be a dictionary, got {type(cfg_data).__name__}")

    # Extract available profiles
    profiles_raw = cfg_data.get("profiles")
    available_profiles: List[str] = []
    if isinstance(profiles_raw, dict):
        available_profiles = sorted(list(profiles_raw.keys()))
    elif profiles_raw is not None:
        raise ValueError(f"'profiles' must be a dictionary, got {type(profiles_raw).__name__}")

    # Determine targeted profile
    target_profile: Optional[str] = None
    if profile_name:
        if not profiles_raw or profile_name not in profiles_raw:
            raise ValueError(f"Profile '{profile_name}' not found in configuration")
        target_profile = profile_name
    elif cfg_data.get("default_profile"):
        default_prof = str(cfg_data["default_profile"])
        if not profiles_raw or default_prof not in profiles_raw:
            raise ValueError(f"Default profile '{default_prof}' not found in configuration")
        target_profile = default_prof
    elif profiles_raw and "default" in profiles_raw:
        target_profile = "default"

    # Merge base configuration with profile overrides
    combined: Dict[str, Any] = {}
    for k, v in cfg_data.items():
        if k not in ("profiles", "default_profile"):
            combined[k] = v.copy() if isinstance(v, dict) else v

    if target_profile and profiles_raw:
        prof_overrides = profiles_raw[target_profile]
        if not isinstance(prof_overrides, dict):
            raise ValueError(f"Profile '{target_profile}' definition must be a JSON object")
        for k, v in prof_overrides.items():
            if isinstance(v, dict) and isinstance(combined.get(k), dict):
                combined[k] = {**combined[k], **v}
            else:
                combined[k] = v

    # Parse and validate root directory
    root = str(combined.get("root") or resolve_default_root())

    # Parse and validate workers
    workers_raw = combined.get("workers", DEFAULT_WORKERS)
    if isinstance(workers_raw, str) and workers_raw.strip().lower() == "auto":
        workers: int | str = "auto"
        auto_tune_default = True
    else:
        try:
            workers = int(workers_raw)
            if workers <= 0:
                raise ValueError
            auto_tune_default = False
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid 'workers' setting: must be a positive integer or 'auto', got {workers_raw!r}") from exc

    auto_tune = bool(combined.get("auto_tune", auto_tune_default))
    try:
        disk_threshold = float(combined.get("disk_latency_threshold", DEFAULT_DISK_LATENCY_THRESHOLD))
    except (TypeError, ValueError):
        disk_threshold = DEFAULT_DISK_LATENCY_THRESHOLD
    try:
        network_threshold = float(combined.get("network_latency_threshold", DEFAULT_NETWORK_LATENCY_THRESHOLD))
    except (TypeError, ValueError):
        network_threshold = DEFAULT_NETWORK_LATENCY_THRESHOLD

    # Parse and validate timeout
    timeout_raw = combined.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout = int(timeout_raw)
        if timeout <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid 'timeout' setting: must be a positive integer, got {timeout_raw!r}") from exc

    # Parse exclusions (support 'exclude' or 'exclude_dirs')
    exclude_val = combined.get("exclude", combined.get("exclude_dirs", []))
    if isinstance(exclude_val, str):
        exclude = [exclude_val]
    elif isinstance(exclude_val, list):
        if not all(isinstance(x, str) for x in exclude_val):
            raise ValueError("All elements in 'exclude' must be strings")
        exclude = list(exclude_val)
    else:
        raise ValueError(f"Invalid 'exclude' setting: expected list of strings, got {type(exclude_val).__name__}")

    # Parse preferred groups (support 'groups' or single 'group')
    groups_val = combined.get("groups", combined.get("group", []))
    if isinstance(groups_val, str):
        groups = [groups_val]
    elif isinstance(groups_val, list):
        if not all(isinstance(x, str) for x in groups_val):
            raise ValueError("All elements in 'groups' must be strings")
        groups = list(groups_val)
    else:
        raise ValueError(f"Invalid 'groups' setting: expected list of strings, got {type(groups_val).__name__}")

    # Parse pull section & flags
    pull_cfg = combined.get("pull") if isinstance(combined.get("pull"), dict) else {}
    pull_ff_only = bool(pull_cfg.get("ff_only", combined.get("ff_only", True)))
    pull_sync_branches = bool(pull_cfg.get("sync_branches", combined.get("sync_branches", True)))
    pull_all = bool(pull_cfg.get("all", combined.get("pull_all", False)))
    pull_strat_raw = pull_cfg.get("strategy", combined.get("pull_strategy", None))
    pull_strategy = str(pull_strat_raw) if pull_strat_raw is not None else None
    pull_autostash = bool(pull_cfg.get("autostash", combined.get("autostash", True)))
    pull_dry_run = bool(pull_cfg.get("dry_run", combined.get("dry_run", False)))
    global_prune = bool(combined.get("prune", False))
    pull_prune = bool(pull_cfg.get("prune", combined.get("pull_prune", global_prune)))
    pull_remote_raw = pull_cfg.get("remote", combined.get("pull_remote", None))
    pull_remote = str(pull_remote_raw) if pull_remote_raw is not None else None

    # Parse push section & flags
    push_cfg = combined.get("push") if isinstance(combined.get("push"), dict) else {}
    push_dry_run = bool(push_cfg.get("dry_run", combined.get("dry_run", False)))
    push_all = bool(push_cfg.get("all", combined.get("push_all", False)))
    push_branch_raw = push_cfg.get("branch", combined.get("branch", None))
    push_branch = str(push_branch_raw) if push_branch_raw is not None else None
    push_remote_raw = push_cfg.get("remote", combined.get("push_remote", None))
    push_remote = str(push_remote_raw) if push_remote_raw is not None else None

    # Parse status section & flags
    status_cfg = combined.get("status") if isinstance(combined.get("status"), dict) else {}
    status_no_fetch = bool(status_cfg.get("no_fetch", combined.get("no_fetch", False)))
    status_pending = bool(status_cfg.get("pending", combined.get("pending", False)))
    status_compact = bool(status_cfg.get("compact", combined.get("compact", False)))
    status_prune = bool(status_cfg.get("prune", combined.get("status_prune", global_prune)))
    global_hygiene = bool(combined.get("hygiene", False))
    hygiene = bool(status_cfg.get("hygiene", combined.get("hygiene", global_hygiene)))
    if "group" in status_cfg and isinstance(status_cfg["group"], str) and not groups:
        groups = [status_cfg["group"]]

    # Parse global telemetry, benchmark, cache, retry, preflight, and remote settings
    benchmark = bool(combined.get("benchmark", False))
    use_cache = bool(combined.get("use_cache", True))
    retries_raw = combined.get("retries", 2)
    try:
        retries = int(retries_raw)
        if retries < 0:
            retries = 0
    except (TypeError, ValueError):
        retries = 2
    preflight = bool(combined.get("preflight", False))
    remote_raw = combined.get("remote")
    remote = str(remote_raw) if remote_raw is not None else None

    return GitSyncConfig(
        root=root,
        workers=workers,
        auto_tune=auto_tune,
        timeout=timeout,
        disk_latency_threshold=disk_threshold,
        network_latency_threshold=network_threshold,
        exclude=exclude,
        groups=groups,
        pull_ff_only=pull_ff_only,
        pull_sync_branches=pull_sync_branches,
        pull_all=pull_all,
        pull_strategy=pull_strategy,
        pull_autostash=pull_autostash,
        pull_dry_run=pull_dry_run,
        pull_prune=pull_prune,
        push_dry_run=push_dry_run,
        push_all=push_all,
        push_branch=push_branch,
        status_no_fetch=status_no_fetch,
        status_pending=status_pending,
        status_compact=status_compact,
        status_prune=status_prune,
        hygiene=hygiene,
        prune=global_prune,
        benchmark=benchmark,
        use_cache=use_cache,
        retries=retries,
        preflight=preflight,
        remote=remote,
        pull_remote=pull_remote,
        push_remote=push_remote,
        active_profile=target_profile,
        config_path=config_path,
        available_profiles=available_profiles,
    )


def load_config(
    config_path: Optional[str] = None,
    profile_name: Optional[str] = None,
) -> GitSyncConfig:
    """
    Discover, load, and resolve git-sync configuration from disk.
    If no config file exists, returns default GitSyncConfig.
    """
    resolved_path = find_config_file(config_path)
    if not resolved_path:
        if profile_name:
            raise ValueError(f"Profile '{profile_name}' not found: no configuration file located")
        return resolve_config(data=None, profile_name=None, config_path=None)

    data = load_config_file(resolved_path)
    return resolve_config(data=data, profile_name=profile_name, config_path=resolved_path)
