"""
Typed domain contracts and data payload definitions for git-sync engine operations.
Provides TypedDict and dataclass specifications replacing unchecked Dict[str, Any] signatures.

# INVARIANT: Zero external dependencies; standard library only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict, Union


class SshAgentStatusDict(TypedDict, total=False):
    status: str
    socket: Optional[str]
    identities_count: int
    identities: List[str]


class SshMultiplexInfoDict(TypedDict, total=False):
    is_enabled: bool
    control_master: Optional[str]
    control_path: Optional[str]
    control_persist: Optional[str]
    socket_dir: Optional[str]
    socket_dir_exists: bool
    stale_sockets_pruned: List[str]
    active_sockets: List[str]


class GithubCliStatusDict(TypedDict, total=False):
    installed: bool
    logged_in: bool
    token_expired: bool
    active_account: Optional[str]
    host: Optional[str]
    protocol: Optional[str]
    token_scopes: List[str]
    error: Optional[str]
    suggestion: Optional[str]


class GitLfsStatusDict(TypedDict, total=False):
    installed: bool
    version: Optional[str]
    filter_configured: bool


class LfsInfoDict(TypedDict, total=False):
    has_lfs: bool
    objects_count: int
    total_size_bytes: int
    pointers_count: int
    unsmudged_count: int
    files: List[str]


class SparseCheckoutInfoDict(TypedDict, total=False):
    enabled: bool
    cone_mode: bool
    patterns: List[str]
    pattern_count: int
    sparse_index: bool
    out_of_cone_files: List[str]
    warning: Optional[str]


class PreflightPushCheckResult(TypedDict, total=False):
    can_push: bool
    ahead: int
    behind: int
    diverged: bool
    branch: str
    upstream: Optional[str]
    is_protected: bool
    blocked_by_protection: bool
    unreachable: bool
    error: Optional[str]
    remediation: Optional[str]


class RepoStatusRecord(TypedDict, total=False):
    name: str
    path: str
    rel_path: str
    group: str
    branch: str
    remote_url: str
    remotes: Dict[str, str]
    target_remote: Optional[str]
    multi_remote: bool
    is_dirty: bool
    dirty_files: List[str]
    fetch_ok: bool
    fetch_error: Optional[str]
    diagnostic: Optional[Dict[str, Any]]
    network_state: str
    remediation: Optional[str]
    upstream: Optional[str]
    ahead: int
    behind: int
    aux_branches_behind: Dict[str, int]
    aux_branches_ahead: Dict[str, int]
    status: str
    action_required: str
    is_worktree: bool
    worktree_main_path: Optional[str]
    worktrees: List[Dict[str, Any]]
    stale_worktrees: List[Dict[str, Any]]
    submodules: List[Dict[str, Any]]
    is_shallow: bool
    has_lfs: bool
    lfs: Optional[Dict[str, Any]]
    sparse_checkout: Dict[str, Any]
    stale_refs: List[str]
    pruned_refs: List[str]
    gone_branches: List[str]
    merged_branches: List[str]
    branch_hygiene: Optional[Dict[str, Any]]
    changelog: Optional[Dict[str, Any]]


class PullResult(TypedDict, total=False):
    repo: str
    repo_rel: Optional[str]
    branch: str
    remote: Optional[str]
    success: bool
    stdout: str
    stderr: str
    aux_branches: Dict[str, Any]
    worktree_isolated: Any
    is_worktree: bool
    worktree_main_path: Optional[str]
    autostash: Dict[str, Any]
    dry_run: bool
    ahead: int
    behind: int
    diagnostic: Optional[Dict[str, Any]]
    is_shallow: bool
    unshallow: bool
    sparse_checkout: Dict[str, Any]
    reapply_sparse: bool
    sparse_reapply: Optional[Dict[str, Any]]
    lfs: Dict[str, Any]
    stale_refs: List[str]
    pruned_refs: List[str]
    would_prune_refs: List[str]
    submodules: Optional[Dict[str, Any]]
    resolved: Optional[Union[ResolveResult, Dict[str, Any]]]


class PushResult(TypedDict, total=False):
    repo: str
    repo_rel: Optional[str]
    branch: str
    remote: Optional[str]
    success: bool
    stdout: str
    stderr: str
    dry_run: bool
    ahead: int
    diagnostic: Optional[Dict[str, Any]]
    preflight: Dict[str, Any]
    is_worktree: bool
    worktree_main_path: Optional[str]
    aux_branches: Optional[Dict[str, Any]]
    aux_diagnostics: Optional[Dict[str, Any]]


class ResolveResult(TypedDict, total=False):
    repo: str
    branch: str
    strategy: str
    success: bool
    stdout: str
    stderr: str
    message: str
    dry_run: bool
    repo_rel: Optional[str]
    details: Optional[Dict[str, Any]]


class DoctorSanityReport(TypedDict, total=False):
    ok: bool
    ssh_agent: Dict[str, Any]
    ssh_keys: List[str]
    ssh_multiplexing: Dict[str, Any]
    ssh_audit: Optional[Dict[str, Any]]
    github_cli: Dict[str, Any]
    git_lfs: Dict[str, Any]
    lfs_repos: List[str]
    credential_helpers: List[str]
    credential_helper_diagnostics: Dict[str, Any]
    has_token_env: bool
    remotes: Dict[str, Any]
    multi_remote_matrix: Dict[str, Any]
    worktree_health: Dict[str, Any]
    locked_repos: List[Dict[str, Any]]
    warnings: List[str]
    errors: List[str]
    suggestions: List[str]


__all__ = [
    "DoctorSanityReport",
    "GitLfsStatusDict",
    "GithubCliStatusDict",
    "LfsInfoDict",
    "PreflightPushCheckResult",
    "PullResult",
    "PushResult",
    "RepoStatusRecord",
    "ResolveResult",
    "SparseCheckoutInfoDict",
    "SshAgentStatusDict",
    "SshMultiplexInfoDict",
]
