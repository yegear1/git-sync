"""
Unit tests for git-sync divergence detection, interactive resolution assistant,
and automated batch strategies (rebase, merge, abort, stash).
"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure src is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import _prompt_resolution_choice, main
from git_sync.core import (
    DEFAULT_COMPACT_DIFF_TOKENS,
    find_diverged_repos,
    get_divergence_details,
    get_divergence_diff,
    pull_repo,
    resolve_divergence,
    run_git,
    split_diff_files,
    truncate_diff,
)


class TestDivergenceAndResolution(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _init_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        run_git(str(path), ["init", "-b", "main"])
        run_git(str(path), ["config", "user.email", "test@example.com"])
        run_git(str(path), ["config", "user.name", "Test User"])
        return path

    def _init_bare_remote(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        run_git(str(path), ["init", "--bare", "-b", "main"])
        return path

    def _create_diverged_repo_pair(self, name: str = "diverged-repo") -> Path:
        """
        Creates a bare remote and a local clone that diverges from the remote.
        Remote will have a commit on main.
        Local will have a different commit on main.
        Returns the local repo path.
        """
        remote_path = self.root / f"{name}-remote.git"
        self._init_bare_remote(remote_path)

        # Temp clone to populate remote main
        setup_clone = self.root / f"{name}-setup"
        self._init_repo(setup_clone)
        run_git(str(setup_clone), ["remote", "add", "origin", str(remote_path)])
        (setup_clone / "base.txt").write_text("initial base content\n", encoding="utf-8")
        run_git(str(setup_clone), ["add", "base.txt"])
        run_git(str(setup_clone), ["commit", "-m", "Initial commit"])
        run_git(str(setup_clone), ["push", "-u", "origin", "main"])

        # Create active local repo
        local_repo = self.root / name
        run_git(str(self.root), ["clone", str(remote_path), str(local_repo)])
        run_git(str(local_repo), ["config", "user.email", "test@example.com"])
        run_git(str(local_repo), ["config", "user.name", "Test User"])

        # Push a new commit to remote via setup_clone
        (setup_clone / "remote_file.txt").write_text("remote commit content\n", encoding="utf-8")
        run_git(str(setup_clone), ["add", "remote_file.txt"])
        run_git(str(setup_clone), ["commit", "-m", "Remote update commit"])
        run_git(str(setup_clone), ["push", "origin", "main"])

        # Create local commit in local_repo without pulling
        (local_repo / "local_file.txt").write_text("local commit content\n", encoding="utf-8")
        run_git(str(local_repo), ["add", "local_file.txt"])
        run_git(str(local_repo), ["commit", "-m", "Local work commit"])

        # Fetch remote updates so git knows about origin/main
        run_git(str(local_repo), ["fetch", "origin"])

        return local_repo

    def test_divergence_details_on_diverged_repo(self):
        local_repo = self._create_diverged_repo_pair("inspect-diverged")
        details = get_divergence_details(str(local_repo))

        self.assertEqual(details["branch"], "main")
        self.assertEqual(details["upstream"], "origin/main")
        self.assertEqual(details["ahead"], 1)
        self.assertEqual(details["behind"], 1)
        self.assertTrue(details["is_diverged"])
        self.assertIsNotNone(details["merge_base"])
        self.assertEqual(len(details["local_commits"]), 1)
        self.assertIn("Local work commit", details["local_commits"][0]["subject"])
        self.assertEqual(len(details["remote_commits"]), 1)
        self.assertIn("Remote update commit", details["remote_commits"][0]["subject"])
        self.assertFalse(details["is_dirty"])

    def test_divergence_details_untracked_and_up_to_date(self):
        repo_path = self.root / "local-only"
        self._init_repo(repo_path)
        (repo_path / "foo.txt").write_text("foo", encoding="utf-8")
        run_git(str(repo_path), ["add", "foo.txt"])
        run_git(str(repo_path), ["commit", "-m", "feat: local only"])

        details = get_divergence_details(str(repo_path))
        self.assertFalse(details["is_diverged"])
        self.assertIsNone(details["upstream"])
        self.assertIn("No upstream", details["error"])

    def test_get_divergence_diff(self):
        local_repo = self._create_diverged_repo_pair("diff-repo")
        diff_text = get_divergence_diff(str(local_repo))
        self.assertIn("remote_file.txt", diff_text)
        self.assertIn("remote commit content", diff_text)

        stat_text = get_divergence_diff(str(local_repo), stat_only=True)
        self.assertIn("remote_file.txt", stat_text)

    def test_resolve_divergence_abort(self):
        local_repo = self._create_diverged_repo_pair("abort-repo")
        res = resolve_divergence(str(local_repo), strategy="abort")
        self.assertTrue(res["success"])
        self.assertEqual(res["strategy"], "abort")

        # Verify state is untouched
        details = get_divergence_details(str(local_repo))
        self.assertEqual(details["ahead"], 1)
        self.assertEqual(details["behind"], 1)

    def test_resolve_divergence_rebase_clean(self):
        local_repo = self._create_diverged_repo_pair("rebase-clean")
        res = resolve_divergence(str(local_repo), strategy="rebase")
        self.assertTrue(res["success"])
        self.assertEqual(res["strategy"], "rebase")

        # Verify branch is now rebased: ahead=1, behind=0
        details = get_divergence_details(str(local_repo))
        self.assertEqual(details["ahead"], 1)
        self.assertEqual(details["behind"], 0)
        self.assertFalse(details["is_diverged"])
        self.assertTrue((local_repo / "remote_file.txt").exists())
        self.assertTrue((local_repo / "local_file.txt").exists())

    def test_resolve_divergence_merge_clean(self):
        local_repo = self._create_diverged_repo_pair("merge-clean")
        res = resolve_divergence(str(local_repo), strategy="merge")
        self.assertTrue(res["success"])
        self.assertEqual(res["strategy"], "merge")

        # After merge, branch has local commit + remote commit + merge commit (ahead = 2, behind = 0)
        details = get_divergence_details(str(local_repo))
        self.assertEqual(details["behind"], 0)
        self.assertGreaterEqual(details["ahead"], 1)
        self.assertFalse(details["is_diverged"])
        self.assertTrue((local_repo / "remote_file.txt").exists())
        self.assertTrue((local_repo / "local_file.txt").exists())

    def test_resolve_divergence_rebase_conflict_auto_aborts(self):
        """When rebase encounters conflicts, it must automatically abort to preserve repository state."""
        local_repo = self._create_diverged_repo_pair("rebase-conflict")

        # Modify same file base.txt in remote
        remote_path = self.root / "rebase-conflict-remote.git"
        setup_clone = self.root / "rebase-conflict-setup"
        (setup_clone / "base.txt").write_text("remote conflicting line\n", encoding="utf-8")
        run_git(str(setup_clone), ["add", "base.txt"])
        run_git(str(setup_clone), ["commit", "-m", "Remote edit to base.txt"])
        run_git(str(setup_clone), ["push", "origin", "main"])

        # Modify base.txt in local_repo differently
        (local_repo / "base.txt").write_text("local conflicting line\n", encoding="utf-8")
        run_git(str(local_repo), ["add", "base.txt"])
        run_git(str(local_repo), ["commit", "-m", "Local edit to base.txt"])

        # Fetch
        run_git(str(local_repo), ["fetch", "origin"])

        res = resolve_divergence(str(local_repo), strategy="rebase")
        self.assertFalse(res["success"])
        self.assertIn("automatically aborted", res["message"])

        # INVARIANT: Git working copy must not be stuck in rebase-apply or rebase-merge
        status_proc = run_git(str(local_repo), ["status"])
        self.assertNotIn("rebase in progress", status_proc.stdout)

    def test_resolve_divergence_merge_conflict_auto_aborts(self):
        """When merge encounters conflicts, it must automatically abort to preserve clean state."""
        local_repo = self._create_diverged_repo_pair("merge-conflict")

        # Modify base.txt in remote
        remote_path = self.root / "merge-conflict-remote.git"
        setup_clone = self.root / "merge-conflict-setup"
        (setup_clone / "base.txt").write_text("remote edit for merge conflict\n", encoding="utf-8")
        run_git(str(setup_clone), ["add", "base.txt"])
        run_git(str(setup_clone), ["commit", "-m", "Remote conflict commit"])
        run_git(str(setup_clone), ["push", "origin", "main"])

        # Modify base.txt in local_repo
        (local_repo / "base.txt").write_text("local edit for merge conflict\n", encoding="utf-8")
        run_git(str(local_repo), ["add", "base.txt"])
        run_git(str(local_repo), ["commit", "-m", "Local conflict commit"])

        # Fetch
        run_git(str(local_repo), ["fetch", "origin"])

        res = resolve_divergence(str(local_repo), strategy="merge")
        self.assertFalse(res["success"])
        self.assertIn("automatically aborted", res["message"])

        # INVARIANT: Working tree must not have unmerged conflict markers
        status_proc = run_git(str(local_repo), ["status"])
        self.assertNotIn("Unmerged paths", status_proc.stdout)

    def test_resolve_divergence_stash(self):
        local_repo = self._create_diverged_repo_pair("stash-repo")
        # Dirty the repository
        (local_repo / "dirty_file.txt").write_text("uncommitted work\n", encoding="utf-8")
        run_git(str(local_repo), ["add", "dirty_file.txt"])

        res = resolve_divergence(str(local_repo), strategy="stash")
        self.assertTrue(res["success"])
        self.assertEqual(res["strategy"], "stash")

        # Verify stash removed uncommitted changes
        status_proc = run_git(str(local_repo), ["status", "--porcelain"])
        self.assertEqual(status_proc.stdout.strip(), "")

    def test_resolve_divergence_unknown_strategy(self):
        local_repo = self._create_diverged_repo_pair("unknown-strat")
        res = resolve_divergence(str(local_repo), strategy="invalid-strat")
        self.assertFalse(res["success"])
        self.assertIn("Unknown resolution strategy", res["stderr"])

    def test_find_diverged_repos(self):
        local_repo = self._create_diverged_repo_pair("diverged-target")
        clean_repo = self.root / "clean-repo"
        self._init_repo(clean_repo)

        diverged = find_diverged_repos([str(local_repo), str(clean_repo)], str(self.root), do_fetch=False)
        self.assertEqual(len(diverged), 1)
        self.assertEqual(diverged[0]["path"], str(local_repo))

    def test_pull_repo_with_strategy_fallback(self):
        local_repo = self._create_diverged_repo_pair("pull-strategy")
        # Standard ff-only pull fails because branches diverged
        res_ff = pull_repo(str(local_repo), ff_only=True)
        self.assertFalse(res_ff["success"])

        # Pull with strategy="rebase" resolves divergence
        res_rebase = pull_repo(str(local_repo), ff_only=True, strategy="rebase")
        self.assertTrue(res_rebase["success"])
        self.assertIn("resolved", res_rebase)
        self.assertEqual(res_rebase["resolved"]["strategy"], "rebase")

    def test_cli_resolve_all_batch_rebase(self):
        local_repo = self._create_diverged_repo_pair("cli-resolve-rebase")
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main([
                "--root", str(self.root),
                "resolve", "--all",
                "--strategy", "rebase",
                "--no-fetch",
            ])
        self.assertEqual(exit_code, 0)
        self.assertIn("rebase", stdout_capture.getvalue().lower())

        details = get_divergence_details(str(local_repo))
        self.assertFalse(details["is_diverged"])

    def test_cli_resolve_json_output(self):
        local_repo = self._create_diverged_repo_pair("cli-resolve-json")
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main([
                "--root", str(self.root),
                "--json",
                "resolve", "cli-resolve-json",
                "--strategy", "abort",
                "--no-fetch",
            ])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout_capture.getvalue())
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["strategy"], "abort")
        self.assertTrue(payload[0]["details"]["is_diverged"])

    def test_cli_resolve_diff_flag(self):
        local_repo = self._create_diverged_repo_pair("cli-resolve-diff")
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main([
                "--root", str(self.root),
                "resolve", "cli-resolve-diff",
                "--diff",
                "--no-fetch",
            ])
        self.assertEqual(exit_code, 0)
        self.assertIn("Diff de divergência", stdout_capture.getvalue())
        self.assertIn("remote_file.txt", stdout_capture.getvalue())

    def test_interactive_resolution_prompt_rebase(self):
        local_repo = self._create_diverged_repo_pair("prompt-rebase")
        details = get_divergence_details(str(local_repo))

        # Simulate user choosing option 2 (rebase)
        with patch("sys.stdout", io.StringIO()):
            res = _prompt_resolution_choice(
                str(local_repo),
                "prompt-rebase",
                details,
                input_func=lambda _: "2",
            )
        self.assertTrue(res["success"])
        self.assertEqual(res["strategy"], "rebase")

    def test_interactive_resolution_prompt_diff_then_abort(self):
        local_repo = self._create_diverged_repo_pair("prompt-diff-abort")
        details = get_divergence_details(str(local_repo))

        # Sequence: user asks for diff (1), then aborts (5)
        responses = iter(["1", "5"])
        with patch("sys.stdout", io.StringIO()):
            res = _prompt_resolution_choice(
                str(local_repo),
                "prompt-diff-abort",
                details,
                input_func=lambda _: next(responses),
            )
        self.assertTrue(res["success"])
        self.assertEqual(res["strategy"], "abort")

    def test_split_diff_files(self):
        diff = (
            "diff --git a/file1.py b/file1.py\n"
            "index 111..222 100644\n"
            "--- a/file1.py\n"
            "+++ b/file1.py\n"
            "@@ -1 +1 @@\n"
            "+hello\n"
            "diff --git a/file2.py b/file2.py\n"
            "index 333..444 100644\n"
            "--- a/file2.py\n"
            "+++ b/file2.py\n"
            "@@ -1 +1 @@\n"
            "+world\n"
        )
        chunks = split_diff_files(diff)
        self.assertEqual(len(chunks), 2)
        self.assertIn("file1.py", chunks[0])
        self.assertNotIn("file2.py", chunks[0])
        self.assertIn("file2.py", chunks[1])
        self.assertEqual("".join(chunks), diff)

    def test_truncate_diff_no_truncation_under_budget(self):
        diff = "diff --git a/a.txt b/a.txt\n+line\n"
        # 34 chars <= 10 tokens * 4 = 40 chars
        truncated = truncate_diff(diff, max_tokens=10)
        self.assertEqual(truncated, diff)
        self.assertNotIn("truncated", truncated)

    def test_truncate_diff_none_or_empty(self):
        self.assertEqual(truncate_diff("", max_tokens=100), "")
        self.assertEqual(truncate_diff("diff --git a/a b/a", max_tokens=None), "diff --git a/a b/a")
        self.assertEqual(truncate_diff("diff --git a/a b/a", max_tokens=0), "diff --git a/a b/a")

    def test_truncate_diff_preserves_whole_files(self):
        file1 = (
            "diff --git a/file1.py b/file1.py\n"
            "--- a/file1.py\n"
            "+++ b/file1.py\n"
            "@@ -1 +1 @@\n"
            "+change1\n"
        )
        file2 = (
            "diff --git a/file2.py b/file2.py\n"
            "--- a/file2.py\n"
            "+++ b/file2.py\n"
            "@@ -1 +1 @@\n"
            "+change2\n"
        )
        file3 = (
            "diff --git a/file3.py b/file3.py\n"
            "--- a/file3.py\n"
            "+++ b/file3.py\n"
            "@@ -1 +1 @@\n"
            "+change3\n"
        )
        full_diff = file1 + file2 + file3
        # len(file1) ~ 75 chars, len(file2) ~ 75 chars, len(file3) ~ 75 chars. Total ~ 225 chars.
        # Budget for 2 files: ~38 tokens * 4 = 152 chars (fits file1 + file2, but not file3).
        budget_tokens = (len(file1) + len(file2) + 5) // 4
        result = truncate_diff(full_diff, max_tokens=budget_tokens)

        # File1 and File2 must be preserved entirely
        self.assertIn("file1.py", result)
        self.assertIn("+change1", result)
        self.assertIn("file2.py", result)
        self.assertIn("+change2", result)
        # File3 must be omitted
        self.assertNotIn("file3.py", result)
        self.assertNotIn("+change3", result)
        # Explicit marker must specify 1 more file and character count
        self.assertIn("[... truncated 1 more file(s), ~", result)

    def test_truncate_diff_oversized_single_file(self):
        # 1 file with 100 lines of diff
        lines = ["diff --git a/huge.py b/huge.py\n", "--- a/huge.py\n", "+++ b/huge.py\n"]
        for i in range(50):
            lines.append(f"+line number {i:03d} content here\n")
        diff = "".join(lines)
        # Budget: 20 tokens * 4 = 80 chars (first file is >1000 chars)
        result = truncate_diff(diff, max_tokens=20)

        self.assertIn("huge.py", result)
        self.assertIn("[... truncated 0 more file(s), ~", result)
        # Output length must respect token budget or line boundary
        self.assertLess(len(result), len(diff))

    def test_truncate_diff_oversized_first_file_with_multiple_files(self):
        file1 = "diff --git a/huge.py b/huge.py\n" + ("+change\n" * 100)
        file2 = "diff --git a/small.py b/small.py\n+tiny\n"
        full_diff = file1 + file2
        result = truncate_diff(full_diff, max_tokens=20)
        # First file is partially truncated, 1 more file omitted
        self.assertIn("[... truncated 1 more file(s), ~", result)

    def test_truncate_diff_custom_chars_per_token(self):
        diff = "diff --git a/a.txt b/a.txt\n" + ("+line\n" * 20)
        # 10 tokens at 2 chars/token = 20 chars max
        result = truncate_diff(diff, max_tokens=10, chars_per_token=2)
        self.assertIn("truncated", result)

    def test_get_divergence_diff_with_token_budget(self):
        local_repo = self._create_diverged_repo_pair("diff-budget-repo")
        # Unbounded diff
        full_diff = get_divergence_diff(str(local_repo))
        self.assertIn("remote_file.txt", full_diff)

        # Truncated diff with very tight token budget
        truncated_diff = get_divergence_diff(str(local_repo), max_tokens=5)
        self.assertIn("[... truncated", truncated_diff)

    def test_cli_resolve_diff_with_max_tokens(self):
        local_repo = self._create_diverged_repo_pair("cli-diff-tokens")
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main([
                "--root", str(self.root),
                "resolve", "cli-diff-tokens",
                "--diff",
                "--max-tokens", "5",
                "--no-fetch",
            ])
        self.assertEqual(exit_code, 0)
        output = stdout_capture.getvalue()
        self.assertIn("Diff de divergência", output)
        self.assertIn("truncated", output)

    def test_cli_resolve_diff_compact_default(self):
        local_repo = self._create_diverged_repo_pair("cli-diff-compact")
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main([
                "--root", str(self.root),
                "--compact",
                "resolve", "cli-diff-compact",
                "--diff",
                "--no-fetch",
            ])
        self.assertEqual(exit_code, 0)
        output = stdout_capture.getvalue()
        self.assertIn("Diff de divergência", output)

    def test_cli_resolve_diff_json_with_max_tokens(self):
        local_repo = self._create_diverged_repo_pair("cli-diff-json-tokens")
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main([
                "--root", str(self.root),
                "--json",
                "resolve", "cli-diff-json-tokens",
                "--diff",
                "--max-tokens", "5",
                "--no-fetch",
            ])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout_capture.getvalue())
        self.assertIsInstance(payload, list)
        self.assertIn("diff", payload[0])
        self.assertIn("[... truncated", payload[0]["diff"])


if __name__ == "__main__":
    unittest.main()
