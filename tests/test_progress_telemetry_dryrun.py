"""
Unit tests for terminal progress indicators, structured telemetry breakdowns, and comprehensive dry-run operations.
"""

import concurrent.futures
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.config import GitSyncConfig, load_config
from git_sync.core import (
    BulkCache,
    DEFAULT_ROOT,
    DEFAULT_WORKERS,
    ProgressIndicator,
    TelemetryBenchmark,
    check_repo_dirty,
    inspect_repo,
    pull_repo,
    push_repo,
    resolve_divergence,
    run_git,
)


class TestProgressIndicator(unittest.TestCase):
    """Test safe terminal progress indicators and non-blocking spinner mechanics."""

    def test_progress_indicator_advance_and_frames(self):
        stream = io.StringIO()
        with ProgressIndicator(message="Auditing repos", total=5, enabled=True, stream=stream) as p:
            self.assertEqual(p.count, 0)
            p.advance("repo-1")
            self.assertEqual(p.count, 1)
            p.advance("repo-2")
            self.assertEqual(p.count, 2)

        output = stream.getvalue()
        self.assertIn("Auditing repos", output)
        self.assertIn("repo-1", output)
        self.assertIn("repo-2", output)
        self.assertIn("[ 1/ 5]", output)
        self.assertIn("(20%)", output)

    def test_progress_indicator_update_explicit(self):
        stream = io.StringIO()
        p = ProgressIndicator(message="Syncing", total=10, enabled=True, stream=stream)
        p.update(5, "my-repo")
        self.assertEqual(p.count, 5)
        p.finish()
        output = stream.getvalue()
        self.assertIn("[ 5/10]", output)
        self.assertIn("(50%)", output)
        self.assertIn("my-repo", output)

    def test_progress_indicator_suppressed_when_disabled(self):
        stream = io.StringIO()
        with ProgressIndicator(message="Silent task", total=10, enabled=False, stream=stream) as p:
            p.advance("ignored-repo")
            p.update(5, "ignored-repo-2")
        self.assertEqual(stream.getvalue(), "")

    def test_progress_indicator_auto_suppression_non_tty(self):
        # When enabled=None and stream is a StringIO (non-TTY), it auto-suppresses
        stream = io.StringIO()
        with ProgressIndicator(message="Auto silent", total=10, enabled=None, stream=stream) as p:
            self.assertFalse(p.enabled)
            p.advance("repo-x")
        self.assertEqual(stream.getvalue(), "")

    def test_progress_indicator_auto_suppression_dumb_terminal(self):
        # Even if stream pretends to be a TTY, TERM=dumb forces suppression
        class FakeTTYStream(io.StringIO):
            def isatty(self):
                return True

        stream = FakeTTYStream()
        with patch.dict(os.environ, {"TERM": "dumb", "ANTIGRAVITY_AGENT": "", "CI": ""}, clear=False):
            with patch("sys.stdout.isatty", return_value=True):
                with ProgressIndicator(message="Dumb terminal", total=10, enabled=None, stream=stream) as p:
                    self.assertFalse(p.enabled)
                    p.advance("repo-dumb")
        self.assertEqual(stream.getvalue(), "")

    def test_progress_indicator_auto_suppression_agent_and_ci(self):
        # Even if stream is a TTY, ANTIGRAVITY_AGENT or CI forces suppression
        class FakeTTYStream(io.StringIO):
            def isatty(self):
                return True

        stream = FakeTTYStream()
        with patch.dict(os.environ, {"ANTIGRAVITY_AGENT": "1", "TERM": "xterm-256color"}, clear=False):
            with patch("sys.stdout.isatty", return_value=True):
                with ProgressIndicator(message="Agent session", total=10, enabled=None, stream=stream) as p:
                    self.assertFalse(p.enabled)
                    p.advance("repo-agent")
        self.assertEqual(stream.getvalue(), "")

    def test_progress_indicator_finish_clears_line(self):
        stream = io.StringIO()
        p = ProgressIndicator(message="Testing finish", total=1, enabled=True, stream=stream)
        p.advance("item")
        p.finish()
        output = stream.getvalue()
        # Verify carriage return and 79 space overwrite sequence
        self.assertIn("\r" + " " * 79 + "\r", output)

    def test_progress_indicator_concurrent_safety(self):
        stream = io.StringIO()
        p = ProgressIndicator(message="Concurrent audit", total=100, enabled=True, stream=stream)

        def worker(idx: int):
            for i in range(10):
                p.advance(f"task-{idx}-{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(worker, range(10)))

        self.assertEqual(p.count, 100)
        p.finish()


class TestTelemetryEnhancements(unittest.TestCase):
    """Test structured telemetry breakdowns of concurrency, latencies, and failure metrics."""

    def test_telemetry_concurrency_and_latencies(self):
        bench = TelemetryBenchmark(workers=16, total_repos=40)
        bench.active_workers = 12

        # Record latencies
        bench.record_network_latency(1.234)
        bench.record_network_latency(0.566)
        bench.record_disk_latency(0.123)
        bench.record_disk_latency(0.077)

        # Record failures
        bench.record_failure("fetch_error")
        bench.record_failure("no_remote")
        bench.record_failure("fetch_error")

        data = bench.to_dict()

        # Concurrency verification
        self.assertIn("concurrency", data)
        self.assertEqual(data["concurrency"]["configured_workers"], 16)
        self.assertEqual(data["concurrency"]["active_workers"], 12)

        # Latencies verification
        self.assertIn("latencies", data)
        self.assertAlmostEqual(data["latencies"]["network_seconds"], 1.8, places=3)
        self.assertAlmostEqual(data["latencies"]["disk_seconds"], 0.2, places=3)

        # Failures verification
        self.assertIn("failures", data)
        self.assertEqual(data["failures"]["total"], 3)
        self.assertEqual(data["failures"]["by_type"]["fetch_error"], 2)
        self.assertEqual(data["failures"]["by_type"]["no_remote"], 1)

    def test_telemetry_summary_line_with_failures_and_latency(self):
        bench = TelemetryBenchmark(workers=8, total_repos=25)
        bench.record_stage("discovery", 0.05)
        bench.record_stage("inspection", 0.35)
        bench.record_network_latency(0.8)
        bench.record_disk_latency(0.2)
        bench.record_failure("fetch_error")
        bench.cache_hits = 10

        summary = bench.summary_line()
        self.assertIn("⚡ Benchmark:", summary)
        self.assertIn("net: 0.80s, disk: 0.20s", summary)
        self.assertIn("25 repos", summary)
        self.assertIn("8 workers", summary)
        self.assertIn("cache: 10 hits", summary)
        self.assertIn("1 failure(s)", summary)


class TestDryRunOperations(unittest.TestCase):
    """Test dry-run simulation mode across pull and resolve operations."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name)

    def tearDown(self):
        self.test_dir.cleanup()

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

    def test_pull_repo_dry_run_fast_forward_preview(self):
        # Create bare remote and seed commit
        remote_dir = self._init_bare_remote(self.root / "remote.git")
        seed_dir = self._init_repo(self.root / "seed")
        (seed_dir / "file.txt").write_text("v1\n")
        run_git(str(seed_dir), ["add", "file.txt"])
        run_git(str(seed_dir), ["commit", "-m", "commit 1"])
        run_git(str(seed_dir), ["remote", "add", "origin", str(remote_dir)])
        run_git(str(seed_dir), ["push", "-u", "origin", "main"])

        # Clone to local repo
        local_dir = self.root / "local"
        run_git(str(self.root), ["clone", str(remote_dir), "local"])
        run_git(str(local_dir), ["config", "user.email", "test@example.com"])
        run_git(str(local_dir), ["config", "user.name", "Test User"])

        # Push commit 2 from seed to remote
        (seed_dir / "file.txt").write_text("v2\n")
        run_git(str(seed_dir), ["commit", "-am", "commit 2"])
        run_git(str(seed_dir), ["push", "origin", "main"])

        # Fetch in local so tracking branch is updated
        run_git(str(local_dir), ["fetch"])

        # Dry-run pull on local
        head_before = run_git(str(local_dir), ["rev-parse", "HEAD"]).stdout.strip()
        res = pull_repo(str(local_dir), dry_run=True)

        self.assertTrue(res["dry_run"])
        self.assertTrue(res["success"])
        self.assertEqual(res["behind"], 1)
        self.assertEqual(res["ahead"], 0)
        self.assertIn("Fast-forward update possible", res["stdout"])

        # INVARIANT: HEAD MUST NOT change during dry-run
        head_after = run_git(str(local_dir), ["rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(head_before, head_after)

    def test_pull_repo_dry_run_dirty_tree_preservation(self):
        # Remote and local setup
        remote_dir = self._init_bare_remote(self.root / "remote.git")
        seed_dir = self._init_repo(self.root / "seed")
        (seed_dir / "file.txt").write_text("v1\n")
        run_git(str(seed_dir), ["add", "file.txt"])
        run_git(str(seed_dir), ["commit", "-m", "init"])
        run_git(str(seed_dir), ["remote", "add", "origin", str(remote_dir)])
        run_git(str(seed_dir), ["push", "-u", "origin", "main"])

        local_dir = self.root / "local"
        run_git(str(self.root), ["clone", str(remote_dir), "local"])
        run_git(str(local_dir), ["config", "user.email", "test@example.com"])
        run_git(str(local_dir), ["config", "user.name", "Test User"])

        # Make local working tree dirty with untracked and modified files
        (local_dir / "file.txt").write_text("modified uncommitted\n")
        (local_dir / "untracked.txt").write_text("new file\n")

        # Execute dry-run pull
        res = pull_repo(str(local_dir), dry_run=True)

        self.assertTrue(res["dry_run"])
        self.assertTrue(res["autostash"]["would_stash"])
        self.assertFalse(res["autostash"]["stashed"])
        self.assertEqual(len(res["autostash"]["dirty_files"]), 2)

        # INVARIANT: Stash list MUST remain empty; files MUST remain uncommitted in working tree
        p_stash = run_git(str(local_dir), ["stash", "list"])
        self.assertEqual(p_stash.stdout.strip(), "")
        is_dirty, dirty_files = check_repo_dirty(str(local_dir))
        self.assertTrue(is_dirty)
        self.assertEqual(len(dirty_files), 2)

    def test_resolve_divergence_dry_run_strategies(self):
        repo_dir = self._init_repo(self.root / "diverged_repo")
        remote_dir = self._init_bare_remote(self.root / "bare.git")
        (repo_dir / "f.txt").write_text("base\n")
        run_git(str(repo_dir), ["add", "f.txt"])
        run_git(str(repo_dir), ["commit", "-m", "base"])
        run_git(str(repo_dir), ["remote", "add", "origin", str(remote_dir)])
        run_git(str(repo_dir), ["push", "-u", "origin", "main"])

        # Create upstream divergence: remote commit
        other_dir = self._init_repo(self.root / "other")
        run_git(str(other_dir), ["remote", "add", "origin", str(remote_dir)])
        run_git(str(other_dir), ["pull", "origin", "main"])
        (other_dir / "remote.txt").write_text("remote\n")
        run_git(str(other_dir), ["add", "remote.txt"])
        run_git(str(other_dir), ["commit", "-m", "remote commit"])
        run_git(str(other_dir), ["push", "origin", "main"])

        # Create local commit
        (repo_dir / "local.txt").write_text("local\n")
        run_git(str(repo_dir), ["add", "local.txt"])
        run_git(str(repo_dir), ["commit", "-m", "local commit"])

        # Fetch in repo_dir to track divergence
        run_git(str(repo_dir), ["fetch", "origin"])

        head_before = run_git(str(repo_dir), ["rev-parse", "HEAD"]).stdout.strip()

        # 1. Test dry-run rebase
        res_rebase = resolve_divergence(str(repo_dir), strategy="rebase", dry_run=True)
        self.assertTrue(res_rebase["dry_run"])
        self.assertTrue(res_rebase["success"])
        self.assertIn("Would rebase", res_rebase["message"])
        self.assertEqual(run_git(str(repo_dir), ["rev-parse", "HEAD"]).stdout.strip(), head_before)

        # 2. Test dry-run merge
        res_merge = resolve_divergence(str(repo_dir), strategy="merge", dry_run=True)
        self.assertTrue(res_merge["dry_run"])
        self.assertTrue(res_merge["success"])
        self.assertIn("Would merge", res_merge["message"])
        self.assertEqual(run_git(str(repo_dir), ["rev-parse", "HEAD"]).stdout.strip(), head_before)

        # 3. Test dry-run stash
        (repo_dir / "dirty.txt").write_text("dirty\n")
        res_stash = resolve_divergence(str(repo_dir), strategy="stash", dry_run=True)
        self.assertTrue(res_stash["dry_run"])
        self.assertTrue(res_stash["success"])
        self.assertIn("Would stash", res_stash["message"])
        # Verify no actual stash created
        self.assertEqual(run_git(str(repo_dir), ["stash", "list"]).stdout.strip(), "")


class TestCliDryRunIntegration(unittest.TestCase):
    """Test CLI commands in dry-run mode."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name)

    def tearDown(self):
        self.test_dir.cleanup()

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

    def test_cli_pull_dry_run_text_and_json(self):
        remote_dir = self._init_bare_remote(self.root / "origin.git")
        seed_dir = self._init_repo(self.root / "seed")
        (seed_dir / "file.txt").write_text("v1\n")
        run_git(str(seed_dir), ["add", "file.txt"])
        run_git(str(seed_dir), ["commit", "-m", "init"])
        run_git(str(seed_dir), ["remote", "add", "origin", str(remote_dir)])
        run_git(str(seed_dir), ["push", "-u", "origin", "main"])

        local_dir = self.root / "cli_repo"
        run_git(str(self.root), ["clone", str(remote_dir), "cli_repo"])
        run_git(str(local_dir), ["config", "user.email", "test@example.com"])
        run_git(str(local_dir), ["config", "user.name", "Test User"])

        # Push an update to remote
        (seed_dir / "file.txt").write_text("v2\n")
        run_git(str(seed_dir), ["commit", "-am", "v2"])
        run_git(str(seed_dir), ["push", "origin", "main"])

        # 1. Text dry-run pull
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main(["pull", "cli_repo", "--root", str(self.root), "--dry-run"])
        self.assertEqual(exit_code, 0)
        text_out = stdout_capture.getvalue()
        self.assertIn("[DRY-RUN]", text_out)

        # 2. JSON dry-run pull
        stdout_json = io.StringIO()
        with patch("sys.stdout", stdout_json):
            exit_code = main(["pull", "cli_repo", "--root", str(self.root), "--dry-run", "--json"])
        self.assertEqual(exit_code, 0)
        parsed = json.loads(stdout_json.getvalue())
        self.assertEqual(len(parsed), 1)
        self.assertTrue(parsed[0]["dry_run"])

    def test_cli_config_reflects_pull_dry_run_setting(self):
        cfg_file = self.root / ".git-sync.json"
        cfg_data = {
            "root": str(self.root),
            "pull": {"dry_run": True},
            "push": {"dry_run": False}
        }
        cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main(["--config", str(cfg_file), "config"])
        self.assertEqual(exit_code, 0)
        text_out = stdout_capture.getvalue()
        self.assertIn("Pull (dry-run):      True", text_out)
        self.assertIn("Push (dry-run):      False", text_out)


if __name__ == "__main__":
    unittest.main()
