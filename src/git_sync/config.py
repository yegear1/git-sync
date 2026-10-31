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


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively deep-merge two configuration dictionaries.
    # INVARIANT: Child dictionaries are merged recursively; scalar values and lists at higher precedence override base.
    """
    merged: Dict[str, Any] = dict(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge(merged[k], v)
        elif isinstance(v, dict):
            merged[k] = deep_merge({}, v)
        elif isinstance(v, list):
            merged[k] = list(v)
        else:
            merged[k] = v
    return merged


DEFAULT_CONFIG_TEMPLATE: Dict[str, Any] = {
    "workers": "auto",
    "auto_tune": True,
    "timeout": DEFAULT_TIMEOUT,
    "exclude": [],
    "groups": [],
    "pull": {
        "ff_only": True,
        "sync_branches": True,
        "autostash": True,
        "strategy": "abort",
    },
    "push": {
        "dry_run": False,
        "protected_branches": ["main", "master", "release/*", "production"],
        "block_protected": False,
    },
    "status": {
        "compact": False,
        "no_fetch": False,
        "changelog": False,
    },
    "profiles": {
        "work": {
            "groups": ["work"],
        },
        "oss": {
            "groups": ["oss"],
        },
    },
}


def get_default_config_path(global_config: bool = False, custom_path: Optional[str] = None) -> str:
    """Determine target configuration file path for init or edit operations."""
    if custom_path:
        return str(Path(custom_path).expanduser().resolve())
    if global_config:
        xdg_env = os.environ.get("XDG_CONFIG_HOME")
        xdg_base = Path(xdg_env).expanduser().resolve() if (xdg_env and xdg_env.strip()) else (Path.home() / ".config")
        return str((xdg_base / "git-sync" / "config.json").resolve())
    return str((Path.cwd() / CONFIG_FILENAME).resolve())


def init_config_file(
    target_path: Optional[str] = None,
    global_config: bool = False,
    root: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Initialize a configuration template file if missing without clobbering existing configuration.
    # INVARIANT: Preserves existing files idempotently and generates valid zero-dependency JSON templates.
    """
    path_str = get_default_config_path(global_config=global_config, custom_path=target_path)
    p = Path(path_str)

    if p.is_file():
        return {
            "ok": True,
            "action": "already_exists",
            "path": str(p),
            "message": f"Configuration file already exists: {p}",
        }

    p.parent.mkdir(parents=True, exist_ok=True)
    template = {
        "root": root or resolve_default_root(),
        **DEFAULT_CONFIG_TEMPLATE,
    }

    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2)
            f.write("\n")
    except OSError as exc:
        return {
            "ok": False,
            "action": "error",
            "path": str(p),
            "error": str(exc),
            "message": f"Failed to create configuration file '{p}': {exc}",
        }

    return {
        "ok": True,
        "action": "created",
        "path": str(p),
        "message": f"Created configuration template at {p}",
    }


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
    sources: List[str] = field(default_factory=lambda: ["defaults"])
    pull_ff_only: bool = True
    pull_sync_branches: bool = True
    pull_all: bool = False
    pull_strategy: Optional[str] = None
    pull_autostash: bool = True
    pull_dry_run: bool = False
    pull_prune: bool = False
    pull_submodules: bool = False
    pull_unshallow: bool = False
    pull_reapply_sparse: bool = False
    push_dry_run: bool = False
    push_all: bool = False
    push_branch: Optional[str] = None
    push_protected_branches: List[str] = field(default_factory=lambda: ["main", "master", "release/*", "production", "prod"])
    push_block_protected: bool = False
    push_allow_protected: bool = False
    push_force: bool = False
    status_no_fetch: bool = False
    status_pending: bool = False
    status_compact: bool = False
    status_prune: bool = False
    status_changelog: bool = False
    status_submodules: bool = False
    hygiene: bool = False
    prune: bool = False
    submodules: bool = False
    unshallow: bool = False
    benchmark: bool = False
    use_cache: bool = True
    retries: int = 2
    preflight: bool = False
    remote: Optional[str] = None
    pull_remote: Optional[str] = None
    push_remote: Optional[str] = None
    max_tokens: Optional[int] = None
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
            "sources": list(self.sources),
            "pull": {
                "ff_only": self.pull_ff_only,
                "sync_branches": self.pull_sync_branches,
                "all": self.pull_all,
                "strategy": self.pull_strategy,
                "autostash": self.pull_autostash,
                "dry_run": self.pull_dry_run,
                "prune": self.pull_prune,
                "submodules": self.pull_submodules,
                "unshallow": self.pull_unshallow,
                "reapply_sparse": self.pull_reapply_sparse,
                "remote": self.pull_remote,
            },
            "push": {
                "dry_run": self.push_dry_run,
                "all": self.push_all,
                "branch": self.push_branch,
                "remote": self.push_remote,
                "protected_branches": list(self.push_protected_branches),
                "block_protected": self.push_block_protected,
                "allow_protected": self.push_allow_protected,
                "force": self.push_force,
            },
            "status": {
                "no_fetch": self.status_no_fetch,
                "pending": self.status_pending,
                "compact": self.status_compact,
                "prune": self.status_prune,
                "submodules": self.status_submodules,
                "changelog": self.status_changelog,
                "hygiene": self.hygiene,
            },
            "prune": self.prune,
            "submodules": self.submodules,
            "unshallow": self.unshallow,
            "hygiene": self.hygiene,
            "benchmark": self.benchmark,
            "use_cache": self.use_cache,
            "remote": self.remote,
            "max_tokens": self.max_tokens,
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
    sources: Optional[List[str]] = None,
) -> GitSyncConfig:
    """
    Merge raw configuration dictionary, apply workspace profile inheritance, and validate types.
    # INVARIANT: Top-level settings act as the base fallback; profile-specific settings override them.
    """
    cfg_data = data or {}
    if not isinstance(cfg_data, dict):
        raise ValueError(f"Configuration data must be a dictionary, got {type(cfg_data).__name__}")

    effective_sources: List[str] = list(sources) if sources is not None else ["defaults"]
    if config_path and config_path not in effective_sources:
        effective_sources.append(config_path)

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

    # Merge base configuration with profile overrides using deep_merge
    combined: Dict[str, Any] = {}
    for k, v in cfg_data.items():
        if k not in ("profiles", "default_profile"):
            combined[k] = v.copy() if isinstance(v, dict) else v

    if target_profile and profiles_raw:
        prof_overrides = profiles_raw[target_profile]
        if not isinstance(prof_overrides, dict):
            raise ValueError(f"Profile '{target_profile}' definition must be a JSON object")
        combined = deep_merge(combined, prof_overrides)
        profile_source_tag = f"profile:{target_profile}"
        if profile_source_tag not in effective_sources:
            effective_sources.append(profile_source_tag)

    # Parse and validate root directory
    root_raw = combined.get("root")
    if root_raw:
        root_str = str(root_raw).strip()
        if root_str.startswith("~"):
            root = str(Path(root_str).expanduser().resolve())
        else:
            root = root_str
    else:
        root = resolve_default_root()

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
    global_submodules = bool(combined.get("submodules", False))
    pull_submodules = bool(pull_cfg.get("submodules", combined.get("pull_submodules", global_submodules)))
    global_unshallow = bool(combined.get("unshallow", False))
    pull_unshallow = bool(pull_cfg.get("unshallow", combined.get("pull_unshallow", global_unshallow)))
    global_reapply_sparse = bool(combined.get("reapply_sparse", False))
    pull_reapply_sparse = bool(pull_cfg.get("reapply_sparse", combined.get("pull_reapply_sparse", global_reapply_sparse)))
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
    push_block_protected = bool(push_cfg.get("block_protected", combined.get("push_block_protected", False)))
    push_allow_protected = bool(push_cfg.get("allow_protected", combined.get("push_allow_protected", False)))
    push_force = bool(push_cfg.get("force", combined.get("push_force", False)))

    prot_branches_val = push_cfg.get("protected_branches", combined.get("protected_branches", None))
    if prot_branches_val is None:
        push_protected_branches = ["main", "master", "release/*", "production", "prod"]
    elif isinstance(prot_branches_val, list):
        push_protected_branches = [str(x) for x in prot_branches_val]
    elif isinstance(prot_branches_val, str):
        push_protected_branches = [p.strip() for p in prot_branches_val.split(",") if p.strip()]
    else:
        push_protected_branches = ["main", "master", "release/*", "production", "prod"]

    # Parse status section & flags
    status_cfg = combined.get("status") if isinstance(combined.get("status"), dict) else {}
    status_no_fetch = bool(status_cfg.get("no_fetch", combined.get("no_fetch", False)))
    status_pending = bool(status_cfg.get("pending", combined.get("pending", False)))
    status_compact = bool(status_cfg.get("compact", combined.get("compact", False)))
    status_prune = bool(status_cfg.get("prune", combined.get("status_prune", global_prune)))
    status_submodules = bool(status_cfg.get("submodules", combined.get("status_submodules", global_submodules)))
    status_changelog = bool(status_cfg.get("changelog", combined.get("changelog", False)))
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

    max_tokens_raw = combined.get("max_tokens")
    if max_tokens_raw is None and isinstance(combined.get("resolve"), dict):
        max_tokens_raw = combined.get("resolve", {}).get("max_tokens")
    try:
        max_tokens = int(max_tokens_raw) if max_tokens_raw is not None else None
    except (TypeError, ValueError):
        max_tokens = None

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
        pull_submodules=pull_submodules,
        pull_unshallow=pull_unshallow,
        pull_reapply_sparse=pull_reapply_sparse,
        push_dry_run=push_dry_run,
        push_all=push_all,
        push_branch=push_branch,
        push_protected_branches=push_protected_branches,
        push_block_protected=push_block_protected,
        push_allow_protected=push_allow_protected,
        push_force=push_force,
        status_no_fetch=status_no_fetch,
        status_pending=status_pending,
        status_compact=status_compact,
        status_prune=status_prune,
        status_submodules=status_submodules,
        status_changelog=status_changelog,
        hygiene=hygiene,
        prune=global_prune,
        submodules=global_submodules,
        unshallow=global_unshallow,
        benchmark=benchmark,
        use_cache=use_cache,
        retries=retries,
        preflight=preflight,
        remote=remote,
        pull_remote=pull_remote,
        push_remote=push_remote,
        max_tokens=max_tokens,
        active_profile=target_profile,
        config_path=config_path,
        available_profiles=available_profiles,
        sources=effective_sources,
    )


def load_config(
    config_path: Optional[str] = None,
    profile_name: Optional[str] = None,
) -> GitSyncConfig:
    """
    Discover, load, and hierarchically deep-merge configuration across precedence layers:
    defaults < XDG config < ~/.git-sync.json < ./.git-sync.json < env < profile < CLI
    # INVARIANT: Tracks override provenance in GitSyncConfig.sources in ascending order of precedence.
    """
    if profile_name is None:
        env_prof = os.environ.get("GIT_SYNC_PROFILE")
        if env_prof and env_prof.strip():
            profile_name = env_prof.strip()

    sources: List[str] = ["defaults"]
    accumulated: Dict[str, Any] = {}
    last_file_path: Optional[str] = None
    files_loaded = 0

    # Layer 1: XDG Base Directory specification ($XDG_CONFIG_HOME/git-sync/config.json or ~/.config/git-sync/config.json)
    xdg_env = os.environ.get("XDG_CONFIG_HOME")
    xdg_base = Path(xdg_env).expanduser().resolve() if (xdg_env and xdg_env.strip()) else (Path.home() / ".config")
    xdg_cfg = xdg_base / "git-sync" / "config.json"
    if xdg_cfg.is_file():
        p_str = str(xdg_cfg.resolve())
        xdg_data = load_config_file(p_str)
        accumulated = deep_merge(accumulated, xdg_data)
        if p_str not in sources:
            sources.append(p_str)
        last_file_path = p_str
        files_loaded += 1

    # Layer 2: User Home Directory (~/.git-sync.json)
    home_cfg = Path.home() / CONFIG_FILENAME
    if home_cfg.is_file():
        p_str = str(home_cfg.resolve())
        if p_str not in sources:
            home_data = load_config_file(p_str)
            accumulated = deep_merge(accumulated, home_data)
            sources.append(p_str)
            last_file_path = p_str
            files_loaded += 1

    # Layer 3: Workspace Directory (.git-sync.json), GIT_SYNC_CONFIG, or explicit custom --config
    if config_path:
        custom_p = Path(config_path).expanduser().resolve()
        if not custom_p.is_file():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        p_str = str(custom_p)
        custom_data = load_config_file(p_str)
        accumulated = deep_merge(accumulated, custom_data)
        if p_str not in sources:
            sources.append(p_str)
        last_file_path = p_str
        files_loaded += 1
    else:
        env_cfg = os.environ.get("GIT_SYNC_CONFIG")
        if env_cfg and env_cfg.strip():
            env_p = Path(env_cfg).expanduser().resolve()
            if env_p.is_file():
                p_str = str(env_p)
                if p_str not in sources:
                    env_data = load_config_file(p_str)
                    accumulated = deep_merge(accumulated, env_data)
                    sources.append(p_str)
                    last_file_path = p_str
                    files_loaded += 1
        else:
            cwd_cfg = Path.cwd() / CONFIG_FILENAME
            if cwd_cfg.is_file():
                p_str = str(cwd_cfg.resolve())
                if p_str not in sources:
                    cwd_data = load_config_file(p_str)
                    accumulated = deep_merge(accumulated, cwd_data)
                    sources.append(p_str)
                    last_file_path = p_str
                    files_loaded += 1

    # Layer 4: Environment Variables (GIT_SYNC_*)
    env_overrides: Dict[str, Any] = {}
    if "GIT_SYNC_ROOT" in os.environ and os.environ["GIT_SYNC_ROOT"].strip():
        env_overrides["root"] = os.environ["GIT_SYNC_ROOT"].strip()
    if "GIT_SYNC_WORKERS" in os.environ and os.environ["GIT_SYNC_WORKERS"].strip():
        w_val = os.environ["GIT_SYNC_WORKERS"].strip()
        if w_val.lower() == "auto":
            env_overrides["workers"] = "auto"
            env_overrides["auto_tune"] = True
        else:
            try:
                env_overrides["workers"] = int(w_val)
            except ValueError:
                pass
    if "GIT_SYNC_TIMEOUT" in os.environ and os.environ["GIT_SYNC_TIMEOUT"].strip():
        try:
            env_overrides["timeout"] = int(os.environ["GIT_SYNC_TIMEOUT"].strip())
        except ValueError:
            pass
    if "GIT_SYNC_AUTO_TUNE" in os.environ:
        env_overrides["auto_tune"] = os.environ["GIT_SYNC_AUTO_TUNE"].strip().lower() in ("1", "true", "yes")
    if "GIT_SYNC_REMOTE" in os.environ and os.environ["GIT_SYNC_REMOTE"].strip():
        env_overrides["remote"] = os.environ["GIT_SYNC_REMOTE"].strip()
    if "GIT_SYNC_RETRIES" in os.environ and os.environ["GIT_SYNC_RETRIES"].strip():
        try:
            env_overrides["retries"] = int(os.environ["GIT_SYNC_RETRIES"].strip())
        except ValueError:
            pass
    if "GIT_SYNC_PREFLIGHT" in os.environ:
        env_overrides["preflight"] = os.environ["GIT_SYNC_PREFLIGHT"].strip().lower() in ("1", "true", "yes")
    if "GIT_SYNC_BENCHMARK" in os.environ:
        env_overrides["benchmark"] = os.environ["GIT_SYNC_BENCHMARK"].strip().lower() in ("1", "true", "yes")
    if "GIT_SYNC_PUSH_BLOCK_PROTECTED" in os.environ:
        env_overrides["push_block_protected"] = os.environ["GIT_SYNC_PUSH_BLOCK_PROTECTED"].strip().lower() in ("1", "true", "yes")
    if "GIT_SYNC_PUSH_PROTECTED_BRANCHES" in os.environ:
        env_overrides["protected_branches"] = [
            b.strip() for b in os.environ["GIT_SYNC_PUSH_PROTECTED_BRANCHES"].split(",") if b.strip()
        ]

    if env_overrides:
        accumulated = deep_merge(accumulated, env_overrides)
        sources.append("env")

    # If no configuration files were located and profile was explicitly requested
    if files_loaded == 0 and not accumulated.get("profiles"):
        if profile_name:
            raise ValueError(f"Profile '{profile_name}' not found: no configuration file located")

    return resolve_config(
        data=accumulated if (files_loaded > 0 or env_overrides) else None,
        profile_name=profile_name,
        config_path=last_file_path,
        sources=sources,
    )
