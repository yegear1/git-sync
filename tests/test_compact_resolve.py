"""
Unit tests for token-budget compact reporting in git-sync resolve.

# INVARIANT: Strict token budget: each compact repository entry must consume <150 tokens (~600 chars).
# INVARIANT: Zero external dependencies; standard library only.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.engine import DEFAULT_CHARS_PER_TOKEN, run_git
from git_sync.ui.formatters import compact_resolve_item

MAX_ENTRY_CHARS = 150 * DEFAULT_CHARS_PER_TOKEN


def _verbose_result(**overrides: Any) -> dict[str, Any]:
    res: dict[str, Any] = {
        "repo": "/work/demo",
        "repo_rel": "demo",
        "branch": "main",
        "strategy": "rebase",
        "success": True,
        "stdout": "Successfully rebased and updated refs/heads/main.\n" * 20,
        "stderr": "warning: noise\n" * 20,
        "dry_run": False,
        "details": {
            "branch": "main",
            "upstream": "origin/main",
            "ahead": 3,
            "behind": 2,
            "is_diverged": True,
            "is_dirty": True,
            "dirty_files": [" M a.py", "?? b.py"],
            "local_commits": [f"abc{i} local commit {i}" for i in range(30)],
            "remote_commits": [f"def{i} remote commit {i}" for i in range(30)],
        },
    }
    res.update(overrides)
    return res


class TestCompactResolveItem(unittest.TestCase):
    def test_schema_and_omissions(self):
        compact = compact_resolve_item(_verbose_result())
        self.assertEqual(compact["strategy"], "rebase")
        self.assertTrue(compact["success"])
        self.assertEqual(compact["branch"], "main")
        self.assertEqual(compact["upstream"], "origin/main")
        self.assertEqual(compact["ahead"], 3)
        self.assertEqual(compact["behind"], 2)
        self.assertTrue(compact["is_dirty"])
        self.assertEqual(compact["dirty_files_count"], 2)
        for omitted in ("stdout", "stderr", "details", "local_commits", "remote_commits", "dirty_files"):
            self.assertNotIn(omitted, compact)

    def test_token_budget(self):
        compact = compact_resolve_item(_verbose_result())
        self.assertLess(len(json.dumps(compact)), MAX_ENTRY_CHARS)

    def test_failure_keeps_diagnostic_and_truncated_message(self):
        diag = {"category": "CONFLICT", "action_required": "manual_merge"}
        compact = compact_resolve_item(
            _verbose_result(success=False, diagnostic=diag, message="x" * 1000)
        )
        self.assertFalse(compact["success"])
        self.assertEqual(compact["diagnostic"], diag)
        self.assertLessEqual(len(compact["message"]), 160)
        self.assertLess(len(json.dumps(compact)), MAX_ENTRY_CHARS)

    def test_missing_details(self):
        compact = compact_resolve_item({"repo": "/r", "success": True})
        self.assertEqual(compact["strategy"], "none")
        self.assertNotIn("ahead", compact)


class TestCliResolveCompact(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _git(self, path: Path, *args: str) -> None:
        run_git(str(path), list(args))

    def _create_diverged_repo(self, name: str) -> Path:
        remote = self.root / f"{name}-remote.git"
        remote.mkdir()
        self._git(remote, "init", "--bare", "-b", "main")
        setup = self.root / f"{name}-setup"
        setup.mkdir()
        for cmd in (
            ("init", "-b", "main"),
            ("config", "user.email", "t@example.com"),
            ("config", "user.name", "T"),
            ("remote", "add", "origin", str(remote)),
        ):
            self._git(setup, *cmd)
        (setup / "base.txt").write_text("base\n", encoding="utf-8")
        for cmd in (("add", "."), ("commit", "-m", "init"), ("push", "-u", "origin", "main")):
            self._git(setup, *cmd)
        local = self.root / name
        run_git(str(self.root), ["clone", str(remote), str(local)])
        self._git(local, "config", "user.email", "t@example.com")
        self._git(local, "config", "user.name", "T")
        (setup / "remote.txt").write_text("remote\n", encoding="utf-8")
        for cmd in (("add", "."), ("commit", "-m", "remote"), ("push", "origin", "main")):
            self._git(setup, *cmd)
        (local / "local.txt").write_text("local\n", encoding="utf-8")
        for cmd in (("add", "."), ("commit", "-m", "local"), ("fetch", "origin")):
            self._git(local, *cmd)
        return local

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(argv)
        return code, buf.getvalue()

    def test_default_json_is_compact(self):
        self._create_diverged_repo("rc-default")
        code, out = self._run(["--root", str(self.root), "--json", "resolve", "rc-default", "--strategy", "abort", "--no-fetch"])
        self.assertEqual(code, 0)
        item = json.loads(out)[0]
        self.assertNotIn("details", item)
        self.assertNotIn("stdout", item)
        self.assertEqual(item["ahead"], 1)
        self.assertEqual(item["behind"], 1)
        self.assertIn("is_dirty", item)
        self.assertIn("dirty_files_count", item)
        self.assertLess(len(json.dumps(item)), MAX_ENTRY_CHARS)

    def test_full_preserves_raw_payload(self):
        self._create_diverged_repo("rc-full")
        code, out = self._run(["--root", str(self.root), "--json", "--full", "resolve", "rc-full", "--strategy", "abort", "--no-fetch"])
        self.assertEqual(code, 0)
        item = json.loads(out)[0]
        self.assertIn("details", item)
        self.assertTrue(item["details"]["is_diverged"])
        self.assertIn("stdout", item)

    def test_human_compact_single_line_summary(self):
        self._create_diverged_repo("rc-human")
        code, out = self._run(["--root", str(self.root), "--human", "--compact", "resolve", "rc-human", "--strategy", "rebase", "--no-fetch"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "Total: 1 repos | Updated: 1 | Up-to-date: 0 | Errors: 0")

    def test_human_full_prints_per_repo_lines(self):
        self._create_diverged_repo("rc-human-full")
        code, out = self._run(["--root", str(self.root), "--human", "resolve", "rc-human-full", "--strategy", "abort", "--no-fetch"])
        self.assertEqual(code, 0)
        self.assertIn("[rc-human-full]", out)
        self.assertNotIn("Total:", out)

    def test_missing_repo_counts_as_error(self):
        self._create_diverged_repo("rc-missing")
        code, out = self._run(["--root", str(self.root), "--human", "--compact", "resolve", "nonexistent", "--strategy", "abort", "--no-fetch"])
        self.assertEqual(code, 1)
        self.assertIn("Errors: 1", out)


if __name__ == "__main__":
    unittest.main()
