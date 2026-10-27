"""
End-to-end stress testing, fault injection scenarios, and benchmark throughput verification.
Validates bulk concurrent synchronization across 50+ synthetic repositories, locked index recovery,
network disconnections, and worker pool saturation.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.core import (
    DEFAULT_TIMEOUT,
    BulkCache,
    ProgressIndicator,
    TelemetryBenchmark,
    check_credentials_sanity,
    check_repo_dirty,
    diagnose_git_error,
    discover_repos,
    find_locked_repos,
    find_repo_locks,
    inspect_repo,
    is_transient_network_error,
    pull_repo,
    push_repo,
    recover_index_lock,
    recover_repo_locks,
    run_git,
    run_git_with_retry,
)


class TestMultiRepoConcurrencyStress(unittest.TestCase):
    """Stress tests simulating concurrent synchronization across 50+ synthetic repositories."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name)
        self.repos: list[Path] = []
        self._setup_synthetic_workspace(count=52)

    def tearDown(self):
        self.test_dir.cleanup()

    def _init_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        run_git(str(path), ["init", "-b", "main"])
        run_git(str(path), ["config", "user.email", "stress@example.com"])
        run_git(str(path), ["config", "user.name", "Stress Tester"])
        run_git(str(path), ["config", "commit.gpgsign", "false"])
        return path

    def _init_bare_remote(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        run_git(str(path), ["init", "--bare", "-b", "main"])
        return path

    def _setup_synthetic_workspace(self, count: int = 52):
        """Create 52 synthetic repositories across 3 organizational groups with varied git states."""
        groups = ["org-alpha", "org-beta", "org-gamma"]
        remotes_dir = self.root / "_remotes"
        remotes_dir.mkdir(parents=True, exist_ok=True)

        for i in range(count):
            group = groups[i % len(groups)]
            repo_path = self.root / group / f"repo_{i:02d}"
            self._init_repo(repo_path)
            self.repos.append(repo_path)

            # Baseline commit
            readme = repo_path / "README.md"
            readme.write_text(f"# Repository {i}\nBaseline content\n")
            run_git(str(repo_path), ["add", "README.md"])
            run_git(str(repo_path), ["commit", "-m", "Initial baseline commit"])

            # 1. First 20 repos: clean sync with remote
            if i < 20:
                bare = self._init_bare_remote(remotes_dir / f"repo_{i:02d}.git")
                run_git(str(repo_path), ["remote", "add", "origin", str(bare)])
                run_git(str(repo_path), ["push", "-u", "origin", "main"])

            # 2. Next 10 repos (20..29): pending pull (remote has new commit)
            elif 20 <= i < 30:
                bare = self._init_bare_remote(remotes_dir / f"repo_{i:02d}.git")
                run_git(str(repo_path), ["remote", "add", "origin", str(bare)])
                run_git(str(repo_path), ["push", "-u", "origin", "main"])

                # Push an extra commit to bare remote via temporary clone
                with tempfile.TemporaryDirectory() as cl_tmp:
                    cl_path = Path(cl_tmp) / "clone"
                    run_git(str(self.root), ["clone", str(bare), str(cl_path)])
                    run_git(str(cl_path), ["config", "user.email", "stress@example.com"])
                    run_git(str(cl_path), ["config", "user.name", "Stress Tester"])
                    run_git(str(cl_path), ["config", "commit.gpgsign", "false"])
                    (cl_path / "remote_update.txt").write_text(f"Remote update for repo {i}\n")
                    run_git(str(cl_path), ["add", "remote_update.txt"])
                    run_git(str(cl_path), ["commit", "-m", "Remote upstream feature commit"])
                    run_git(str(cl_path), ["push", "origin", "main"])

            # 3. Next 10 repos (30..39): dirty working tree (staged & unstaged)
            elif 30 <= i < 40:
                bare = self._init_bare_remote(remotes_dir / f"repo_{i:02d}.git")
                run_git(str(repo_path), ["remote", "add", "origin", str(bare)])
                run_git(str(repo_path), ["push", "-u", "origin", "main"])
                (repo_path / "untracked_file.txt").write_text("untracked work in progress\n")
                (repo_path / "README.md").write_text(f"# Repository {i}\nDirty modification\n")

            # 4. Next 6 repos (40..45): pending push (local commits ahead)
            elif 40 <= i < 46:
                bare = self._init_bare_remote(remotes_dir / f"repo_{i:02d}.git")
                run_git(str(repo_path), ["remote", "add", "origin", str(bare)])
                run_git(str(repo_path), ["push", "-u", "origin", "main"])
                (repo_path / "local_feature.txt").write_text(f"Local feature commit in repo {i}\n")
                run_git(str(repo_path), ["add", "local_feature.txt"])
                run_git(str(repo_path), ["commit", "-m", "Local ahead commit"])

            # 5. Next 4 repos (46..49): diverged
            elif 46 <= i < 50:
                bare = self._init_bare_remote(remotes_dir / f"repo_{i:02d}.git")
                run_git(str(repo_path), ["remote", "add", "origin", str(bare)])
                run_git(str(repo_path), ["push", "-u", "origin", "main"])

                # Remote commit
                with tempfile.TemporaryDirectory() as cl_tmp:
                    cl_path = Path(cl_tmp) / "clone"
                    run_git(str(self.root), ["clone", str(bare), str(cl_path)])
                    run_git(str(cl_path), ["config", "user.email", "stress@example.com"])
                    run_git(str(cl_path), ["config", "user.name", "Stress Tester"])
                    run_git(str(cl_path), ["config", "commit.gpgsign", "false"])
                    (cl_path / "diverge_remote.txt").write_text("Remote divergent work\n")
                    run_git(str(cl_path), ["add", "diverge_remote.txt"])
                    run_git(str(cl_path), ["commit", "-m", "Divergent remote commit"])
                    run_git(str(cl_path), ["push", "origin", "main"])

                # Local commit
                (repo_path / "diverge_local.txt").write_text("Local divergent work\n")
                run_git(str(repo_path), ["add", "diverge_local.txt"])
                run_git(str(repo_path), ["commit", "-m", "Divergent local commit"])

            # 6. Last 2 repos (50..51): no remote configured
            else:
                pass

    def test_stress_discovery_and_concurrent_inspection_50_plus(self):
        """Verify discovery and concurrent inspection across 50+ repositories without contention."""
        discovered = discover_repos(str(self.root))
        self.assertEqual(len(discovered), 52)

        cache = BulkCache(enabled=True)
        benchmark = TelemetryBenchmark(total_repos=len(discovered), workers=16)

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            inspections = list(executor.map(
                lambda r: inspect_repo(r, str(self.root), do_fetch=True, cache=cache, benchmark=benchmark),
                discovered
            ))
        elapsed = time.perf_counter() - t0
        benchmark.stop()

        self.assertEqual(len(inspections), 52)
        # Ensure high throughput (> 5 repos per second even with remote fetches)
        self.assertGreater(benchmark.repos_per_second, 5.0)

        status_counts = {}
        for item in inspections:
            st = item["status"]
            status_counts[st] = status_counts.get(st, 0) + 1

        self.assertEqual(status_counts.get("UP_TO_DATE", 0), 30)
        self.assertEqual(status_counts.get("PULL_PENDING", 0), 10)
        self.assertEqual(status_counts.get("PUSH_PENDING", 0), 6)
        self.assertEqual(status_counts.get("DIVERGED", 0), 4)
        self.assertEqual(status_counts.get("NO_REMOTE", 0), 2)

        dirty_count = sum(1 for item in inspections if item["is_dirty"])
        self.assertEqual(dirty_count, 10)

    def test_stress_bulk_pull_and_autostash_50_plus(self):
        """Verify concurrent bulk pull with autostash across all repositories requiring updates."""
        discovered = discover_repos(str(self.root))
        cache = BulkCache(enabled=True)

        # Inspect all first
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            inspections = list(executor.map(
                lambda r: inspect_repo(r, str(self.root), do_fetch=True, cache=cache),
                discovered
            ))

        pending_pull = [item["path"] for item in inspections if item["status"] == "PULL_PENDING"]
        self.assertEqual(len(pending_pull), 10)

        # Execute concurrent pulls with autostash enabled
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            pull_results = list(executor.map(
                lambda r: pull_repo(r, autostash=True, cache=cache),
                pending_pull
            ))

        for res in pull_results:
            self.assertTrue(res["success"], f"Pull failed for {res['repo']}: {res['stderr']}")

        # Re-inspect pulled repositories: should now be UP_TO_DATE
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            re_inspected = list(executor.map(
                lambda r: inspect_repo(r, str(self.root), do_fetch=False, cache=cache),
                pending_pull
            ))

        for item in re_inspected:
            self.assertEqual(item["status"], "UP_TO_DATE")
            self.assertEqual(item["behind"], 0)

    def test_cli_compact_json_summary_50_plus(self):
        """Verify CLI compact JSON triage across 50+ repositories."""
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main(["status", "--compact", "--json", "--root", str(self.root), "--fetch"])
        self.assertEqual(exit_code, 0)

        data = json.loads(stdout_buf.getvalue())
        self.assertEqual(data["total"], 52)
        self.assertEqual(len(data["dirty"]), 10)
        self.assertEqual(len(data["push_pending"]), 6)
        self.assertEqual(len(data["diverged"]), 4)


class TestFaultInjectionAndHardening(unittest.TestCase):
    """Fault injection scenarios verifying recovery under locked indexes and interrupted fetches."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.test_dir.name)
        self.repo = self.root / "fault_repo"
        self._init_repo(self.repo)

    def tearDown(self):
        self.test_dir.cleanup()

    def _init_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        run_git(str(path), ["init", "-b", "main"])
        run_git(str(path), ["config", "user.email", "fault@example.com"])
        run_git(str(path), ["config", "user.name", "Fault Tester"])
        run_git(str(path), ["config", "commit.gpgsign", "false"])
        (path / "file.txt").write_text("initial\n")
        run_git(str(path), ["add", "file.txt"])
        run_git(str(path), ["commit", "-m", "init"])
        return path

    def test_locked_index_error_diagnosis(self):
        """Verify diagnose_git_error correctly classifies locked index errors."""
        err_msg = (
            "fatal: Unable to create '/path/to/.git/index.lock': File exists.\n"
            "Another git process seems to be running in this repository."
        )
        diag = diagnose_git_error(err_msg)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "INDEX_LOCKED")
        self.assertEqual(diag["action_required"], "unlock_index")
        self.assertIn("index.lock", diag["message"])

        # Reference lock
        err_ref = "fatal: Unable to create '/path/to/.git/FETCH_HEAD.lock': File exists."
        diag_ref = diagnose_git_error(err_ref)
        self.assertIsNotNone(diag_ref)
        self.assertEqual(diag_ref["category"], "INDEX_LOCKED")

    def test_find_repo_locks_and_stale_detection(self):
        """Verify find_repo_locks discovers active and stale locks with age calculations."""
        lock_file = self.repo / ".git" / "index.lock"
        lock_file.write_text("dummy lock")

        # 1. Fresh lock (<300s)
        locks = find_repo_locks(str(self.repo), stale_threshold_seconds=300.0)
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["lock_name"], "index.lock")
        self.assertFalse(locks[0]["stale"])

        # 2. Stale lock (>300s): artificially age mtime by 1 hour
        stale_time = time.time() - 3600
        os.utime(str(lock_file), (stale_time, stale_time))

        locks_stale = find_repo_locks(str(self.repo), stale_threshold_seconds=300.0)
        self.assertEqual(len(locks_stale), 1)
        self.assertTrue(locks_stale[0]["stale"])
        self.assertGreater(locks_stale[0]["age_seconds"], 3500)

    def test_recover_index_lock_active_preservation_and_stale_clearing(self):
        """Verify recover_index_lock refuses to delete fresh active locks without force."""
        lock_file = self.repo / ".git" / "index.lock"
        lock_file.write_text("locked")

        # Fresh lock without force: must be preserved
        ok, msg = recover_index_lock(str(self.repo), force=False, max_age_seconds=300.0)
        self.assertFalse(ok)
        self.assertIn("active", msg)
        self.assertTrue(lock_file.exists())

        # Fresh lock with force=True: must be cleared
        ok_force, msg_force = recover_index_lock(str(self.repo), force=True)
        self.assertTrue(ok_force)
        self.assertFalse(lock_file.exists())

        # Stale lock: recreated and aged
        lock_file.write_text("locked again")
        stale_time = time.time() - 400
        os.utime(str(lock_file), (stale_time, stale_time))

        ok_stale, msg_stale = recover_index_lock(str(self.repo), force=False, max_age_seconds=300.0)
        self.assertTrue(ok_stale)
        self.assertIn("stale", msg_stale)
        self.assertFalse(lock_file.exists())

    def test_pull_repo_recovers_from_stale_index_lock(self):
        """Verify pull_repo with recover_stale_lock recovers from stale index lock and finishes pull."""
        bare = self.root / "bare.git"
        bare.mkdir(parents=True)
        run_git(str(bare), ["init", "--bare", "-b", "main"])
        run_git(str(self.repo), ["remote", "add", "origin", str(bare)])
        run_git(str(self.repo), ["push", "-u", "origin", "main"])

        # Push commit to bare remote
        with tempfile.TemporaryDirectory() as cl_tmp:
            cl = Path(cl_tmp) / "clone"
            run_git(str(self.root), ["clone", str(bare), str(cl)])
            run_git(str(cl), ["config", "user.email", "tester@example.com"])
            run_git(str(cl), ["config", "user.name", "Tester"])
            run_git(str(cl), ["config", "commit.gpgsign", "false"])
            (cl / "upstream.txt").write_text("data\n")
            run_git(str(cl), ["add", "upstream.txt"])
            run_git(str(cl), ["commit", "-m", "upstream commit"])
            run_git(str(cl), ["push", "origin", "main"])

        # Fetch tracking branch so repo knows it is behind
        run_git(str(self.repo), ["fetch"])

        # Inject stale index.lock in local repo
        lock_file = self.repo / ".git" / "index.lock"
        lock_file.write_text("crashed process")
        stale_time = time.time() - 600
        os.utime(str(lock_file), (stale_time, stale_time))

        # 1. pull_repo without recover_stale_lock should fail gracefully with INDEX_LOCKED diagnostic
        res_fail = pull_repo(str(self.repo), recover_stale_lock=False)
        self.assertFalse(res_fail["success"])
        self.assertIsNotNone(res_fail["diagnostic"])
        self.assertEqual(res_fail["diagnostic"]["category"], "INDEX_LOCKED")

        # 2. pull_repo with recover_stale_lock=True should automatically recover lock and succeed
        res_ok = pull_repo(str(self.repo), recover_stale_lock=True)
        self.assertTrue(res_ok["success"], f"Pull failed: {res_ok['stderr']}")
        self.assertFalse(lock_file.exists())
        self.assertTrue((self.repo / "upstream.txt").exists())

    def test_cmd_unlock_cli_workflow(self):
        """Verify git-sync unlock subcommand safely handles locked repositories."""
        lock_file = self.repo / ".git" / "index.lock"
        lock_file.write_text("locked")

        # 1. Fresh lock: skipped
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main(["unlock", "--all", "--root", str(self.root), "--json"])
        self.assertEqual(exit_code, 0)
        data = json.loads(stdout_buf.getvalue())
        self.assertEqual(data["total_locked"], 1)
        self.assertEqual(data["unlocked"][0]["status"], "SKIPPED_ACTIVE")
        self.assertTrue(lock_file.exists())

        # 2. Force unlock: removed
        stdout_buf2 = io.StringIO()
        with patch("sys.stdout", stdout_buf2):
            exit_code2 = main(["unlock", "--all", "--force", "--root", str(self.root), "--json"])
        self.assertEqual(exit_code2, 0)
        data2 = json.loads(stdout_buf2.getvalue())
        self.assertEqual(data2["total_locked"], 1)
        self.assertEqual(data2["unlocked"][0]["status"], "UNLOCKED")
        self.assertFalse(lock_file.exists())

    def test_interrupted_fetch_and_transient_error_classification(self):
        """Verify transient error classification and retry handling for interrupted fetches."""
        # Check diverse interrupted fetch signatures
        transient_messages = [
            "fatal: the remote end hung up unexpectedly",
            "fatal: fetch-pack: invalid index-pack output",
            "error: RPC failed; curl 56 GnuTLS recv error (-110): The TLS connection was non-properly terminated.",
            "fatal: early EOF",
            "fatal: connection reset by peer",
            "fatal: index-pack failed",
            "fatal: protocol error: bad line length",
            "Connection closed by remote host",
            "interrupted system call",
        ]
        for msg in transient_messages:
            self.assertTrue(
                is_transient_network_error(None, msg),
                f"Expected '{msg}' to be classified as transient network error"
            )

        # Permanent authentication errors must NEVER be transient
        permanent_messages = [
            "Permission denied (publickey).",
            "fatal: Authentication failed for 'https://github.com/repo.git'",
            "fatal: terminal prompts disabled",
            "fatal: could not read Username for 'https://github.com': No such device or address",
            "fatal: repository 'https://github.com/nonexistent/repo.git' not found",
        ]
        for p_msg in permanent_messages:
            self.assertFalse(
                is_transient_network_error(None, p_msg),
                f"Expected '{p_msg}' to NOT be classified as transient"
            )

    def test_run_git_with_retry_recovers_interrupted_call(self):
        """Verify run_git_with_retry handles transient failures and succeeds on subsequent attempt."""
        benchmark = TelemetryBenchmark(total_repos=1, workers=1)
        delays: list[float] = []

        def mock_sleep(d):
            delays.append(d)

        # Mock run_git to fail once with EOF, then succeed
        call_count = 0
        def fake_run_git(path, args, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return subprocess.CompletedProcess(
                    args=["git", "fetch"],
                    returncode=128,
                    stdout="",
                    stderr="fatal: the remote end hung up unexpectedly\nfatal: early EOF"
                )
            return subprocess.CompletedProcess(
                args=["git", "fetch"],
                returncode=0,
                stdout="From remote\n * [new branch] main -> origin/main",
                stderr=""
            )

        with patch("git_sync.core.run_git", side_effect=fake_run_git):
            proc = run_git_with_retry(
                str(self.repo),
                ["fetch"],
                retries=2,
                backoff_factor=0.1,
                benchmark=benchmark,
                sleep_func=mock_sleep,
            )

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(call_count, 2)
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], 0.1)
        # Benchmark must record retry recovery
        self.assertEqual(benchmark.retries_recovered, 1)


class TestBenchmarkThroughputUnderSaturatedWorkers(unittest.TestCase):
    """Benchmark throughput and latency verification under saturated worker configurations."""

    def test_telemetry_benchmark_scaling_and_saturation(self):
        """Verify benchmark counters accurately scale across worker saturation levels."""
        for workers in [1, 4, 16, 32, 64]:
            bench = TelemetryBenchmark(total_repos=50, workers=workers)
            bench.record_stage("discovery", 0.05)
            bench.record_stage("inspection", 0.20)
            bench.record_network_latency(0.12)
            bench.record_disk_latency(0.08)
            bench.record_retry(recovered=True)
            bench.record_failure("index_locked")
            bench.stop()

            d = bench.to_dict()
            self.assertEqual(d["total_repos"], 50)
            self.assertEqual(d["concurrency"]["configured_workers"], workers)
            self.assertEqual(d["stages"]["discovery"], 0.05)
            self.assertEqual(d["stages"]["inspection"], 0.20)
            self.assertEqual(d["latencies"]["network_seconds"], 0.12)
            self.assertEqual(d["latencies"]["disk_seconds"], 0.08)
            self.assertEqual(d["failures"]["total"], 1)
            self.assertEqual(d["failures"]["by_type"]["index_locked"], 1)
            self.assertEqual(d["retries"]["recovered"], 1)
            self.assertGreater(d["repos_per_second"], 0.0)

            summary = bench.summary_line()
            self.assertIn(f"{workers} workers", summary)
            self.assertIn("repos/s", summary)


if __name__ == "__main__":
    unittest.main()
