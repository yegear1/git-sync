"""Regression coverage for compact doctor and worktree inventory JSON."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.config import GitSyncConfig
from git_sync.engine import DoctorSanityReport
from git_sync.ui.formatters import compact_doctor_report


class TestCompactDoctorWorktree(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        config_patch = patch(
            "git_sync.cli.load_config", return_value=GitSyncConfig(root=self.root)
        )
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def invoke(self, arguments: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(arguments)
        return code, cast(dict[str, object], json.loads(output.getvalue()))

    def doctor_report(self, ok: bool = False) -> DoctorSanityReport:
        return {
            "ok": ok,
            "multi_remote_matrix": {
                "total_repos_audited": 3,
                "healthy_endpoints": 2,
                "unreachable_endpoints": 1,
                "grade_distribution": {"A": 1, "B": 1, "C": 0, "F": 1},
                "matrix": [
                    {
                        "repo": "fast",
                        "has_unreachable": False,
                        "remotes": {"origin": {"grade": "A", "ok": True}},
                    },
                    {
                        "repo": "normal",
                        "has_unreachable": False,
                        "remotes": {"origin": {"grade": "B", "ok": True}},
                    },
                    {
                        "repo": "offline",
                        "has_unreachable": True,
                        "remotes": {"origin": {"grade": "F", "ok": False}},
                    },
                ],
            },
            "worktree_health": {
                "total_repos_audited": 3,
                "total_dirty_worktrees": 1,
                "total_stale_worktrees": 1,
                "items": [
                    {"repo": "fast", "is_healthy": True},
                    {
                        "repo": "dirty",
                        "is_healthy": False,
                        "dirty_worktrees": ["/dirty"],
                    },
                    {
                        "repo": "stale",
                        "is_healthy": False,
                        "stale_worktrees": ["/missing"],
                    },
                ],
            },
            "locked_repos": [{"repo": "locked"}],
            "warnings": ["warning"],
            "errors": ["error"],
            "suggestions": ["suggestion"],
        }

    def test_doctor_compact_preserves_totals_anomalies_and_original_report(
        self,
    ) -> None:
        report = self.doctor_report()
        original = deepcopy(report)
        expected = deepcopy(report)
        expected["multi_remote_matrix"]["matrix"] = report["multi_remote_matrix"][
            "matrix"
        ][2:]
        expected["worktree_health"]["items"] = report["worktree_health"]["items"][1:]
        for flags in ([], ["--compact"], ["--summary"]):
            with (
                self.subTest(flags=flags),
                patch("git_sync.cli.check_credentials_sanity", return_value=report),
            ):
                code, payload = self.invoke(["doctor", *flags])
                self.assertEqual(code, 1)
                self.assertEqual(payload, expected)
                self.assertEqual(report, original)

    def test_doctor_full_keeps_complete_matrices(self) -> None:
        report = self.doctor_report(ok=True)
        for flag in ("--full", "--no-compact"):
            with (
                self.subTest(flag=flag),
                patch("git_sync.cli.check_credentials_sanity", return_value=report),
            ):
                code, payload = self.invoke(["doctor", flag])
                self.assertEqual(code, 0)
                self.assertEqual(payload, report)

    def test_doctor_compact_all_healthy_has_empty_arrays(self) -> None:
        report = self.doctor_report(ok=True)
        report["multi_remote_matrix"]["matrix"] = report["multi_remote_matrix"][
            "matrix"
        ][:2]
        report["worktree_health"]["items"] = report["worktree_health"]["items"][:1]
        expected = deepcopy(report)
        expected["multi_remote_matrix"]["matrix"] = []
        expected["worktree_health"]["items"] = []
        with patch("git_sync.cli.check_credentials_sanity", return_value=report):
            code, payload = self.invoke(["doctor"])
        self.assertEqual(code, 0)
        self.assertEqual(payload, expected)

    def test_doctor_compact_missing_optional_audits(self) -> None:
        self.assertEqual(compact_doctor_report({"ok": True}), {"ok": True})

    def test_worktree_compact_and_full_inventory(self) -> None:
        names = ["clean", "linked", "stale", "prunable", "locked"]
        repos = [os.path.join(self.root, name) for name in names]
        inventories: list[dict[str, object]] = []
        entries: list[dict[str, object]] = []
        for repo, name in zip(repos, names):
            worktrees: list[dict[str, object]] = [{"path": repo, "is_main": True}]
            stale = [{"path": repo + "/missing"}] if name == "stale" else []
            if name == "linked":
                worktrees.append({"path": repo + "/topic", "is_main": False})
            if name == "prunable":
                worktrees[0]["is_prunable"] = True
            if name == "locked":
                worktrees[0]["is_locked"] = True
            inventories.append({"worktrees": worktrees, "stale_worktrees": stale})
            entries.append(
                {
                    "repo": repo,
                    "rel_path": name,
                    "is_worktree": False,
                    "main_worktree": None,
                    "worktrees": worktrees,
                    "stale_worktrees": stale,
                }
            )
        for flags, expected in (
            ([], entries[1:]),
            (["--compact"], entries[1:]),
            (["--full"], entries),
            (["--no-compact"], entries),
        ):
            with (
                self.subTest(flags=flags),
                patch("git_sync.cli.discover_repos", return_value=repos),
                patch(
                    "git_sync.cli.BulkCache.get_worktree_info",
                    side_effect=deepcopy(inventories),
                ),
            ):
                code, payload = self.invoke(["worktree", "list", *flags])
                self.assertEqual(code, 0)
                self.assertEqual(
                    payload,
                    {
                        "root": self.root,
                        "total_repos_with_worktrees": len(expected),
                        "repositories": expected,
                    },
                )

    def test_worktree_empty_workspace_in_both_modes(self) -> None:
        for flags in ([], ["--full"]):
            with (
                self.subTest(flags=flags),
                patch("git_sync.cli.discover_repos", return_value=[]),
            ):
                code, payload = self.invoke(["worktree", "list", *flags])
                self.assertEqual(code, 0)
                self.assertEqual(payload["repositories"], [])
                self.assertEqual(payload["total_repos_with_worktrees"], 0)
