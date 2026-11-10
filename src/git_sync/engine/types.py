"""
Typed domain contracts and data payload definitions for git-sync engine operations.
Provides TypedDict and dataclass specifications replacing unchecked Dict[str, Any] signatures.

# INVARIANT: Zero external dependencies; standard library only.
"""

from __future__ import annotations

from typing import Any, TypedDict


class SshAgentStatusDict(TypedDict, total=False):
    status: str
    socket: str | None
    identities_count: int
    identities: list[str]


class SshMultiplexInfoDict(TypedDict, total=False):
    is_enabled: bool
    control_master: str | None
    control_path: str | None
    control_persist: str | None
    socket_dir: str | None
    socket_dir_exists: bool
    stale_sockets_pruned: list[str]
    active_sockets: list[str]


class GithubCliStatusDict(TypedDict, total=False):
    installed: bool
    logged_in: bool
    token_expired: bool
    active_account: str | None
    host: str | None
    protocol: str | None
    token_scopes: list[str]
    error: str | None
    suggestion: str | None


class GitLfsStatusDict(TypedDict, total=False):
    installed: bool
    version: str | None
    filter_configured: bool


class LfsInfoDict(TypedDict, total=False):
    has_lfs: bool
    objects_count: int
    total_size_bytes: int
    pointers_count: int
    unsmudged_count: int
    files: list[str]


class SparseCheckoutInfoDict(TypedDict, total=False):
    enabled: bool
    cone_mode: bool
    patterns: list[str]
    pattern_count: int
    sparse_index: bool
    out_of_cone_files: list[str]
    warning: str | None


class PreflightPushCheckResult(TypedDict, total=False):
    can_push: bool
    ahead: int
    behind: int
    diverged: bool
    branch: str
    upstream: str | None
    is_protected: bool
    blocked_by_protection: bool
    unreachable: bool
    error: str | None
    remediation: str | None


class RepoStatusRecord(TypedDict, total=False):
    name: str
    path: str
    rel_path: str
    group: str
    branch: str
    remote_url: str
    remotes: dict[str, str]
    target_remote: str | None
    multi_remote: bool
    is_dirty: bool
    dirty_files: list[str]
    fetch_ok: bool
    fetch_error: str | None
    diagnostic: dict[str, Any] | None
    network_state: str
    remediation: str | None
    upstream: str | None
    ahead: int
    behind: int
    aux_branches_behind: dict[str, int]
    aux_branches_ahead: dict[str, int]
    status: str
    action_required: str
    is_worktree: bool
    worktree_main_path: str | None
    worktrees: list[dict[str, Any]]
    stale_worktrees: list[dict[str, Any]]
    submodules: list[dict[str, Any]]
    is_shallow: bool
    has_lfs: bool
    lfs: dict[str, Any] | None
    sparse_checkout: dict[str, Any]
    stale_refs: list[str]
    pruned_refs: list[str]
    gone_branches: list[str]
    merged_branches: list[str]
    branch_hygiene: dict[str, Any] | None
    changelog: dict[str, Any] | None


class PullResult(TypedDict, total=False):
    repo: str
    repo_rel: str | None
    branch: str
    remote: str | None
    success: bool
    stdout: str
    stderr: str
    aux_branches: dict[str, Any]
    worktree_isolated: Any
    is_worktree: bool
    worktree_main_path: str | None
    autostash: dict[str, Any]
    dry_run: bool
    ahead: int
    behind: int
    diagnostic: dict[str, Any] | None
    is_shallow: bool
    unshallow: bool
    sparse_checkout: dict[str, Any]
    reapply_sparse: bool
    sparse_reapply: dict[str, Any] | None
    lfs: dict[str, Any]
    stale_refs: list[str]
    pruned_refs: list[str]
    would_prune_refs: list[str]
    submodules: dict[str, Any] | None
    resolved: ResolveResult | dict[str, Any] | None


class PushResult(TypedDict, total=False):
    repo: str
    repo_rel: str | None
    branch: str
    remote: str | None
    success: bool
    stdout: str
    stderr: str
    dry_run: bool
    ahead: int
    diagnostic: dict[str, Any] | None
    preflight: dict[str, Any]
    is_worktree: bool
    worktree_main_path: str | None
    aux_branches: dict[str, Any] | None
    aux_diagnostics: dict[str, Any] | None


class ResolveResult(TypedDict, total=False):
    repo: str
    branch: str
    strategy: str
    success: bool
    stdout: str
    stderr: str
    message: str
    dry_run: bool
    repo_rel: str | None
    details: dict[str, Any] | None


class DoctorSanityReport(TypedDict, total=False):
    ok: bool
    ssh_agent: dict[str, Any]
    ssh_keys: list[str]
    ssh_multiplexing: dict[str, Any]
    ssh_audit: dict[str, Any] | None
    github_cli: dict[str, Any]
    git_lfs: dict[str, Any]
    lfs_repos: list[str]
    credential_helpers: list[str]
    credential_helper_diagnostics: dict[str, Any]
    has_token_env: bool
    remotes: dict[str, Any]
    multi_remote_matrix: dict[str, Any]
    worktree_health: dict[str, Any]
    locked_repos: list[dict[str, Any]]
    warnings: list[str]
    errors: list[str]
    suggestions: list[str]


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
