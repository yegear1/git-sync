"""
Centralized ANSI terminal styling and diff stat formatting helpers for git-sync.

# INVARIANT: Zero external dependencies; standard library only.
# INVARIANT: Strictly respects the NO_COLOR specification (https://no-color.org/) and TTY detection.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Optional

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"

_COLOR_CODES = {
    "bold": ANSI_BOLD,
    "dim": ANSI_DIM,
    "red": ANSI_RED,
    "green": ANSI_GREEN,
    "yellow": ANSI_YELLOW,
    "blue": ANSI_BLUE,
    "magenta": ANSI_MAGENTA,
    "cyan": ANSI_CYAN,
}


def should_use_color(stream: Optional[Any] = None, force: Optional[bool] = None) -> bool:
    """
    Determine whether ANSI color escape sequences should be emitted.
    # INVARIANT: NO_COLOR (when present and non-empty) unconditionally disables ANSI colors unless force=True.
    # INVARIANT: Automatically disables color on non-TTY streams or TERM=dumb.
    """
    if force is not None:
        return force

    # NO_COLOR specification: any non-empty value disables color
    no_color = os.environ.get("NO_COLOR")
    if no_color is not None and no_color != "":
        return False

    force_color = os.environ.get("FORCE_COLOR")
    if force_color is not None and force_color not in ("", "0", "false", "False"):
        return True

    if os.environ.get("TERM", "").lower() == "dumb":
        return False

    target_stream = stream if stream is not None else sys.stdout
    try:
        return bool(getattr(target_stream, "isatty", lambda: False)())
    except Exception:
        return False


class AnsiStyler:
    """
    Terminal ANSI styling helper that gracefully degrades to plain text when color is disabled.
    """

    def __init__(self, enabled: Optional[bool] = None, stream: Optional[Any] = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = should_use_color(stream=self.stream, force=enabled)

    def style(self, text: str, *styles: str) -> str:
        """Apply one or more named ANSI styles ('bold', 'green', 'red', etc.) to text."""
        if not self.enabled or not text or not styles:
            return text
        prefix = "".join(_COLOR_CODES.get(s, "") for s in styles)
        if not prefix:
            return text
        return f"{prefix}{text}{ANSI_RESET}"

    def bold(self, text: str) -> str:
        return self.style(text, "bold")

    def dim(self, text: str) -> str:
        return self.style(text, "dim")

    def red(self, text: str) -> str:
        return self.style(text, "red")

    def green(self, text: str) -> str:
        return self.style(text, "green")

    def yellow(self, text: str) -> str:
        return self.style(text, "yellow")

    def blue(self, text: str) -> str:
        return self.style(text, "blue")

    def magenta(self, text: str) -> str:
        return self.style(text, "magenta")

    def cyan(self, text: str) -> str:
        return self.style(text, "cyan")


def style_text(
    text: str,
    *styles: str,
    enabled: Optional[bool] = None,
    stream: Optional[Any] = None,
) -> str:
    """Convenience function to style text with ANSI codes when enabled."""
    return AnsiStyler(enabled=enabled, stream=stream).style(text, *styles)


_DIFF_STAT_BAR_RE = re.compile(r"^(\s*.+?\|\s*\d+\s+)([+-]+)(.*)$")
_DIFF_SUMMARY_INSERT_RE = re.compile(r"(\d+\s+insertions?\(\+\))")
_DIFF_SUMMARY_DELETE_RE = re.compile(r"(\d+\s+deletions?\(-\))")


def format_diff_stat(
    stat_text: str,
    enabled: Optional[bool] = None,
    stream: Optional[Any] = None,
) -> str:
    """
    Format `git diff --stat` output or unified diff lines with ANSI colors:
    - `+` bars and insertion counts in green (`\033[32m`)
    - `-` bars and deletion counts in red (`\033[31m`)
    # INVARIANT: Preserves exact character layout and returns unmodified text when color is disabled.
    """
    if not stat_text:
        return stat_text

    styler = AnsiStyler(enabled=enabled, stream=stream)
    if not styler.enabled:
        return stat_text

    formatted_lines = []
    for line in stat_text.splitlines(keepends=True):
        # Extract line ending to preserve it accurately
        ending = ""
        content = line
        if content.endswith("\r\n"):
            ending = "\r\n"
            content = content[:-2]
        elif content.endswith("\n"):
            ending = "\n"
            content = content[:-1]

        bar_match = _DIFF_STAT_BAR_RE.match(content)
        if bar_match:
            prefix, bars, suffix = bar_match.groups()
            plus_count = bars.count("+")
            minus_count = bars.count("-")
            colored_bars = ""
            if plus_count:
                colored_bars += styler.green("+" * plus_count)
            if minus_count:
                colored_bars += styler.red("-" * minus_count)
            formatted_lines.append(f"{prefix}{colored_bars}{suffix}{ending}")
            continue

        # Check summary line: e.g., " 2 files changed, 15 insertions(+), 4 deletions(-)"
        if "changed" in content and ("insertion" in content or "deletion" in content):
            colored_summary = _DIFF_SUMMARY_INSERT_RE.sub(
                lambda m: styler.green(m.group(1)), content
            )
            colored_summary = _DIFF_SUMMARY_DELETE_RE.sub(
                lambda m: styler.red(m.group(1)), colored_summary
            )
            formatted_lines.append(f"{colored_summary}{ending}")
            continue

        # Unified diff lines fallback (+ / - lines)
        if content.startswith("+++") or content.startswith("---"):
            formatted_lines.append(f"{styler.bold(content)}{ending}")
        elif content.startswith("+"):
            formatted_lines.append(f"{styler.green(content)}{ending}")
        elif content.startswith("-"):
            formatted_lines.append(f"{styler.red(content)}{ending}")
        elif content.startswith("@@"):
            formatted_lines.append(f"{styler.cyan(content)}{ending}")
        else:
            formatted_lines.append(line)

    return "".join(formatted_lines)


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
    "format_diff_stat",
    "should_use_color",
    "style_text",
]
