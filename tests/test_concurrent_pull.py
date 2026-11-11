"""CLI tests for concurrent multi-repository `pull` execution."""

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.config import GitSyncConfig

NAMES = ("repo-a", "repo-b", "repo-c", "repo-d")


class TestConcurrentPull(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.repos = [os.path.join(self.root, "org", n) for n in NAMES]
        self.stack.enter_context(
            patch("git_sync.cli.load_config", return_value=GitSyncConfig(root=self.root))
        )
        self.stack.enter_context(patch("git_sync.cli.discover_repos", return_value=list(self.repos)))
        self.threads: set[int] = set()
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.main_thread = threading.get_ident()
        self.stack.enter_context(patch("git_sync.cli.pull_repo", side_effect=self._pull))

    def _pull(self, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        name = os.path.basename(path)
        with self.lock:
            self.threads.add(threading.get_ident())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        # Earlier repos finish last so completion order is reversed.
        time.sleep(0.05 * (len(NAMES) - NAMES.index(name)))
        with self.lock:
            self.active -= 1
        if name == "repo-c":
            raise RuntimeError("boom")
        return {"repo": path, "success": True, "stdout": "", "stderr": "", "behind": 1}

    def invoke(self, arguments: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(arguments)
        return code, output.getvalue()

    def test_runs_concurrently_in_worker_threads(self) -> None:
        code, _ = self.invoke(["pull", "--workers", "4", "repo-a", "repo-b", "repo-d"])
        self.assertEqual(code, 0)
        self.assertGreater(self.max_active, 1)
        self.assertNotIn(self.main_thread, self.threads)

    def test_results_sorted_by_repo_rel(self) -> None:
        _, out = self.invoke(["pull", "--workers", "4", "--full", "repo-a", "repo-b", "repo-c", "repo-d"])
        rels = [r["repo_rel"] for r in json.loads(out)]
        self.assertEqual(rels, sorted(rels))
        self.assertEqual(len(rels), 4)

    def test_worker_exception_isolated(self) -> None:
        code, out = self.invoke(["pull", "--workers", "4", "--full", "repo-a", "repo-b", "repo-c", "repo-d"])
        self.assertEqual(code, 1)
        data = {os.path.basename(r["repo"]): r for r in json.loads(out)}
        self.assertEqual(len(data), 4)
        self.assertFalse(data["repo-c"]["success"])
        self.assertEqual(data["repo-c"]["diagnostic"]["category"], "PULL_EXCEPTION")
        self.assertEqual(data["repo-c"]["stderr"], "boom")
        for ok in ("repo-a", "repo-b", "repo-d"):
            self.assertTrue(data[ok]["success"])

    def test_interactive_tty_stays_sequential(self) -> None:
        with patch("git_sync.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            code, _ = self.invoke(["pull", "--human", "-i", "--workers", "4", "repo-a", "repo-b", "repo-d"])
        self.assertEqual(code, 0)
        self.assertEqual(self.max_active, 1)
        self.assertEqual(self.threads, {self.main_thread})


if __name__ == "__main__":
    unittest.main()
