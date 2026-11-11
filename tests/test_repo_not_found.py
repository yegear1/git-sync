"""CLI regression tests for explicit repository selection failures."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.config import DEFAULT_TIMEOUT, GitSyncConfig


class TestRepoNotFound(unittest.TestCase):
    commands = (
        (["pull"], None),
        (["push"], None),
        (["resolve"], None),
        (["unlock"], "unlocked"),
        (["worktree", "list"], "repositories"),
        (["worktree", "prune"], "pruned_repos"),
    )

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.repo = os.path.join(self.root, "org", "existing")
        self.stack.enter_context(
            patch(
                "git_sync.cli.load_config",
                return_value=GitSyncConfig(root=self.root),
            )
        )
        self.stack.enter_context(
            patch(
                "git_sync.cli.discover_repos",
                return_value=[self.repo],
            )
        )
        self.operations: dict[str, MagicMock] = {}
        for operation, result in (
            ("pull_repo", {"repo": self.repo, "success": True}),
            ("push_repo", {"repo": self.repo, "success": True}),
            ("resolve_divergence", {"repo": self.repo, "success": True}),
            ("get_divergence_details", {"branch": "main", "ahead": 1}),
            ("inspect_repo", {"branch": "main", "ahead": 1}),
            ("find_locked_repos", []),
            (
                "prune_repo_worktrees",
                {
                    "repo": self.repo,
                    "rel_path": "org/existing",
                    "pruned": [],
                    "dirty_worktrees": [],
                    "warnings": [],
                },
            ),
            (
                "BulkCache.get_worktree_info",
                {
                    "worktrees": [{"path": self.repo, "is_main": True}],
                },
            ),
        ):
            self.operations[operation] = self.stack.enter_context(
                patch(
                    f"git_sync.cli.{operation}",
                    return_value=result,
                )
            )

    def invoke(self, arguments: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(arguments)
        return code, output.getvalue()

    def records(self, output: str, key: str | None) -> list[dict[str, object]]:
        payload = json.loads(output)
        return cast(list[dict[str, object]], payload[key] if key else payload)

    def expected_error(self, query: str) -> dict[str, object]:
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

    def test_missing_names_emit_complete_errors_in_all_json_modes(self) -> None:
        for command, key in self.commands:
            for flags in ([], ["--json"], ["--compact"], ["--full"]):
                with self.subTest(command=command, flags=flags):
                    code, output = self.invoke(
                        [*command, "unknown", "org/typo", *flags]
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(
                        self.records(output, key),
                        [
                            self.expected_error("unknown"),
                            self.expected_error("org/typo"),
                        ],
                    )
        for operation in (
            "pull_repo",
            "push_repo",
            "resolve_divergence",
            "prune_repo_worktrees",
        ):
            self.operations[operation].assert_not_called()

    def test_human_mode_reports_missing_names_and_fails(self) -> None:
        for command, _ in self.commands:
            for flags in (["--human"], ["--human", "--full"]):
                with self.subTest(command=command, flags=flags):
                    code, output = self.invoke([*command, "unknown", *flags])
                    self.assertEqual(code, 1)
                    self.assertIn("❌ No repository found matching: 'unknown'", output)

    def test_valid_targets_still_run_but_missing_target_fails_batch(self) -> None:
        for command, key in self.commands:
            with self.subTest(command=command):
                code, output = self.invoke(
                    [
                        *command,
                        "unknown",
                        "org/existing",
                        "--full",
                        "--dry-run",
                    ]
                )
                self.assertEqual(code, 1)
                records = self.records(output, key)
                self.assertEqual(records[-1], self.expected_error("unknown"))
                if command != ["unlock"]:
                    self.assertEqual(records[0]["repo"], self.repo)
        self.operations["pull_repo"].assert_called_once()
        self.operations["push_repo"].assert_called_once()
        self.operations["get_divergence_details"].assert_called_once_with(
            self.repo, timeout=DEFAULT_TIMEOUT
        )
        self.assertEqual(
            self.operations["find_locked_repos"].call_args.kwargs["repos"], [self.repo]
        )
        self.operations["prune_repo_worktrees"].assert_called_once()

    def test_valid_only_targets_keep_success_exit_code(self) -> None:
        for command, _ in self.commands:
            with self.subTest(command=command):
                code, _ = self.invoke([*command, "org/existing", "--dry-run"])
                self.assertEqual(code, 0)

    def test_preview_does_not_hide_missing_targets(self) -> None:
        for command in (["pull"], ["push"], ["resolve"], ["worktree", "prune"]):
            with self.subTest(command=command):
                flags = ["--strategy", "rebase"] if command == ["resolve"] else []
                code, output = self.invoke(
                    [
                        *command,
                        "existing",
                        "unknown",
                        "--preview",
                        *flags,
                    ]
                )
                self.assertEqual(code, 1)
                self.assertEqual(
                    self.records(output, "items")[-1], self.expected_error("unknown")
                )
        for operation in ("pull_repo", "push_repo", "resolve_divergence"):
            self.operations[operation].assert_not_called()
        self.assertTrue(
            self.operations["prune_repo_worktrees"].call_args.kwargs["dry_run"]
        )

    def _bulk_commands(self) -> list[tuple[list[str], str]]:
        return [
            (["pull"], "pull"),
            (["push"], "push"),
            (["resolve", "--strategy", "rebase"], "resolve"),
            (["worktree", "prune"], "worktree_prune"),
        ]

    def assert_not_executed(self) -> None:
        for operation in ("pull_repo", "push_repo", "resolve_divergence"):
            self.operations[operation].assert_not_called()
        self.operations["prune_repo_worktrees"].assert_not_called()

    def test_preview_all_unmatched_emits_envelope(self) -> None:
        for command, action in self._bulk_commands():
            with self.subTest(command=command):
                code, output = self.invoke([*command, "unknown", "org/typo", "--preview", "--json"])
                self.assertEqual(code, 1)
                payload = json.loads(output)
                self.assertIs(payload["preview"], True)
                self.assertEqual(payload["action"], action)
                self.assertEqual(payload["total"], 0)
                self.assertEqual(
                    payload["items"],
                    [self.expected_error("unknown"), self.expected_error("org/typo")],
                )
        self.assert_not_executed()

    def test_confirm_all_unmatched_emits_error_envelope(self) -> None:
        for command, _ in self._bulk_commands():
            with self.subTest(command=command):
                code, output = self.invoke([*command, "unknown", "--confirm", "--json"])
                self.assertEqual(code, 1)
                payload = json.loads(output)
                self.assertIn("error", payload)
                self.assertEqual(payload["preview"], [self.expected_error("unknown")])
        self.assert_not_executed()

    def test_confirm_all_unmatched_human_mode_does_not_prompt(self) -> None:
        with patch("git_sync.cli.prompt_bulk_confirmation") as prompt:
            for command, _ in self._bulk_commands():
                with self.subTest(command=command):
                    code, _ = self.invoke([*command, "unknown", "--confirm", "--human"])
                    self.assertEqual(code, 1)
            prompt.assert_not_called()
        self.assert_not_executed()

    def test_benchmark_envelope_preserves_missing_errors(self) -> None:
        for command in ("pull", "push"):
            with self.subTest(command=command):
                code, output = self.invoke([command, "unknown", "--benchmark"])
                self.assertEqual(code, 1)
                self.assertEqual(
                    self.records(output, "results"), [self.expected_error("unknown")]
                )
                self.assertIn("telemetry", json.loads(output))

    def test_group_filter_reports_excluded_target_as_missing(self) -> None:
        for command, key in self.commands:
            with self.subTest(command=command):
                code, output = self.invoke([*command, "existing", "--group", "other"])
                self.assertEqual(code, 1)
                self.assertEqual(
                    self.records(output, key), [self.expected_error("existing")]
                )

    def test_mixed_compact_pull_push_keeps_successes_and_errors(self) -> None:
        for command in ("pull", "push"):
            with self.subTest(command=command):
                code, output = self.invoke([command, "existing", "unknown"])
                records = self.records(output, None)
                self.assertEqual(code, 1)
                self.assertEqual(records[0]["repo"], self.repo)
                self.assertTrue(records[0]["success"])
                self.assertEqual(records[1], self.expected_error("unknown"))

    def test_unlock_keeps_lock_results_alongside_missing_errors(self) -> None:
        lock = Path(self.root) / "index.lock"
        lock.touch()
        self.operations["find_locked_repos"].return_value = [
            {
                "repo": self.repo,
                "rel_path": "org/existing",
                "lock_file": str(lock),
                "lock_name": "index.lock",
                "age_seconds": 500,
                "stale": True,
            }
        ]
        code, output = self.invoke(["unlock", "existing", "unknown"])
        records = self.records(output, "unlocked")
        self.assertEqual(code, 1)
        self.assertEqual(records[0]["status"], "UNLOCKED")
        self.assertEqual(records[1], self.expected_error("unknown"))
        self.assertFalse(lock.exists())
