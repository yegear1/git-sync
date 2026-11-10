"""
Unit tests for token-budget optimization, diffstat summarization, and concise single-line reporting
in git-sync pull and git-sync push.

# INVARIANT: Strict token budget: each compact repository entry must consume <150 tokens (~600 chars).
# INVARIANT: Zero external dependencies; standard library only.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.engine import (
    DEFAULT_CHARS_PER_TOKEN,
    PullResult,
    PushResult,
    parse_diffstat,
    run_git,
)
from git_sync.ui.formatters import (
    compact_pull_item,
    compact_push_item,
    format_compact_summary_line,
    is_pull_updated,
    is_push_updated,
)


class TestDiffstatParsing(unittest.TestCase):
    """Test git diffstat extraction into files_changed, insertions, deletions."""

    def test_parse_diffstat_standard_summary(self) -> None:
        text = (
            "Updating 1234567..abcdef0\n"
            "Fast-forward\n"
            " src/git_sync/cli.py | 15 ++++++++++-----\n"
            " tests/test_cli.py   |  8 ++++++++\n"
            " 2 files changed, 16 insertions(+), 7 deletions(-)\n"
        )
        res = parse_diffstat(text)
        self.assertEqual(res["files_changed"], 2)
        self.assertEqual(res["insertions"], 16)
        self.assertEqual(res["deletions"], 7)

    def test_parse_diffstat_insertions_only(self) -> None:
        text = " 1 file changed, 12 insertions(+)\n"
        res = parse_diffstat(text)
        self.assertEqual(res["files_changed"], 1)
        self.assertEqual(res["insertions"], 12)
        self.assertEqual(res["deletions"], 0)

    def test_parse_diffstat_deletions_only(self) -> None:
        text = " 3 files changed, 45 deletions(-)\n"
        res = parse_diffstat(text)
        self.assertEqual(res["files_changed"], 3)
        self.assertEqual(res["insertions"], 0)
        self.assertEqual(res["deletions"], 45)

    def test_parse_diffstat_multiple_summaries(self) -> None:
        text = (
            "Updating 1111111..2222222\n"
            " 1 file changed, 5 insertions(+)\n"
            "Updating 3333333..4444444\n"
            " 2 files changed, 3 insertions(+), 8 deletions(-)\n"
        )
        res = parse_diffstat(text)
        self.assertEqual(res["files_changed"], 3)
        self.assertEqual(res["insertions"], 8)
        self.assertEqual(res["deletions"], 8)

    def test_parse_diffstat_bar_lines_fallback(self) -> None:
        text = (
            " file_a.py | 3 +++\n"
            " file_b.py | 4 ++--\n"
        )
        res = parse_diffstat(text)
        self.assertEqual(res["files_changed"], 2)
        self.assertEqual(res["insertions"], 5)
        self.assertEqual(res["deletions"], 2)

    def test_parse_diffstat_already_up_to_date_and_empty(self) -> None:
        for t in ["Already up to date.\n", "Already up-to-date.\n", "", "   \n"]:
            res = parse_diffstat(t)
            self.assertEqual(res["files_changed"], 0)
            self.assertEqual(res["insertions"], 0)
            self.assertEqual(res["deletions"], 0)


class TestCompactItemTransformation(unittest.TestCase):
    """Test compact item pruning and strict token budget bounds."""

    def test_compact_pull_item_prunes_empty_and_parses_diffstat(self) -> None:
        raw_res: PullResult = {
            "repo": "/home/user/github/my-repo",
            "repo_rel": "my-repo",
            "branch": "main",
            "remote": "origin",
            "success": True,
            "stdout": (
                "Updating 1234567..abcdef0\n"
                "Fast-forward\n"
                + " large_file.py | 100 " + "+" * 50 + "\n"
                + " 1 file changed, 100 insertions(+)\n"
                + "commit 1234567890abcdef" * 50  # Bloat
            ),
            "stderr": "",
            "aux_branches": {},
            "worktree_isolated": {},
            "stale_refs": [],
            "pruned_refs": [],
            "would_prune_refs": [],
            "submodules": {"has_submodules": False, "submodules": []},
            "autostash": {
                "stashed": False,
                "restored": False,
                "conflict": False,
                "conflict_files": [],
                "stash_ref": None,
                "error": None,
            },
            "ahead": 0,
            "behind": 1,
            "dry_run": False,
        }

        compact = compact_pull_item(raw_res)

        # 1. Raw bloated stdout is omitted
        self.assertNotIn("stdout", compact)

        # 2. Diffstat metrics are parsed
        self.assertEqual(compact["files_changed"], 1)
        self.assertEqual(compact["insertions"], 100)
        self.assertEqual(compact["deletions"], 0)

        # 3. Empty fields are pruned
        self.assertNotIn("stderr", compact)
        self.assertNotIn("aux_branches", compact)
        self.assertNotIn("worktree_isolated", compact)
        self.assertNotIn("stale_refs", compact)
        self.assertNotIn("pruned_refs", compact)
        self.assertNotIn("would_prune_refs", compact)
        self.assertNotIn("submodules", compact)
        self.assertNotIn("autostash", compact)

        # 4. Strict token budget: <150 tokens (~600 chars)
        serialized = json.dumps(compact, indent=2)
        token_estimate = len(serialized) / DEFAULT_CHARS_PER_TOKEN
        self.assertLess(token_estimate, 150, f"Compact entry exceeded 150 tokens: {token_estimate}")

    def test_compact_pull_item_preserves_non_empty_collections(self) -> None:
        raw_res: PullResult = {
            "repo": "/home/user/github/repo-with-aux",
            "repo_rel": "repo-with-aux",
            "branch": "main",
            "remote": "origin",
            "success": False,
            "stdout": "",
            "stderr": "fatal: Authentication failed",
            "aux_branches": {"feat": True},
            "worktree_isolated": {"topic": "/path/to/wt"},
            "stale_refs": ["refs/remotes/origin/dead"],
            "pruned_refs": ["refs/remotes/origin/dead"],
            "would_prune_refs": [],
            "submodules": {
                "has_submodules": True,
                "success": True,
                "submodules": [{"path": "vendor/sub", "status": "UP_TO_DATE"}],
            },
            "autostash": {
                "stashed": True,
                "restored": True,
                "conflict": False,
                "dirty_files": ["file1.txt", "file2.txt"],
            },
            "diagnostic": {"category": "AUTH_FAILED", "suggestion": "Check credentials"},
        }

        compact = compact_pull_item(raw_res)

        self.assertEqual(compact["stderr"], "fatal: Authentication failed")
        self.assertEqual(compact["aux_branches"], {"feat": True})
        self.assertEqual(compact["worktree_isolated"], {"topic": "/path/to/wt"})
        self.assertEqual(compact["stale_refs"], ["refs/remotes/origin/dead"])
        self.assertEqual(compact["pruned_refs"], ["refs/remotes/origin/dead"])
        self.assertNotIn("would_prune_refs", compact)  # Empty was pruned

        # Submodules summarized to count
        self.assertTrue(compact["submodules"]["has_submodules"])
        self.assertEqual(compact["submodules"]["count"], 1)
        self.assertNotIn("submodules", compact["submodules"])

        # Autostash dirty_files summarized to count
        self.assertTrue(compact["autostash"]["stashed"])
        self.assertTrue(compact["autostash"]["restored"])
        self.assertEqual(compact["autostash"]["dirty_files_count"], 2)
        self.assertNotIn("dirty_files", compact["autostash"])

        # Diagnostic kept
        self.assertEqual(compact["diagnostic"]["category"], "AUTH_FAILED")

        # Strict token budget: <150 tokens
        serialized = json.dumps(compact)
        token_estimate = len(serialized) / DEFAULT_CHARS_PER_TOKEN
        self.assertLess(token_estimate, 150, f"Compact entry exceeded 150 tokens: {token_estimate}")

    def test_compact_push_item_token_budget_and_pruning(self) -> None:
        raw_res: PushResult = {
            "repo": "/home/user/github/push-repo",
            "repo_rel": "push-repo",
            "branch": "main",
            "remote": "origin",
            "success": True,
            "stdout": "To github.com:user/push-repo.git\n   a1b2c3d..e4f5g6h  main -> main\n",
            "stderr": "",
            "ahead": 2,
            "dry_run": False,
            "aux_branches": {},
            "aux_diagnostics": {},
        }

        compact = compact_push_item(raw_res)

        self.assertNotIn("stdout", compact)
        self.assertNotIn("stderr", compact)
        self.assertNotIn("aux_branches", compact)
        self.assertNotIn("aux_diagnostics", compact)
        self.assertEqual(compact["ahead"], 2)
        self.assertEqual(compact["files_changed"], 0)

        serialized = json.dumps(compact, indent=2)
        token_estimate = len(serialized) / DEFAULT_CHARS_PER_TOKEN
        self.assertLess(token_estimate, 150, f"Compact entry exceeded 150 tokens: {token_estimate}")


class TestCompactHumanSummaryLine(unittest.TestCase):
    """Test single-line concise summary helper and update detection."""

    def test_format_compact_summary_line(self) -> None:
        line = format_compact_summary_line(total=10, updated=2, uptodate=7, errors=1)
        self.assertEqual(line, "Total: 10 repos | Updated: 2 | Up-to-date: 7 | Errors: 1")

    def test_is_pull_updated_detection(self) -> None:
        up_to_date_res: PullResult = {
            "repo": "r",
            "success": True,
            "stdout": "Already up to date.\n",
        }
        self.assertFalse(is_pull_updated(up_to_date_res))

        updated_res: PullResult = {
            "repo": "r",
            "success": True,
            "stdout": " 1 file changed, 2 insertions(+)\n",
        }
        self.assertTrue(is_pull_updated(updated_res))

        failed_res: PullResult = {
            "repo": "r",
            "success": False,
            "stdout": "",
            "stderr": "fatal: error",
        }
        self.assertFalse(is_pull_updated(failed_res))

    def test_is_push_updated_detection(self) -> None:
        up_to_date_push: PushResult = {
            "repo": "r",
            "success": True,
            "stdout": "Everything up-to-date\n",
            "ahead": 0,
        }
        self.assertFalse(is_push_updated(up_to_date_push))

        active_push: PushResult = {
            "repo": "r",
            "success": True,
            "stdout": "   a1b2c3d..e4f5g6h  main -> main",
            "ahead": 2,
        }
        self.assertTrue(is_push_updated(active_push))


class TestCliCompactVsFullIntegration(unittest.TestCase):
    """End-to-end integration tests for CLI --compact vs --full JSON and --human --compact."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="git_sync_test_compact_")
        self.root = Path(self.tmp_dir)

        # Create upstream bare repo
        self.upstream = self.root / "upstream.git"
        run_git(str(self.root), ["init", "--bare", str(self.upstream)])

        # Create local clone
        self.local = self.root / "local_repo"
        run_git(str(self.root), ["clone", str(self.upstream), str(self.local)])
        (self.local / "init.txt").write_text("initial\n")
        run_git(str(self.local), ["add", "init.txt"])
        run_git(str(self.local), ["commit", "-m", "init"])
        run_git(str(self.local), ["push", "-u", "origin", "HEAD"])

        # Second clone to push an upstream update
        self.other = self.root / "other_clone"
        run_git(str(self.root), ["clone", str(self.upstream), str(self.other)])
        (self.other / "file.txt").write_text("updated content\n")
        run_git(str(self.other), ["add", "file.txt"])
        run_git(str(self.other), ["commit", "-m", "new commit from other"])
        run_git(str(self.other), ["push", "origin", "HEAD"])

        # Fetch on local to make pending pull visible
        run_git(str(self.local), ["fetch", "origin"])

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_pull_compact_json_default_omits_raw_stdout_and_adds_diffstat(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["pull", "local_repo", "--root", str(self.root), "--json"])
        self.assertEqual(code, 0)

        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload), 1)
        entry = payload[0]

        # Compact requirements
        self.assertTrue(entry["success"])
        self.assertNotIn("stdout", entry)
        self.assertNotIn("stderr", entry)  # Empty stderr pruned
        self.assertEqual(entry["files_changed"], 1)
        self.assertGreaterEqual(entry["insertions"], 1)
        self.assertEqual(entry["deletions"], 0)

        # Token budget
        serialized = json.dumps(entry, indent=2)
        self.assertLess(len(serialized) / DEFAULT_CHARS_PER_TOKEN, 150)

    def test_pull_full_json_preserves_raw_stdout_and_keys(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["pull", "local_repo", "--root", str(self.root), "--full", "--json"])
        self.assertEqual(code, 0)

        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload), 1)
        entry = payload[0]

        # Full mode preserves raw stdout and keys
        self.assertIn("stdout", entry)
        self.assertIn("stderr", entry)
        self.assertEqual(entry["stderr"], "")
        self.assertIn("stale_refs", entry)
        self.assertIn("pruned_refs", entry)

    def test_pull_human_compact_renders_single_line_summary(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["pull", "local_repo", "--root", str(self.root), "--human", "--compact"])
        self.assertEqual(code, 0)

        out_lines = [l.strip() for l in buf.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(out_lines), 1)
        self.assertEqual(out_lines[0], "Total: 1 repos | Updated: 1 | Up-to-date: 0 | Errors: 0")

    def test_pull_human_compact_already_up_to_date(self) -> None:
        # First pull to bring up to date
        with redirect_stdout(io.StringIO()):
            main(["pull", "local_repo", "--root", str(self.root), "--json"])

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["pull", "--all", "--root", str(self.root), "--human", "--compact"])
        self.assertEqual(code, 0)

        out_lines = [l.strip() for l in buf.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(out_lines), 1)
        self.assertEqual(out_lines[0], "Total: 2 repos | Updated: 0 | Up-to-date: 2 | Errors: 0")

    def test_push_human_compact_already_up_to_date(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["push", "--all", "--root", str(self.root), "--human", "--compact"])
        self.assertEqual(code, 0)

        out_lines = [l.strip() for l in buf.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(out_lines), 1)
        self.assertEqual(out_lines[0], "Total: 2 repos | Updated: 0 | Up-to-date: 2 | Errors: 0")


if __name__ == "__main__":
    unittest.main()
