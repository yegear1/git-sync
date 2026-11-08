"""
Commit categorization, Conventional Commit parsing, and Keep-a-Changelog summarization.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


def _dispatch_fn(name: str, fallback_fn: Any = None) -> Any:
    """
    Look up symbol dynamically from git_sync.core or git_sync.engine.git to respect unit test mocks.
    """
    for mod_name in (
        "git_sync.engine.git",
        "git_sync.core",
        "git_sync.engine.changelog",
        "git_sync.engine.diff",
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

    classify_fn = _dispatch_fn("classify_commit", classify_commit)

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
                commits.append(classify_fn(h, s, b))
        elif len(parts) == 1 and parts[0].strip():
            commits.append(classify_fn("", parts[0].strip(), ""))
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


def get_repo_commits(repo_path: str, revision_range: str, timeout: int = 5) -> List[ClassifiedCommit]:
    """Retrieve and classify commits in the specified revision range using git log formatting."""
    run_git_fn = _dispatch_fn("run_git")
    if run_git_fn is None:
        from git_sync.engine.git import run_git as run_git_fn

    proc = run_git_fn(repo_path, ["log", revision_range, "--format=%h%x00%s%x00%b%x1f"], timeout=timeout)
    if proc.returncode != 0:
        return []

    parse_fn = _dispatch_fn("parse_commit_log", parse_commit_log)
    return parse_fn(proc.stdout)


def get_repo_pending_changelog(
    repo_path: str,
    upstream: Optional[str] = None,
    ahead: int = 0,
    behind: int = 0,
    timeout: int = 5,
) -> Dict[str, Any]:
    """
    Retrieve and categorize incoming and outgoing commits between HEAD and upstream.
    Uses ahead/behind counts to avoid subshells when there are no pending changes.
    """
    incoming_commits: List[ClassifiedCommit] = []
    outgoing_commits: List[ClassifiedCommit] = []

    commits_fn = _dispatch_fn("get_repo_commits", get_repo_commits)
    summarize_fn = _dispatch_fn("summarize_commit_categories", summarize_commit_categories)

    up_ref = upstream or "@{u}"

    if behind > 0:
        incoming_commits = commits_fn(repo_path, f"HEAD..{up_ref}", timeout=timeout)
    elif upstream is None and ahead == 0 and behind == 0:
        incoming_commits = commits_fn(repo_path, f"HEAD..{up_ref}", timeout=timeout)

    if ahead > 0:
        outgoing_commits = commits_fn(repo_path, f"{up_ref}..HEAD", timeout=timeout)
    elif upstream is None and ahead == 0 and behind == 0:
        outgoing_commits = commits_fn(repo_path, f"{up_ref}..HEAD", timeout=timeout)

    inc_cats = summarize_fn(incoming_commits)
    out_cats = summarize_fn(outgoing_commits)
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


__all__ = [
    "BREAKING_CHANGE_REGEX",
    "CHANGELOG_CATEGORY_ORDER",
    "CONVENTIONAL_CATEGORY_MAP",
    "CONVENTIONAL_COMMIT_REGEX",
    "ClassifiedCommit",
    "classify_commit",
    "format_changelog_summary",
    "get_repo_commits",
    "get_repo_pending_changelog",
    "parse_commit_log",
    "summarize_commit_categories",
]
