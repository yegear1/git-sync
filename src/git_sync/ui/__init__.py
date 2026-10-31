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
from git_sync.ui.progress import ProgressIndicator
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
    "AnsiStyler",
    "EXIT_CODE_SIGINT",
    "InteractiveDashboard",
    "ProgressIndicator",
    "format_diff_stat",
    "get_repo_diff_stat",
    "read_single_key",
    "should_use_color",
    "style_text",
]
