"""
Automated unit tests for performance telemetry, bulk cache optimizations, and multi-remote coordination.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.core import (
    BulkCache,
    DEFAULT_ROOT,
    DEFAULT_WORKERS,
    TelemetryBenchmark,
    get_repo_remotes,
    get_submodules_status,
    get_worktree_info,
    inspect_repo,
    pull_repo,
    push_repo,
    run_git,
)


class TestTelemetryBenchmark(unittest.TestCase):
    """Test telemetry benchmark timer, metrics, and formatting."""

    def test_benchmark_lifecycle_and_metrics(self):
        bench = TelemetryBenchmark(total_repos=20, workers=8)
        self.assertGreaterEqual(bench.duration_seconds, 0.0)
        self.assertEqual(bench.total_repos, 20)
        self.assertEqual(bench.workers, 8)

        # Record stages
        bench.record_stage("discovery", 0.05)
        bench.record_stage("inspection", 0.45)
        self.assertEqual(bench.stages["discovery"], 0.05)
        self.assertEqual(bench.stages["inspection"], 0.45)

        # Stop benchmark with explicit duration
        bench.start_time = time.perf_counter() - 0.5
        total = bench.stop()
        self.assertGreaterEqual(total, 0.4)
        self.assertEqual(bench.duration_seconds, total)

        # Test repos per second computation
        self.assertGreater(bench.repos_per_second, 0.0)

        # Test dictionary export
        data = bench.to_dict()
        self.assertIn("duration_seconds", data)
        self.assertEqual(data["total_repos"], 20)
        self.assertEqual(data["workers"], 8)
        self.assertIn("discovery", data["stages"])
        self.assertIn("inspection", data["stages"])
        self.assertIn("cache", data)

        # Test summary line formatting
        bench.cache_hits = 15
        summary = bench.summary_line()
        self.assertIn("Benchmark:", summary)
        self.assertIn("20 repos", summary)
        self.assertIn("8 workers", summary)
        self.assertIn("discovery: 0.05s", summary)
        self.assertIn("15 hits", summary)

    def test_benchmark_zero_division_guard(self):
        bench = TelemetryBenchmark(total_repos=0, workers=4)
        bench.start_time = time.perf_counter()
        bench.end_time = bench.start_time  # 0 duration
        self.assertEqual(bench.repos_per_second, 0.0)


class TestBulkCache(unittest.TestCase):
    """Test thread-safe bulk cache lifecycle, memoization, and submodule avoidance."""

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
        (path / "file.txt").write_text("content\n")
        run_git(str(path), ["add", "file.txt"])
        run_git(str(path), ["commit", "-m", "Initial commit"])
        return path

    def test_cache_disabled_mode(self):
        cache = BulkCache(enabled=False)
        repo = self._init_repo(self.root / "repo")
        info = cache.get_worktree_info(str(repo))
        self.assertFalse(info["is_worktree"])
        stats = cache.stats
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)

    def test_worktree_and_remote_caching(self):
        cache = BulkCache(enabled=True)
        repo = self._init_repo(self.root / "repo")
        run_git(str(repo), ["remote", "add", "origin", "https://github.com/org/repo.git"])

        # First query: cache miss
        remotes1 = cache.get_remotes(str(repo))
        self.assertEqual(remotes1.get("origin"), "https://github.com/org/repo.git")
        stats1 = cache.stats
        self.assertEqual(stats1["misses"], 1)
        self.assertEqual(stats1["hits"], 0)

        # Second query: cache hit
        remotes2 = cache.get_remotes(str(repo))
        self.assertEqual(remotes2, remotes1)
        stats2 = cache.stats
        self.assertEqual(stats2["misses"], 1)
        self.assertEqual(stats2["hits"], 1)

        # Invalidation
        cache.invalidate(str(repo))
        remotes3 = cache.get_remotes(str(repo))
        self.assertEqual(remotes3, remotes1)
        stats3 = cache.stats
        self.assertEqual(stats3["misses"], 2)
        self.assertEqual(stats3["hits"], 1)

        # Clear
        cache.clear()
        self.assertEqual(cache.stats["hits"], 0)
        self.assertEqual(cache.stats["misses"], 0)

    def test_submodule_disk_check_optimization(self):
        cache = BulkCache(enabled=True)
        repo = self._init_repo(self.root / "no_submodules_repo")

        # Repository without .gitmodules should return immediately without executing git submodule status
        subs = cache.get_submodules_status(str(repo))
        self.assertEqual(subs, [])
        # Since it exited early, cache misses should be 0!
        self.assertEqual(cache.stats["misses"], 0)

        # If .gitmodules exists
        (repo / ".gitmodules").write_text("[submodule \"vendor/lib\"]\n\tpath = vendor/lib\n\turl = https://github.com/org/lib.git\n")
        subs_with_file = cache.get_submodules_status(str(repo))
        self.assertEqual(cache.stats["misses"], 1)

    def test_worktree_family_bulk_population(self):
        cache = BulkCache(enabled=True)
        repo = self._init_repo(self.root / "main_repo")
        wt_path = self.root / "linked_wt"

        # Create linked worktree
        run_git(str(repo), ["worktree", "add", "-b", "feat-wt", str(wt_path)])

        # Query main worktree
        info_main = cache.get_worktree_info(str(repo))
        self.assertFalse(info_main["is_worktree"])
        self.assertEqual(len(info_main["worktrees"]), 2)
        self.assertEqual(cache.stats["misses"], 1)

        # Querying the linked worktree should now HIT cache without re-invoking git
        info_wt = cache.get_worktree_info(str(wt_path))
        self.assertTrue(info_wt["is_worktree"])
        self.assertEqual(cache.stats["hits"], 1)


class TestMultiRemoteCoordination(unittest.TestCase):
    """Test multi-remote discovery, inspection, fetch coordination, and push targeting."""

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
        (path / "file.txt").write_text("main content\n")
        run_git(str(path), ["add", "file.txt"])
        run_git(str(path), ["commit", "-m", "Initial commit"])
        return path

    def _init_bare_remote(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        run_git(str(path), ["init", "--bare", "-b", "main"])
        return path

    def test_get_repo_remotes_multiple(self):
        repo = self._init_repo(self.root / "multi_repo")
        run_git(str(repo), ["remote", "add", "origin", "https://github.com/org/origin.git"])
        run_git(str(repo), ["remote", "add", "upstream", "https://github.com/upstream/origin.git"])
        run_git(str(repo), ["remote", "add", "backup", "git@gitlab.com:backup/repo.git"])

        remotes = get_repo_remotes(str(repo))
        self.assertEqual(len(remotes), 3)
        self.assertEqual(remotes["origin"], "https://github.com/org/origin.git")
        self.assertEqual(remotes["upstream"], "https://github.com/upstream/origin.git")
        self.assertEqual(remotes["backup"], "git@gitlab.com:backup/repo.git")

    def test_inspect_repo_multi_remote_details(self):
        repo = self._init_repo(self.root / "org" / "multi_repo")
        run_git(str(repo), ["remote", "add", "origin", "https://github.com/org/origin.git"])
        run_git(str(repo), ["remote", "add", "upstream", "https://github.com/upstream/origin.git"])

        info = inspect_repo(str(repo), str(self.root), do_fetch=False)
        self.assertTrue(info["multi_remote"])
        self.assertIn("origin", info["remotes"])
        self.assertIn("upstream", info["remotes"])
        self.assertEqual(info["remote_url"], "https://github.com/org/origin.git")
        self.assertEqual(info["target_remote"], "origin")

    def test_inspect_repo_targeted_remote(self):
        repo = self._init_repo(self.root / "org" / "multi_repo")
        run_git(str(repo), ["remote", "add", "origin", "https://github.com/org/origin.git"])
        run_git(str(repo), ["remote", "add", "upstream", "https://github.com/upstream/origin.git"])

        # Target upstream specifically
        info = inspect_repo(str(repo), str(self.root), do_fetch=False, remote="upstream")
        self.assertEqual(info["target_remote"], "upstream")
        self.assertEqual(info["remote_url"], "https://github.com/upstream/origin.git")

        # Target non-existent remote
        info_err = inspect_repo(str(repo), str(self.root), do_fetch=False, remote="missing_remote")
        self.assertEqual(info_err["status"], "NO_REMOTE")
        self.assertIn("missing_remote", info_err["fetch_error"])

    def test_push_repo_with_specific_remote(self):
        remote1 = self._init_bare_remote(self.root / "remote1.git")
        remote2 = self._init_bare_remote(self.root / "remote2.git")
        repo = self._init_repo(self.root / "local_repo")

        run_git(str(repo), ["remote", "add", "origin", str(remote1)])
        run_git(str(repo), ["remote", "add", "backup", str(remote2)])

        # Push to origin
        res_origin = push_repo(str(repo), branch="main", remote="origin")
        self.assertTrue(res_origin["success"])
        self.assertEqual(res_origin["remote"], "origin")

        # Push to backup
        res_backup = push_repo(str(repo), branch="main", remote="backup")
        self.assertTrue(res_backup["success"])
        self.assertEqual(res_backup["remote"], "backup")

        # Push to invalid remote
        res_inv = push_repo(str(repo), branch="main", remote="nonexistent")
        self.assertFalse(res_inv["success"])
        self.assertIn("not configured", res_inv["stderr"])


class TestCliTelemetryAndMultiRemote(unittest.TestCase):
    """Test CLI commands with telemetry benchmarks, caching options, and remote targeting."""

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
        (path / "file.txt").write_text("content\n")
        run_git(str(path), ["add", "file.txt"])
        run_git(str(path), ["commit", "-m", "Initial commit"])
        return path

    def test_cli_status_benchmark_text(self):
        repo = self._init_repo(self.root / "repo1")
        run_git(str(repo), ["remote", "add", "origin", "https://github.com/org/repo1.git"])

        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            code = main(["status", "--root", str(self.root), "--no-fetch", "--benchmark"])

        self.assertEqual(code, 0)
        output = out_buf.getvalue()
        self.assertIn("Benchmark:", output)
        self.assertIn("repo1", output)

    def test_cli_status_benchmark_json(self):
        repo = self._init_repo(self.root / "repo1")
        run_git(str(repo), ["remote", "add", "origin", "https://github.com/org/repo1.git"])

        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            code = main(["status", "--root", str(self.root), "--no-fetch", "--benchmark", "--json"])

        self.assertEqual(code, 0)
        data = json.loads(out_buf.getvalue())
        self.assertIn("telemetry", data)
        self.assertEqual(data["total"], 1)
        self.assertIn("duration_seconds", data["telemetry"])
        self.assertIn("stages", data["telemetry"])

    def test_cli_config_shows_benchmark_and_cache(self):
        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            code = main(["config"])

        self.assertEqual(code, 0)
        output = out_buf.getvalue()
        self.assertIn("Benchmark:", output)
        self.assertIn("Bulk Cache:", output)
        self.assertIn("Remote:", output)


if __name__ == "__main__":
    unittest.main()
