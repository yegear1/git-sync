"""
User interface and terminal rendering components for git-sync.
"""

from __future__ import annotations

from git_sync.ui.dashboard import (
    EXIT_CODE_SIGINT,
    InteractiveDashboard,
    get_repo_diff_stat,
    read_single_key,
)
from git_sync.ui.formatters import (
    build_status_summary,
    print_benchmark_report,
    print_dirty_report,
    print_doctor_report,
    print_ssh_audit_report,
    print_status_compact,
    print_status_table,
    print_unlock_report,
    print_worktree_list_report,
    print_worktree_prune_report,
    prompt_resolution_choice,
    render_pull_item,
    render_push_item,
)
from git_sync.ui.progress import (
    BulkActionItem,
    ProgressIndicator,
    format_bulk_action_preview,
    prompt_bulk_confirmation,
)
from git_sync.ui.style import (
    ANSI_BLUE,
    ANSI_BOLD,
    ANSI_CYAN,
    ANSI_DIM,
    ANSI_GREEN,
    ANSI_MAGENTA,
    ANSI_RED,
    ANSI_RESET,
    ANSI_YELLOW,
    AnsiStyler,
    format_diff_stat,
    should_use_color,
    style_text,
)

__all__ = [
    "ANSI_BLUE",
    "ANSI_BOLD",
    "ANSI_CYAN",
    "ANSI_DIM",
    "ANSI_GREEN",
    "ANSI_MAGENTA",
    "ANSI_RED",
    "ANSI_RESET",
    "ANSI_YELLOW",
    "EXIT_CODE_SIGINT",
    "AnsiStyler",
    "BulkActionItem",
    "InteractiveDashboard",
    "ProgressIndicator",
    "build_status_summary",
    "format_bulk_action_preview",
    "format_diff_stat",
    "get_repo_diff_stat",
    "print_benchmark_report",
    "print_dirty_report",
    "print_doctor_report",
    "print_ssh_audit_report",
    "print_status_compact",
    "print_status_table",
    "print_unlock_report",
    "print_worktree_list_report",
    "print_worktree_prune_report",
    "prompt_bulk_confirmation",
    "prompt_resolution_choice",
    "read_single_key",
    "render_pull_item",
    "render_push_item",
    "should_use_color",
    "style_text",
]
