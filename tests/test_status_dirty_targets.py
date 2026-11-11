"""CLI tests for positional repository target filtering in `status` and `dirty`."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.config import GitSyncConfig


class TestStatusDirtyTargets(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.repos = {
            name: os.path.join(self.root, "org", name) for name in ("repo-a", "repo-b", "repo-c")
        }
        self.stack.enter_context(
            patch("git_sync.cli.load_config", return_value=GitSyncConfig(root=self.root))
        )
        self.stack.enter_context(
            patch("git_sync.cli.discover_repos", return_value=list(self.repos.values()))
        )
        self.inspected: list[str] = []
        self.stack.enter_context(patch("git_sync.cli.inspect_repo", side_effect=self._inspect))

    def _inspect(self, path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.inspected.append(os.path.basename(path))
        name = os.path.basename(path)
        dirty = name in ("repo-a", "repo-b")
        return {
            "path": path,
            "rel_path": f"org/{name}",
            "status": "PUSH_PENDING" if name == "repo-c" else "UP_TO_DATE",
            "is_dirty": dirty,
            "dirty_files": [f"{name}.txt"] if dirty else [],
        }

    def invoke(self, arguments: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(arguments)
        return code, output.getvalue()

    @staticmethod
    def error(query: str) -> dict[str, Any]:
        return {
            "repo": query,
            "repo_rel": query,
            "success": False,
            "error": "REPO_NOT_FOUND",
            "diagnostic": {
                "category": "REPO_NOT_FOUND",
                "action_required": "verify_repo_name",
                "message": f"No repository found matching: '{query}'",
                "suggestion": "git-sync status",
            },
        }

    # --- status ---

    def test_status_without_targets_inspects_all(self) -> None:
        code, output = self.invoke(["status", "--no-fetch"])
        self.assertEqual(code, 0)
        data = json.loads(output)
        self.assertEqual(data["total"], 3)
        self.assertNotIn("not_found", data)
        self.assertEqual(sorted(self.inspected), ["repo-a", "repo-b", "repo-c"])

    def test_status_filters_to_requested_targets(self) -> None:
        code, output = self.invoke(["status", "--no-fetch", "repo-a", "repo-c"])
        self.assertEqual(code, 0)
        data = json.loads(output)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["dirty"], ["org/repo-a"])
        self.assertEqual(data["push_pending"], ["org/repo-c"])
        self.assertEqual(sorted(self.inspected), ["repo-a", "repo-c"])

    def test_status_duplicate_targets_inspected_once(self) -> None:
        code, output = self.invoke(["status", "--no-fetch", "repo-a", "org/repo-a"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["total"], 1)
        self.assertEqual(self.inspected, ["repo-a"])

    def test_status_mixed_valid_and_invalid_targets(self) -> None:
        code, output = self.invoke(["status", "--no-fetch", "repo-b", "nope"])
        self.assertEqual(code, 1)
        data = json.loads(output)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["dirty"], ["org/repo-b"])
        self.assertEqual(data["not_found"], [self.error("nope")])

    def test_status_only_invalid_targets(self) -> None:
        for flags in ([], ["--full"]):
            with self.subTest(flags=flags):
                self.inspected.clear()
                code, output = self.invoke(["status", "--no-fetch", "nope", *flags])
                self.assertEqual(code, 1)
                data = json.loads(output)
                self.assertEqual(data["total"], 0)
                self.assertEqual(data["not_found"], [self.error("nope")])
                self.assertEqual(self.inspected, [])

    def test_status_human_mode_reports_missing_and_fails(self) -> None:
        code, output = self.invoke(["status", "--no-fetch", "--human", "--compact", "repo-a", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("❌ No repository found matching: 'nope'", output)
        self.assertIn("Total: 1 repositories", output)

    def test_status_missing_target_with_empty_workspace(self) -> None:
        with patch("git_sync.cli.discover_repos", return_value=[]):
            code, output = self.invoke(["status", "--no-fetch", "nope"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["not_found"], [self.error("nope")])

    # --- dirty ---

    def test_dirty_without_targets_reports_all(self) -> None:
        code, output = self.invoke(["dirty"])
        self.assertEqual(code, 0)
        self.assertEqual(
            [r["repo"] for r in json.loads(output)], ["org/repo-a", "org/repo-b"]
        )

    def test_dirty_filters_to_requested_targets(self) -> None:
        code, output = self.invoke(["dirty", "repo-b", "repo-c"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), [{"repo": "org/repo-b", "files": ["repo-b.txt"]}])
        self.assertEqual(sorted(self.inspected), ["repo-b", "repo-c"])

    def test_dirty_mixed_valid_and_invalid_targets(self) -> None:
        code, output = self.invoke(["dirty", "repo-a", "nope"])
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output),
            [{"repo": "org/repo-a", "files": ["repo-a.txt"]}, self.error("nope")],
        )

    def test_dirty_only_invalid_targets(self) -> None:
        code, output = self.invoke(["dirty", "nope"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), [self.error("nope")])
        self.assertEqual(self.inspected, [])

    def test_dirty_human_mode_reports_missing_and_fails(self) -> None:
        code, output = self.invoke(["dirty", "--human", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("❌ No repository found matching: 'nope'", output)


if __name__ == "__main__":
    unittest.main()
