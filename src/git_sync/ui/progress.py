"""
Thread-safe terminal progress indicators and spinners for concurrent worker operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import sys
import threading
from typing import Any, Dict, List, Optional

from git_sync.ui.style import AnsiStyler


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


@dataclass
class BulkActionItem:
    """Structured representation of a planned repository mutation for preview and safety auditing."""
    repo: str
    action: str
    repo_rel: Optional[str] = None
    branch: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    is_dirty: bool = False
    dirty_files_count: int = 0
    is_protected: bool = False
    submodules_count: int = 0
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert item to structured dictionary."""
        return {
            "repo": self.repo,
            "repo_rel": self.repo_rel or self.repo,
            "action": self.action,
            "branch": self.branch,
            "ahead": self.ahead,
            "behind": self.behind,
            "is_dirty": self.is_dirty,
            "dirty_files_count": self.dirty_files_count,
            "is_protected": self.is_protected,
            "submodules_count": self.submodules_count,
            "warnings": list(self.warnings),
            "details": dict(self.details),
        }


def format_bulk_action_preview(
    items: List[BulkActionItem | Dict[str, Any]],
    title: str = "PRÉVIA DE AÇÕES EM LOTE (BULK ACTION PREVIEW)",
    stream: Optional[Any] = None,
    color: Optional[bool] = None,
) -> str:
    """
    Format planned bulk repository mutations into a visual confirmation preview
    with safety summaries and ANSI indicators.
    # INVARIANT: Preserves exact text alignment and respects NO_COLOR / dumb terminals.
    """
    styler = AnsiStyler(enabled=color, stream=stream)
    lines: List[str] = []

    # Border header
    box_width = 80
    header_content = f" 🔍 {title} "
    header_bar = "═" * (box_width - 2)
    lines.append(styler.cyan(f"╔{header_bar}╗"))
    lines.append(styler.cyan(f"║ {styler.bold(header_content):<{box_width - 4}} ║"))
    lines.append(styler.cyan(f"╚{header_bar}╝"))

    if not items:
        lines.append(styler.dim("  (Nenhum repositório pendente de mutação)"))
        return "\n".join(lines)

    lines.append(styler.bold(f"Repositórios alvo ({len(items)}):"))

    dirty_count = 0
    protected_count = 0
    submodules_total = 0

    for raw in items:
        if isinstance(raw, dict):
            repo_rel = raw.get("repo_rel") or raw.get("repo", "")
            action = raw.get("action", "")
            branch = raw.get("branch")
            ahead = int(raw.get("ahead", 0))
            behind = int(raw.get("behind", 0))
            is_dirty = bool(raw.get("is_dirty", False))
            dirty_files_count = int(raw.get("dirty_files_count", 0))
            is_protected = bool(raw.get("is_protected", False))
            submodules_count = int(raw.get("submodules_count", 0))
            warnings = list(raw.get("warnings", []))
        else:
            repo_rel = raw.repo_rel or raw.repo
            action = raw.action
            branch = raw.branch
            ahead = raw.ahead
            behind = raw.behind
            is_dirty = raw.is_dirty
            dirty_files_count = raw.dirty_files_count
            is_protected = raw.is_protected
            submodules_count = raw.submodules_count
            warnings = list(raw.warnings)

        if is_dirty:
            dirty_count += 1
        if is_protected:
            protected_count += 1
        if submodules_count > 0:
            submodules_total += submodules_count

        branch_part = f" [{branch}]" if branch else ""
        meta_parts: List[str] = []
        if behind > 0:
            meta_parts.append(f"{behind} commit(s) atrás")
        if ahead > 0:
            meta_parts.append(f"{ahead} commit(s) à frente")
        meta_str = f" ({', '.join(meta_parts)})" if meta_parts else ""

        line_repo = f"  • {styler.bold(repo_rel)}{branch_part} ↳ {action}{meta_str}"

        tags: List[str] = []
        if is_dirty:
            tag_text = f"⚠️ [Árvore suja: {dirty_files_count} arquivo(s)]"
            tags.append(styler.yellow(tag_text))
        if is_protected:
            tag_text = f"🛡️ [Branch protegida: {branch}]"
            tags.append(styler.magenta(tag_text))
        for w in warnings:
            tags.append(styler.red(f"⚠️ [{w}]"))

        if tags:
            line_repo += " " + " ".join(tags)
        lines.append(line_repo)

    lines.append("")
    lines.append(styler.bold("📊 Resumo de Segurança:"))
    lines.append(f"  • Total de repositórios a modificar: {styler.bold(str(len(items)))}")
    if dirty_count > 0:
        lines.append(f"  • Repositórios com autostash necessário: {styler.yellow(str(dirty_count))}")
    if protected_count > 0:
        lines.append(f"  • Branches protegidas afetadas: {styler.magenta(str(protected_count))}")
    if submodules_total > 0:
        lines.append(f"  • Submódulos com drift pendente: {str(submodules_total)}")

    if dirty_count > 0 or protected_count > 0:
        lines.append("")
        lines.append(styler.yellow("⚠️  Atenção: Revise as operações acima antes de confirmar."))

    return "\n".join(lines)


def prompt_bulk_confirmation(
    items: List[BulkActionItem | Dict[str, Any]],
    title: str = "PRÉVIA DE AÇÕES EM LOTE (BULK ACTION PREVIEW)",
    prompt_text: Optional[str] = None,
    stream: Optional[Any] = None,
    input_func: Optional[Any] = None,
    auto_yes: bool = False,
) -> bool:
    """
    Prompt user interactively to confirm bulk operations with a formatted preview.
    # INVARIANT: Safe default is abort (False) on empty input, non-yes choices, EOF, or SIGINT.
    # INVARIANT: Automatically returns True if auto_yes is True.
    """
    if auto_yes:
        return True
    if not items:
        return True

    target_stream = stream if stream is not None else sys.stdout
    preview = format_bulk_action_preview(items, title=title, stream=target_stream)
    target_stream.write(preview + "\n\n")
    target_stream.flush()

    if prompt_text is None:
        prompt_text = f"Confirmar execução em {len(items)} repositório(s)? [y/N]: "

    prompt_fn = input_func if input_func is not None else input

    try:
        ans = prompt_fn(prompt_text).strip().lower()
    except (EOFError, KeyboardInterrupt):
        target_stream.write("\nOperação abortada pelo usuário.\n")
        target_stream.flush()
        return False

    if ans in ("y", "yes", "s", "sim"):
        return True

    target_stream.write("Operação cancelada pelo usuário.\n")
    target_stream.flush()
    return False


__all__ = [
    "BulkActionItem",
    "ProgressIndicator",
    "format_bulk_action_preview",
    "prompt_bulk_confirmation",
]
