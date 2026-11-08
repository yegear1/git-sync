"""
Token-budget aware diff chunking, truncation, and divergence diff inspection.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional


def _dispatch_fn(name: str, fallback_fn: Any = None) -> Any:
    """
    Look up symbol dynamically from git_sync.core or git_sync.engine.git to respect unit test mocks.
    """
    for mod_name in (
        "git_sync.engine.git",
        "git_sync.core",
        "git_sync.engine.diff",
        "git_sync.engine.changelog",
        "git_sync.engine.ssh",
        "git_sync.engine.credentials",
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
    return fallback_fn


DEFAULT_COMPACT_DIFF_TOKENS = 1000
DEFAULT_CHARS_PER_TOKEN = 4


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

    split_fn = _dispatch_fn("split_diff_files", split_diff_files)
    chunks = split_fn(diff)
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
    timeout: int = 5,
    max_tokens: Optional[int] = None,
) -> str:
    """
    Generate diff text between local branch and remote upstream tracking branch.
    # INVARIANT: Uses symmetric three-dot diff HEAD...upstream to inspect changes introduced on remote since divergence.
    # INVARIANT: When max_tokens is provided and stat_only is False, applies deterministic
    # file-boundary aware diff truncation to protect LLM context windows and CLI outputs.
    """
    run_git_fn = _dispatch_fn("run_git")
    if run_git_fn is None:
        from git_sync.engine.git import run_git as run_git_fn

    curr_branch = branch or run_git_fn(repo_path, ["branch", "--show-current"], timeout=5).stdout.strip()
    if not curr_branch:
        curr_branch = "HEAD"

    p_up = run_git_fn(repo_path, ["rev-parse", "--abbrev-ref", f"{curr_branch}@{{u}}"], timeout=5)
    upstream = p_up.stdout.strip()
    if p_up.returncode != 0 or not upstream:
        return ""

    cmd = ["diff"]
    if stat_only:
        cmd.append("--stat")
    cmd.append(f"{curr_branch}...{upstream}")

    p_diff = run_git_fn(repo_path, cmd, timeout=timeout)
    diff_text = p_diff.stdout.strip()
    if max_tokens is not None and not stat_only and diff_text:
        truncate_fn = _dispatch_fn("truncate_diff", truncate_diff)
        diff_text = truncate_fn(diff_text, max_tokens=max_tokens)
    return diff_text


def get_divergence_details(
    repo_path: str,
    branch: Optional[str] = None,
    timeout: int = 5,
) -> Dict[str, Any]:
    """
    Inspect divergence details between local branch and remote tracking upstream.
    Returns ahead/behind counts, commit logs (local and remote), merge-base, diffstat, and dirty status.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 prevents interactive prompts when querying git refs.
    """
    run_git_fn = _dispatch_fn("run_git")
    if run_git_fn is None:
        from git_sync.engine.git import run_git as run_git_fn

    curr_branch = branch or run_git_fn(repo_path, ["branch", "--show-current"], timeout=5).stdout.strip()
    if not curr_branch:
        curr_branch = "HEAD (detached)"

    # Dirty status check
    p_status = run_git_fn(repo_path, ["status", "--porcelain"], timeout=5)
    dirty_lines = [line.strip() for line in p_status.stdout.splitlines() if line.strip()]

    # Upstream check
    p_up = run_git_fn(repo_path, ["rev-parse", "--abbrev-ref", f"{curr_branch}@{{u}}"], timeout=5)
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
    p_ahead = run_git_fn(repo_path, ["rev-list", "--count", f"{upstream}..{curr_branch}"], timeout=timeout)
    p_behind = run_git_fn(repo_path, ["rev-list", "--count", f"{curr_branch}..{upstream}"], timeout=timeout)
    try:
        ahead = int(p_ahead.stdout.strip() or 0)
        behind = int(p_behind.stdout.strip() or 0)
    except ValueError:
        ahead = 0
        behind = 0

    is_diverged = (ahead > 0 and behind > 0)

    # Common merge base
    p_mb = run_git_fn(repo_path, ["merge-base", curr_branch, upstream], timeout=timeout)
    merge_base = p_mb.stdout.strip() if p_mb.returncode == 0 else None

    # Local commits (ahead of upstream)
    local_commits: List[Dict[str, str]] = []
    if ahead > 0:
        p_log_local = run_git_fn(
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
        p_log_remote = run_git_fn(
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
    p_stat = run_git_fn(repo_path, ["diff", "--stat", f"{curr_branch}...{upstream}"], timeout=timeout)
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


__all__ = [
    "DEFAULT_CHARS_PER_TOKEN",
    "DEFAULT_COMPACT_DIFF_TOKENS",
    "get_divergence_details",
    "get_divergence_diff",
    "split_diff_files",
    "truncate_diff",
]
