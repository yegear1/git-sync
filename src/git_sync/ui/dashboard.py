"""
High-density interactive terminal dashboard for live multi-repository status and actions.

# INVARIANT: Zero external dependencies; standard library only (sys, os, termios, tty).
# INVARIANT: Returns exit code 130 on SIGINT (Ctrl+C) or EOF (Ctrl+D / stream end).
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from git_sync.engine import (
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_WORKERS,
    AdaptiveConcurrencyController,
    discover_repos,
    format_changelog_summary,
    get_divergence_diff,
    get_repo_pending_changelog,
    inspect_repo,
    pull_repo,
    run_git,
)
from git_sync.telemetry import BulkCache
from git_sync.ui.style import AnsiStyler, format_diff_stat

EXIT_CODE_SIGINT = 130

STATUS_BADGES: Dict[str, tuple[str, str]] = {
    "UP_TO_DATE": ("✅ OK", "green"),
    "PULL_PENDING": ("⬇️ PULL", "cyan"),
    "PUSH_PENDING": ("⬆️ PUSH", "magenta"),
    "DIVERGED": ("⚠️ DIVERGIU", "yellow"),
    "AUX_PULL_PENDING": ("⬇️ AUX_PULL", "cyan"),
    "AUX_PUSH_PENDING": ("⬆️ AUX_PUSH", "magenta"),
    "NO_UPSTREAM": ("⚠️ SEM UPSTREAM", "yellow"),
    "FETCH_ERROR": ("❌ ERRO FETCH", "red"),
    "NO_REMOTE": ("❌ SEM REMOTE", "red"),
}


def read_single_key(stream: Optional[Any] = None) -> str:
    """
    Read a single keypress or command token from input stream.
    Uses raw tty mode when stream is a real TTY with termios support; falls back to readline() otherwise.
    Raises KeyboardInterrupt on Ctrl+C ('\\x03') and EOFError on Ctrl+D ('\\x04') or empty EOF.
    # INVARIANT: Terminal attributes are unconditionally restored in a finally block.
    """
    in_stream = stream if stream is not None else sys.stdin
    try:
        is_tty = bool(getattr(in_stream, "isatty", lambda: False)())
        fd = in_stream.fileno() if is_tty else -1
    except Exception:
        is_tty = False
        fd = -1

    if is_tty and fd >= 0:
        try:
            import termios
            import tty
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = in_stream.read(1)
                if not ch or ch == "\x04":
                    raise EOFError()
                if ch == "\x03":
                    raise KeyboardInterrupt()
                if ch == "\x1b":
                    # Read escape sequence for arrow keys
                    seq = in_stream.read(2)
                    if seq == "[A":
                        return "UP"
                    if seq == "[B":
                        return "DOWN"
                    return "ESC"
                if ch in ("\r", "\n"):
                    return "ENTER"
                return ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except (ImportError, OSError, termios.error):
            pass

    line = in_stream.readline()
    if line == "":
        raise EOFError()
    if "\x03" in line:
        raise KeyboardInterrupt()
    if "\x04" in line:
        raise EOFError()
    stripped = line.rstrip("\r\n")
    if stripped == "":
        return "ENTER"
    return stripped


def get_repo_diff_stat(repo_path: str, branch: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> str:
    """
    Retrieve combined working tree `git diff --stat` (HEAD or staged/unstaged) and divergence stat if applicable.
    """
    parts: List[str] = []
    proc_wt = run_git(repo_path, ["diff", "HEAD", "--stat"], timeout=timeout)
    if proc_wt.returncode != 0 or not proc_wt.stdout.strip():
        proc_wt = run_git(repo_path, ["diff", "--stat"], timeout=timeout)
    if proc_wt.returncode == 0 and proc_wt.stdout.strip():
        parts.append(proc_wt.stdout.rstrip())

    div_stat = get_divergence_diff(repo_path, branch=branch, timeout=timeout, stat_only=True)
    if div_stat and div_stat.strip():
        parts.append(div_stat.rstrip())

    return "\n\n".join(parts)


class InteractiveDashboard:
    """
    High-density interactive terminal dashboard for multi-repository status and single-key operations.
    Supports:
      - `Enter`: Pull all repositories with pending updates (`PULL_PENDING` / `AUX_PULL_PENDING`)
      - `p`: Pull currently selected repository
      - `r`: Refresh repository statuses
      - `d`: Inspect dirty files and colored `git diff --stat` for selected (or dirty) repositories
      - `j` / `DOWN` / `+`: Move selection down
      - `k` / `UP` / `-`: Move selection up
      - `q`: Quit dashboard (returns 0)
      - `SIGINT` / `EOF`: Clean exit returning 130
    """

    def __init__(
        self,
        root: str,
        repos: Optional[List[Dict[str, Any]]] = None,
        do_fetch: bool = False,
        workers: int | str = DEFAULT_WORKERS,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        remote: Optional[str] = None,
        exclude: Optional[List[str]] = None,
        group: Optional[str] = None,
        groups: Optional[List[str]] = None,
        ff_only: bool = True,
        sync_branches: bool = True,
        autostash: bool = True,
        dry_run: bool = False,
        stream: Optional[Any] = None,
        color_enabled: Optional[bool] = None,
        key_reader: Optional[Callable[[], str]] = None,
        inspect_fn: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        pull_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        diff_stat_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> None:
        self.root = root
        self.do_fetch = do_fetch
        self.workers = workers
        self.timeout = timeout
        self.retries = retries
        self.remote = remote
        self.exclude = exclude
        self.group = group
        self.groups = groups
        self.ff_only = ff_only
        self.sync_branches = sync_branches
        self.autostash = autostash
        self.dry_run = dry_run
        self.stream = stream if stream is not None else sys.stdout
        self.styler = AnsiStyler(enabled=color_enabled, stream=self.stream)
        self.key_reader = key_reader if key_reader is not None else (lambda: read_single_key(sys.stdin))
        self._inspect_fn = inspect_fn
        self._pull_fn = pull_fn
        self._diff_stat_fn = diff_stat_fn
        self._has_initial_repos = repos is not None
        self.repos: List[Dict[str, Any]] = list(repos) if repos is not None else []
        self.selected_idx: int = 0
        self.last_message: str = ""

    def _write(self, text: str = "") -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def refresh_repos(self, do_fetch: Optional[bool] = None) -> List[Dict[str, Any]]:
        """Inspect all workspace repositories concurrently and update dashboard state."""
        fetch_flag = self.do_fetch if do_fetch is None else do_fetch
        if self._inspect_fn is not None:
            self.repos = self._inspect_fn(fetch_flag)
        else:
            all_paths = discover_repos(self.root, exclude=self.exclude)
            effective_groups = [self.group] if self.group else (self.groups or [])
            if effective_groups:
                all_paths = [
                    r for r in all_paths
                    if any(os.path.relpath(r, self.root).startswith(g) for g in effective_groups)
                ]
            if not all_paths:
                self.repos = []
                self.selected_idx = 0
                return self.repos

            cache = BulkCache(enabled=True)
            controller = AdaptiveConcurrencyController()
            configured_w = None if self.workers == "auto" else int(self.workers)
            eff_workers = controller.determine_workers(
                len(all_paths),
                is_fetch=fetch_flag,
                configured_workers=configured_w,
            )
            inspected: List[Dict[str, Any]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=eff_workers) as executor:
                futures = [
                    executor.submit(
                        inspect_repo,
                        r,
                        self.root,
                        fetch_flag,
                        self.timeout,
                        cache,
                        self.remote,
                        None,
                        self.retries,
                    )
                    for r in all_paths
                ]
                for fut in concurrent.futures.as_completed(futures):
                    inspected.append(fut.result())
            inspected.sort(key=lambda x: x["rel_path"])
            self.repos = inspected

        if self.repos:
            self.selected_idx = max(0, min(self.selected_idx, len(self.repos) - 1))
        else:
            self.selected_idx = 0
        return self.repos

    def format_row(self, repo: Dict[str, Any], selected: bool = False) -> str:
        """Render a single compact repository row for the dashboard table."""
        pointer = self.styler.bold(self.styler.cyan("▸")) if selected else " "
        raw_status = repo.get("status", "UNKNOWN")
        badge_text, badge_color = STATUS_BADGES.get(raw_status, (raw_status, "yellow"))
        if raw_status == "FETCH_ERROR" and repo.get("network_state") == "offline":
            badge_text, badge_color = ("🌐 OFFLINE", "yellow")

        padded_badge = f"{badge_text:<14}"
        styled_badge = self.styler.style(padded_badge, badge_color, *(["bold"] if selected else []))

        rel_path = repo.get("rel_path", repo.get("name", ""))
        if len(rel_path) > 36:
            rel_path = rel_path[:33] + "..."
        padded_repo = f"{rel_path:<36}"
        if selected:
            padded_repo = self.styler.bold(padded_repo)

        branch = repo.get("branch") or "-"
        if len(branch) > 14:
            branch = branch[:11] + "..."
        padded_branch = self.styler.cyan(f"{branch:<14}")

        details: List[str] = []
        if repo.get("behind", 0) > 0:
            details.append(self.styler.cyan(f"↓{repo['behind']}"))
        if repo.get("ahead", 0) > 0:
            details.append(self.styler.magenta(f"↑{repo['ahead']}"))
        if repo.get("changelog"):
            cl = repo["changelog"]
            if cl.get("has_breaking"):
                details.append(self.styler.red("!BREAKING"))
            if cl.get("incoming_categories"):
                inc_s = format_changelog_summary(cl["incoming_categories"])
                details.append(self.styler.cyan(f"inc[{inc_s}]"))
            if cl.get("outgoing_categories"):
                out_s = format_changelog_summary(cl["outgoing_categories"])
                details.append(self.styler.magenta(f"out[{out_s}]"))
        if repo.get("aux_branches_behind"):
            aux_cnt = len(repo["aux_branches_behind"])
            details.append(self.styler.cyan(f"aux↓{aux_cnt}"))
        if repo.get("is_dirty"):
            dirty_cnt = len(repo.get("dirty_files", []))
            details.append(self.styler.yellow(f"dirty({dirty_cnt})"))
        if repo.get("is_worktree"):
            details.append(self.styler.dim("[worktree]"))
        if repo.get("stale_worktrees"):
            details.append(self.styler.yellow(f"wt_stale({len(repo['stale_worktrees'])})"))
        if repo.get("sparse_checkout", {}).get("enabled"):
            sc = repo["sparse_checkout"]
            tag = "cone" if sc.get("cone_mode") else "full"
            warn = "!" if sc.get("warning") else ""
            details.append(self.styler.dim(f"[sparse:{tag}{warn}]"))
        if not details:
            details.append(self.styler.dim("sincronizado"))

        return f" {pointer} {styled_badge} {padded_repo} {padded_branch} {' '.join(details)}"

    def render(self) -> str:
        """Render full high-density dashboard screen string."""
        lines: List[str] = []
        header_title = self.styler.bold(f"📊 GIT-SYNC DASHBOARD — {self.root}")
        lines.append(header_title)
        lines.append("=" * 92)

        pending_pull = sum(1 for r in self.repos if "PULL" in r.get("status", "") or r.get("aux_branches_behind"))
        pending_push = sum(1 for r in self.repos if "PUSH" in r.get("status", ""))
        dirty_count = sum(1 for r in self.repos if r.get("is_dirty"))
        diverged_count = sum(1 for r in self.repos if r.get("status") == "DIVERGED")

        summary_bar = (
            f"Total: {self.styler.bold(str(len(self.repos)))} | "
            f"Pull: {self.styler.cyan(str(pending_pull))} | "
            f"Push: {self.styler.magenta(str(pending_push))} | "
            f"Divergidos: {self.styler.yellow(str(diverged_count))} | "
            f"Dirty: {self.styler.yellow(str(dirty_count))}"
        )
        lines.append(summary_bar)
        lines.append("-" * 92)
        col_header = f"   {'STATUS':<14} {'REPOSITÓRIO':<36} {'BRANCH':<14} {'DETALHES'}"
        lines.append(self.styler.dim(col_header))
        lines.append("-" * 92)

        if not self.repos:
            lines.append("   (Nenhum repositório encontrado)")
        else:
            for idx, r in enumerate(self.repos):
                lines.append(self.format_row(r, selected=(idx == self.selected_idx)))

        lines.append("=" * 92)
        if self.last_message:
            lines.append(f"💬 {self.last_message}")
            lines.append("-" * 92)

        controls = (
            "[Enter] Pull pendentes  [p] Pull selecionado  [r] Atualizar  "
            "[c] Changelog  [d] Dirty/Diff  [j/k] Navegar  [q] Sair"
        )
        lines.append(self.styler.bold(controls))
        output = "\n".join(lines)
        self._write(output)
        return output

    def _execute_pull(self, target_repo: Dict[str, Any]) -> Dict[str, Any]:
        if self._pull_fn is not None:
            return self._pull_fn(target_repo)
        return pull_repo(
            target_repo["path"],
            remote=self.remote,
            ff_only=self.ff_only,
            sync_branches=self.sync_branches,
            autostash=self.autostash,
            timeout=self.timeout,
            dry_run=self.dry_run,
            retries=self.retries,
        )

    def action_pull_pending(self) -> List[Dict[str, Any]]:
        """Pull all repositories that have pending upstream changes."""
        pending = [
            r for r in self.repos
            if "PULL" in r.get("status", "") or r.get("aux_branches_behind")
        ]
        if not pending:
            self.last_message = self.styler.green("✅ Nenhum repositório pendente de pull.")
            return []

        results: List[Dict[str, Any]] = []
        ok_count = 0
        for r in pending:
            res = self._execute_pull(r)
            results.append(res)
            if res.get("success"):
                ok_count += 1

        self.refresh_repos(do_fetch=False)
        if ok_count == len(pending):
            self.last_message = self.styler.green(f"✅ Pull concluído em {ok_count}/{len(pending)} repositório(s).")
        else:
            fail_count = len(pending) - ok_count
            self.last_message = self.styler.yellow(
                f"⚠️ Pull concluído: {ok_count} OK, {fail_count} falha(s)."
            )
        return results

    def action_pull_selected(self) -> Optional[Dict[str, Any]]:
        """Pull the currently selected repository."""
        if not self.repos:
            self.last_message = self.styler.yellow("⚠️ Nenhum repositório selecionado.")
            return None

        target = self.repos[self.selected_idx]
        rel = target.get("rel_path", target.get("name", ""))
        res = self._execute_pull(target)
        self.refresh_repos(do_fetch=False)
        if res.get("success"):
            self.last_message = self.styler.green(f"✅ Pull concluído para '{rel}'.")
        else:
            err = res.get("stderr") or res.get("stdout") or "Erro no pull"
            self.last_message = self.styler.red(f"❌ Falha no pull de '{rel}': {err}")
        return res

    def action_inspect_dirty_diff(self) -> str:
        """Inspect uncommitted files and colored diff --stat for selected or dirty repositories."""
        if not self.repos:
            msg = "⚠️ Nenhum repositório disponível para inspeção."
            self.last_message = self.styler.yellow(msg)
            return msg

        selected = self.repos[self.selected_idx]
        targets = [selected] if (selected.get("is_dirty") or selected.get("status") == "DIVERGED") else [
            r for r in self.repos if r.get("is_dirty") or r.get("status") == "DIVERGED"
        ]
        if not targets:
            targets = [selected]

        blocks: List[str] = []
        for r in targets:
            rel = r.get("rel_path", r.get("name", ""))
            dirty_files = r.get("dirty_files", [])
            header = self.styler.bold(f"📂 Diff / Dirty Inspection: {rel} ({r.get('branch', '-')})")
            sub_lines = [header]
            if dirty_files:
                for df in dirty_files[:15]:
                    sub_lines.append(f"   • {df}")
                if len(dirty_files) > 15:
                    sub_lines.append(f"   ... (+{len(dirty_files) - 15} arquivos)")
            if self._diff_stat_fn is not None:
                raw_stat = self._diff_stat_fn(r)
            else:
                raw_stat = get_repo_diff_stat(r["path"], branch=r.get("branch"), timeout=self.timeout)
            if raw_stat:
                colored = format_diff_stat(raw_stat, enabled=self.styler.enabled, stream=self.stream)
                sub_lines.append(colored)
            elif not dirty_files:
                sub_lines.append("   (Sem alterações locais ou diff pendente)")
            blocks.append("\n".join(sub_lines))

        report = "\n\n".join(blocks)
        self._write("\n" + report + "\n")
        self.last_message = f"🔍 Inspecionado(s) {len(targets)} repositório(s)."
        return report

    def action_inspect_changelog(self) -> str:
        """Inspect and summarize Conventional Commit changelog for selected repository."""
        if not self.repos:
            msg = "⚠️ Nenhum repositório disponível para inspeção."
            self.last_message = self.styler.yellow(msg)
            return msg

        selected = self.repos[self.selected_idx]
        rel = selected.get("rel_path", selected.get("name", ""))
        upstream = selected.get("upstream")
        ahead = selected.get("ahead", 0)
        behind = selected.get("behind", 0)

        cl = selected.get("changelog")
        if not cl or (not cl.get("incoming") and not cl.get("outgoing") and (ahead > 0 or behind > 0)):
            cl = get_repo_pending_changelog(
                selected["path"],
                upstream=upstream,
                ahead=ahead,
                behind=behind,
                timeout=self.timeout,
            )
            selected["changelog"] = cl

        lines: List[str] = []
        header = self.styler.bold(f"📋 Changelog: {rel} ({selected.get('branch', '-')})")
        lines.append(header)

        if not cl.get("incoming") and not cl.get("outgoing"):
            lines.append("   (Sem commits pendentes entre HEAD e upstream)")
        else:
            if cl.get("incoming"):
                inc_hdr = self.styler.cyan(f"⬇️ Commits Recebidos (Incoming - {len(cl['incoming'])}):")
                lines.append(f"   {inc_hdr}")
                for c in cl["incoming"]:
                    cat_badge = self.styler.style(f"[{c['category']}]", "yellow" if c.get("is_breaking") else "green")
                    brk = self.styler.red(" [BREAKING]") if c.get("is_breaking") else ""
                    lines.append(f"     • {cat_badge}{brk} {c['subject']} ({c['hash']})")
            if cl.get("outgoing"):
                out_hdr = self.styler.magenta(f"⬆️ Commits Locais (Outgoing - {len(cl['outgoing'])}):")
                lines.append(f"   {out_hdr}")
                for c in cl["outgoing"]:
                    cat_badge = self.styler.style(f"[{c['category']}]", "yellow" if c.get("is_breaking") else "cyan")
                    brk = self.styler.red(" [BREAKING]") if c.get("is_breaking") else ""
                    lines.append(f"     • {cat_badge}{brk} {c['subject']} ({c['hash']})")

        report = "\n".join(lines)
        self._write("\n" + report + "\n")
        self.last_message = f"📋 Changelog de '{rel}' inspecionado."
        return report

    def run(self) -> int:
        """
        Execute the interactive single-key command loop.
        # INVARIANT: Returns 0 on 'q' quit and 130 on SIGINT (Ctrl+C) or EOF.
        """
        try:
            if not self._has_initial_repos:
                self.refresh_repos(do_fetch=self.do_fetch)
                self._has_initial_repos = True

            while True:
                self.render()
                key = self.key_reader()
                norm_key = key.strip() if key != "ENTER" else "ENTER"

                if norm_key in ("q", "Q", "ESC"):
                    self._write("👋 Saindo do dashboard.")
                    return 0
                elif norm_key == "ENTER":
                    self.action_pull_pending()
                elif norm_key in ("p", "P"):
                    self.action_pull_selected()
                elif norm_key in ("r", "R"):
                    self.refresh_repos(do_fetch=self.do_fetch)
                    self.last_message = self.styler.cyan("🔄 Status dos repositórios atualizado.")
                elif norm_key in ("c", "C"):
                    self.action_inspect_changelog()
                elif norm_key in ("d", "D"):
                    self.action_inspect_dirty_diff()
                elif norm_key in ("j", "J", "DOWN", "+"):
                    if self.repos:
                        self.selected_idx = (self.selected_idx + 1) % len(self.repos)
                        self.last_message = ""
                elif norm_key in ("k", "K", "UP", "-"):
                    if self.repos:
                        self.selected_idx = (self.selected_idx - 1) % len(self.repos)
                        self.last_message = ""
                elif norm_key.isdigit() and self.repos:
                    idx = int(norm_key) - 1
                    if 0 <= idx < len(self.repos):
                        self.selected_idx = idx
                        self.last_message = f"Selecionado: {self.repos[idx].get('rel_path')}"
                    else:
                        self.last_message = self.styler.yellow(f"⚠️ Índice fora do intervalo (1-{len(self.repos)}).")
                else:
                    self.last_message = self.styler.yellow(
                        f"⚠️ Tecla '{norm_key}' não reconhecida. Use Enter, p, r, c, d, j/k ou q."
                    )
        except (KeyboardInterrupt, EOFError):
            self._write("\n⚠️ Dashboard encerrado (SIGINT/EOF).")
            return EXIT_CODE_SIGINT


__all__ = [
    "EXIT_CODE_SIGINT",
    "InteractiveDashboard",
    "STATUS_BADGES",
    "get_repo_diff_stat",
    "read_single_key",
]
