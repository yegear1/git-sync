"""
Thread-safe terminal progress indicators and spinners for concurrent worker operations.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any, Optional


class ProgressIndicator:
    """
    Thread-safe, non-blocking terminal progress indicator and spinner for long-running worker operations.
    # INVARIANT: Automatically suppressed when enabled=False (non-TTY, dumb terminals, AI agent sessions, JSON mode, or CI).
    # INVARIANT: Ephemeral progress lines are erased upon finish() to prevent terminal output corruption.
    """
    SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(
        self,
        message: str = "Processing...",
        total: int = 0,
        enabled: Optional[bool] = None,
        stream: Optional[Any] = None,
    ) -> None:
        self.message = message
        self.total = total
        self.stream = stream if stream is not None else sys.stderr
        if enabled is None:
            try:
                is_dumb = os.environ.get("TERM", "").lower() == "dumb"
                is_agent = bool(
                    os.environ.get("ANTIGRAVITY_AGENT")
                    or os.environ.get("CI")
                    or os.environ.get("DEBIAN_FRONTEND") == "noninteractive"
                )
                self.enabled = (
                    getattr(self.stream, "isatty", lambda: False)()
                    and sys.stdout.isatty()
                    and not is_dumb
                    and not is_agent
                )
            except Exception:
                self.enabled = False
        else:
            self.enabled = enabled
        self.count = 0
        self._frame_idx = 0
        self._lock = threading.Lock()

    def advance(self, item_name: str = "") -> None:
        """Increment progress counter and render current frame."""
        with self._lock:
            self.count += 1
            self._frame_idx = (self._frame_idx + 1) % len(self.SPINNER_FRAMES)
            if self.enabled:
                self._render(item_name)

    def update(self, count: int, item_name: str = "") -> None:
        """Set explicit progress counter and render current frame."""
        with self._lock:
            self.count = count
            self._frame_idx = (self._frame_idx + 1) % len(self.SPINNER_FRAMES)
            if self.enabled:
                self._render(item_name)

    def _render(self, item_name: str = "") -> None:
        frame = self.SPINNER_FRAMES[self._frame_idx]
        if self.total > 0:
            pct = int((self.count / self.total) * 100)
            text = f"\r[{self.count:>2}/{self.total:>2}] ({pct:>2}%) {frame} {self.message}"
        else:
            text = f"\r{frame} {self.message} ({self.count})"
        if item_name:
            short_item = item_name if len(item_name) <= 32 else item_name[:29] + "..."
            text += f" ({short_item})"
        self.stream.write(f"{text:<79}")
        self.stream.flush()

    def finish(self) -> None:
        """Clear ephemeral progress line."""
        with self._lock:
            if self.enabled:
                self.stream.write("\r" + " " * 79 + "\r")
                self.stream.flush()

    def __enter__(self) -> "ProgressIndicator":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.finish()


__all__ = ["ProgressIndicator"]
