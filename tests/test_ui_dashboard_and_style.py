"""
Unit tests for ANSI NO_COLOR styling, colored diff --stat formatting, and interactive dashboard loop.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.core import run_git
from git_sync.ui.dashboard import (
    EXIT_CODE_SIGINT,
    InteractiveDashboard,
    get_repo_diff_stat,
    read_single_key,
)
from git_sync.ui.style import (
    ANSI_BOLD,
    ANSI_GREEN,
    ANSI_RED,
    ANSI_RESET,
    AnsiStyler,
    format_diff_stat,
    should_use_color,
    style_text,
)


class FakeTTYStream(io.StringIO):
    """Stream stub reporting isatty() == True."""

    def isatty(self) -> bool:
        return True


class TestAnsiStyleAndDiffStat(unittest.TestCase):
    """Test centralized ANSI styling helper and colored diff --stat formatting."""

    def test_no_color_env_disables_ansi_on_tty(self) -> None:
        tty_stream = FakeTTYStream()
        with patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm-256color"}, clear=False):
            self.assertFalse(should_use_color(stream=tty_stream))
            styler = AnsiStyler(stream=tty_stream)
            self.assertFalse(styler.enabled)
            self.assertEqual(styler.green("OK"), "OK")

    def test_empty_no_color_allows_tty_colors(self) -> None:
        tty_stream = FakeTTYStream()
        with patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm-256color"}, clear=False):
            self.assertTrue(should_use_color(stream=tty_stream))
            styler = AnsiStyler(stream=tty_stream)
            self.assertTrue(styler.enabled)
            self.assertIn(ANSI_GREEN, styler.green("OK"))
            self.assertIn(ANSI_RESET, styler.green("OK"))

    def test_non_tty_stream_disables_ansi(self) -> None:
        non_tty = io.StringIO()
        with patch.dict(os.environ, {"NO_COLOR": "", "FORCE_COLOR": "", "TERM": "xterm-256color"}, clear=False):
            self.assertFalse(should_use_color(stream=non_tty))
            self.assertEqual(style_text("plain", "red", stream=non_tty), "plain")

    def test_dumb_terminal_disables_ansi(self) -> None:
        tty_stream = FakeTTYStream()
        with patch.dict(os.environ, {"NO_COLOR": "", "FORCE_COLOR": "", "TERM": "dumb"}, clear=False):
            self.assertFalse(should_use_color(stream=tty_stream))

    def test_force_color_overrides_non_tty(self) -> None:
        non_tty = io.StringIO()
        with patch.dict(os.environ, {"NO_COLOR": "", "FORCE_COLOR": "1"}, clear=False):
            self.assertTrue(should_use_color(stream=non_tty))

    def test_format_diff_stat_colors_plus_green_and_minus_red(self) -> None:
        raw_stat = (
            " src/git_sync/cli.py | 12 ++++++++----\n"
            " README.md           |  4 ++++\n"
            " old.py              |  3 ---\n"
            " 3 files changed, 12 insertions(+), 7 deletions(-)\n"
        )
        colored = format_diff_stat(raw_stat, enabled=True)
        self.assertIn(f"{ANSI_GREEN}++++++++{ANSI_RESET}", colored)
        self.assertIn(f"{ANSI_RED}----{ANSI_RESET}", colored)
        self.assertIn(f"{ANSI_GREEN}++++{ANSI_RESET}", colored)
        self.assertIn(f"{ANSI_RED}---{ANSI_RESET}", colored)
        self.assertIn(f"{ANSI_GREEN}12 insertions(+){ANSI_RESET}", colored)
        self.assertIn(f"{ANSI_RED}7 deletions(-){ANSI_RESET}", colored)

        # Disabled color preserves exact string
        plain = format_diff_stat(raw_stat, enabled=False)
        self.assertEqual(plain, raw_stat)

    def test_format_diff_stat_unified_diff_lines(self) -> None:
        unified = (
            "--- a/file.txt\n"
            "+++ b/file.txt\n"
            "@@ -1,2 +1,2 @@\n"
            "-old line\n"
            "+new line\n"
        )
        colored = format_diff_stat(unified, enabled=True)
        self.assertIn(f"{ANSI_BOLD}--- a/file.txt{ANSI_RESET}", colored)
        self.assertIn(f"{ANSI_BOLD}+++ b/file.txt{ANSI_RESET}", colored)
        self.assertIn(f"{ANSI_RED}-old line{ANSI_RESET}", colored)
        self.assertIn(f"{ANSI_GREEN}+new line{ANSI_RESET}", colored)


class TestReadSingleKey(unittest.TestCase):
    """Test single-key input reader with non-TTY fallback and signal handling."""

    def test_read_single_key_line_fallback(self) -> None:
        buf = io.StringIO("\np\nr\nq\n")
        self.assertEqual(read_single_key(buf), "ENTER")
        self.assertEqual(read_single_key(buf), "p")
        self.assertEqual(read_single_key(buf), "r")
        self.assertEqual(read_single_key(buf), "q")
        with self.assertRaises(EOFError):
            read_single_key(buf)

    def test_read_single_key_ctrl_c_and_ctrl_d(self) -> None:
        buf_c = io.StringIO("\x03\n")
        with self.assertRaises(KeyboardInterrupt):
            read_single_key(buf_c)

        buf_d = io.StringIO("\x04\n")
        with self.assertRaises(EOFError):
            read_single_key(buf_d)


class TestInteractiveDashboard(unittest.TestCase):
    """Test interactive terminal dashboard actions, navigation, and exit codes."""

    def _sample_repos(self) -> list[dict]:
        return [
            {
                "name": "repo-alpha",
                "path": "/workspace/org/repo-alpha",
                "rel_path": "org/repo-alpha",
                "branch": "main",
                "status": "PULL_PENDING",
                "behind": 2,
                "ahead": 0,
                "aux_branches_behind": {},
                "is_dirty": False,
                "dirty_files": [],
                "is_worktree": False,
            },
            {
                "name": "repo-beta",
                "path": "/workspace/org/repo-beta",
                "rel_path": "org/repo-beta",
                "branch": "feat/ui",
                "status": "UP_TO_DATE",
                "behind": 0,
                "ahead": 1,
                "aux_branches_behind": {},
                "is_dirty": True,
                "dirty_files": [" M src/app.py"],
                "is_worktree": False,
            },
        ]

    def test_dashboard_quit_returns_zero(self) -> None:
        out = io.StringIO()
        keys = iter(["q"])
        dash = InteractiveDashboard(
            root="/workspace",
            repos=self._sample_repos(),
            stream=out,
            color_enabled=False,
            key_reader=lambda: next(keys),
        )
        code = dash.run()
        self.assertEqual(code, 0)
        rendered = out.getvalue()
        self.assertIn("GIT-SYNC DASHBOARD", rendered)
        self.assertIn("org/repo-alpha", rendered)
        self.assertIn("org/repo-beta", rendered)
        self.assertIn("Saindo do dashboard", rendered)

    def test_dashboard_sigint_and_eof_return_130(self) -> None:
        for exc_cls in (KeyboardInterrupt, EOFError):
            with self.subTest(exc=exc_cls.__name__):
                out = io.StringIO()

                def raise_exc() -> str:
                    raise exc_cls()

                dash = InteractiveDashboard(
                    root="/workspace",
                    repos=self._sample_repos(),
                    stream=out,
                    color_enabled=False,
                    key_reader=raise_exc,
                )
                code = dash.run()
                self.assertEqual(code, EXIT_CODE_SIGINT)
                self.assertEqual(code, 130)

    def test_dashboard_enter_pulls_pending_repos(self) -> None:
        out = io.StringIO()
        pulled_paths: list[str] = []
        repos = self._sample_repos()

        def fake_pull(repo: dict) -> dict:
            pulled_paths.append(repo["rel_path"])
            repo["status"] = "UP_TO_DATE"
            repo["behind"] = 0
            return {"success": True, "stdout": "Updated", "stderr": ""}

        keys = iter(["ENTER", "q"])
        dash = InteractiveDashboard(
            root="/workspace",
            repos=repos,
            stream=out,
            color_enabled=False,
            key_reader=lambda: next(keys),
            inspect_fn=lambda _: repos,
            pull_fn=fake_pull,
        )
        code = dash.run()
        self.assertEqual(code, 0)
        self.assertEqual(pulled_paths, ["org/repo-alpha"])
        self.assertIn("Pull concluído em 1/1 repositório(s)", out.getvalue())

    def test_dashboard_navigate_and_pull_selected(self) -> None:
        out = io.StringIO()
        pulled_paths: list[str] = []
        repos = self._sample_repos()

        def fake_pull(repo: dict) -> dict:
            pulled_paths.append(repo["rel_path"])
            return {"success": True, "stdout": "Already up to date", "stderr": ""}

        # Move down ('j') from index 0 (repo-alpha) to index 1 (repo-beta), then press 'p' to pull selected
        keys = iter(["j", "p", "q"])
        dash = InteractiveDashboard(
            root="/workspace",
            repos=repos,
            stream=out,
            color_enabled=False,
            key_reader=lambda: next(keys),
            inspect_fn=lambda _: repos,
            pull_fn=fake_pull,
        )
        code = dash.run()
        self.assertEqual(code, 0)
        self.assertEqual(pulled_paths, ["org/repo-beta"])
        self.assertIn("Pull concluído para 'org/repo-beta'", out.getvalue())

    def test_dashboard_refresh_and_inspect_dirty_diff(self) -> None:
        out = io.StringIO()
        repos = self._sample_repos()
        refresh_calls: list[bool] = []

        def fake_inspect(do_fetch: bool) -> list[dict]:
            refresh_calls.append(do_fetch)
            return repos

        def fake_diff_stat(repo: dict) -> str:
            return " src/app.py | 5 +++--\n 1 file changed, 3 insertions(+), 2 deletions(-)"

        keys = iter(["r", "d", "q"])
        dash = InteractiveDashboard(
            root="/workspace",
            repos=repos,
            do_fetch=False,
            stream=out,
            color_enabled=True,
            key_reader=lambda: next(keys),
            inspect_fn=fake_inspect,
            diff_stat_fn=fake_diff_stat,
        )
        code = dash.run()
        self.assertEqual(code, 0)
        self.assertEqual(len(refresh_calls), 1)
        output = out.getvalue()
        self.assertIn("Status dos repositórios atualizado", output)
        self.assertIn("Diff / Dirty Inspection: org/repo-beta", output)
        self.assertIn("M src/app.py", output)
        self.assertIn(f"{ANSI_GREEN}+++{ANSI_RESET}", output)
        self.assertIn(f"{ANSI_RED}--{ANSI_RESET}", output)

    def test_cli_dashboard_integration_and_diff_stat_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "demo-repo"
            repo.mkdir(parents=True)
            run_git(str(repo), ["init", "-b", "main"])
            run_git(str(repo), ["config", "user.email", "test@example.com"])
            run_git(str(repo), ["config", "user.name", "Test User"])
            (repo / "hello.py").write_text("print('v1')\n", encoding="utf-8")
            run_git(str(repo), ["add", "hello.py"])
            run_git(str(repo), ["commit", "-m", "initial"])

            # Modify tracked file to produce a working-tree diff --stat
            (repo / "hello.py").write_text("print('v2')\nprint('v3')\n", encoding="utf-8")
            stat_out = get_repo_diff_stat(str(repo), branch="main")
            self.assertIn("hello.py", stat_out)

            # Invoke CLI `git-sync dashboard --no-fetch` with simulated 'd' then 'q'
            stdin_sim = io.StringIO("d\nq\n")
            stdout_sim = io.StringIO()
            with patch("sys.stdin", stdin_sim), patch("sys.stdout", stdout_sim):
                exit_code = main(["--root", str(root), "dashboard", "--no-fetch"])
            self.assertEqual(exit_code, 0)
            cli_out = stdout_sim.getvalue()
            self.assertIn("GIT-SYNC DASHBOARD", cli_out)
            self.assertIn("demo-repo", cli_out)
            self.assertIn("hello.py", cli_out)


if __name__ == "__main__":
    unittest.main()
