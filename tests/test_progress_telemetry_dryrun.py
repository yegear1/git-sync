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
    BulkActionItem,
    BulkCache,
    DEFAULT_ROOT,
    DEFAULT_WORKERS,
    ProgressIndicator,
    TelemetryBenchmark,
    check_repo_dirty,
    format_bulk_action_preview,
    inspect_repo,
    prompt_bulk_confirmation,
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
            "confirm": True,
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
        self.assertIn("Confirm (bulk):      True", text_out)


class TestBulkActionPreviewAndConfirmation(unittest.TestCase):
    """Unit tests for BulkActionItem, preview formatting, and interactive prompt mechanics."""

    def test_bulk_action_item_model_and_dict(self):
        item = BulkActionItem(
            repo="/path/to/repo",
            action="PULL (ff-only)",
            repo_rel="my-repo",
            branch="main",
            ahead=0,
            behind=3,
            is_dirty=True,
            dirty_files_count=2,
            is_protected=True,
            submodules_count=1,
            warnings=["Working tree dirty"],
            details={"custom": 123},
        )
        data = item.to_dict()
        self.assertEqual(data["repo"], "/path/to/repo")
        self.assertEqual(data["repo_rel"], "my-repo")
        self.assertEqual(data["action"], "PULL (ff-only)")
        self.assertEqual(data["behind"], 3)
        self.assertTrue(data["is_dirty"])
        self.assertTrue(data["is_protected"])
        self.assertEqual(data["warnings"], ["Working tree dirty"])
        self.assertEqual(data["details"]["custom"], 123)

    def test_format_bulk_action_preview_empty(self):
        rendered = format_bulk_action_preview([], title="TEST PREVIEW", color=False)
        self.assertIn("TEST PREVIEW", rendered)
        self.assertIn("(Nenhum repositório pendente de mutação)", rendered)

    def test_format_bulk_action_preview_rendering_and_safety_summary(self):
        items = [
            BulkActionItem(
                repo="/repos/repo-a",
                repo_rel="repo-a",
                action="PULL (ff-only)",
                branch="main",
                behind=4,
                is_dirty=True,
                dirty_files_count=3,
                is_protected=True,
            ),
            BulkActionItem(
                repo="/repos/repo-b",
                repo_rel="repo-b",
                action="PUSH",
                branch="feat/test",
                ahead=2,
            ),
        ]
        rendered = format_bulk_action_preview(items, title="TEST AUDIT", color=False)
        self.assertIn("TEST AUDIT", rendered)
        self.assertIn("Repositórios alvo (2):", rendered)
        self.assertIn("repo-a [main] ↳ PULL (ff-only) (4 commit(s) atrás)", rendered)
        self.assertIn("[Árvore suja: 3 arquivo(s)]", rendered)
        self.assertIn("[Branch protegida: main]", rendered)
        self.assertIn("repo-b [feat/test] ↳ PUSH (2 commit(s) à frente)", rendered)
        self.assertIn("Resumo de Segurança:", rendered)
        self.assertIn("Total de repositórios a modificar: 2", rendered)
        self.assertIn("Repositórios com autostash necessário: 1", rendered)
        self.assertIn("Branches protegidas afetadas: 1", rendered)

    def test_prompt_bulk_confirmation_accepts_yes(self):
        stream = io.StringIO()
        items = [BulkActionItem(repo="/r1", action="PULL", repo_rel="r1")]
        # User answers 'y'
        res = prompt_bulk_confirmation(items, stream=stream, input_func=lambda _: "y")
        self.assertTrue(res)

        # User answers 'sim'
        res_sim = prompt_bulk_confirmation(items, stream=stream, input_func=lambda _: "sim")
        self.assertTrue(res_sim)

    def test_prompt_bulk_confirmation_denied_or_abort(self):
        stream = io.StringIO()
        items = [BulkActionItem(repo="/r1", action="PULL", repo_rel="r1")]

        # User answers 'n'
        res_no = prompt_bulk_confirmation(items, stream=stream, input_func=lambda _: "n")
        self.assertFalse(res_no)
        self.assertIn("Operação cancelada pelo usuário.", stream.getvalue())

        # User presses Ctrl+C or EOF
        def raise_eof(_):
            raise EOFError()

        res_eof = prompt_bulk_confirmation(items, stream=stream, input_func=raise_eof)
        self.assertFalse(res_eof)
        self.assertIn("Operação abortada pelo usuário.", stream.getvalue())

    def test_prompt_bulk_confirmation_auto_yes(self):
        items = [BulkActionItem(repo="/r1", action="PULL", repo_rel="r1")]
        # Should return True without calling input_func
        called = []
        res = prompt_bulk_confirmation(items, input_func=lambda _: called.append(True), auto_yes=True)
        self.assertTrue(res)
        self.assertEqual(len(called), 0)


class TestCliBulkActionPreviewAndInteractiveConfirmation(unittest.TestCase):
    """Integration tests for CLI --preview and --confirm flags."""

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

    def _setup_repos_with_remote(self):
        remote_dir = self._init_bare_remote(self.root / "remote.git")
        seed_dir = self._init_repo(self.root / "seed")
        (seed_dir / "file.txt").write_text("v1\n")
        run_git(str(seed_dir), ["add", "file.txt"])
        run_git(str(seed_dir), ["commit", "-m", "init"])
        run_git(str(seed_dir), ["remote", "add", "origin", str(remote_dir)])
        run_git(str(seed_dir), ["push", "-u", "origin", "main"])

        local_dir = self.root / "repo1"
        run_git(str(self.root), ["clone", str(remote_dir), "repo1"])
        run_git(str(local_dir), ["config", "user.email", "test@example.com"])
        run_git(str(local_dir), ["config", "user.name", "Test User"])

        # Create upstream commit
        (seed_dir / "file.txt").write_text("v2\n")
        run_git(str(seed_dir), ["commit", "-am", "v2"])
        run_git(str(seed_dir), ["push", "origin", "main"])

        return remote_dir, seed_dir, local_dir

    def test_cli_pull_preview_mode_text_and_json(self):
        remote_dir, seed_dir, local_dir = self._setup_repos_with_remote()
        # Fetch remote updates
        run_git(str(local_dir), ["fetch"])

        # 1. Text preview mode
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main(["pull", "repo1", "--root", str(self.root), "--preview"])
        self.assertEqual(exit_code, 0)
        out = stdout_capture.getvalue()
        self.assertIn("PRÉVIA DE PULL EM LOTE", out)
        self.assertIn("repo1 [main] ↳ PULL (ff-only)", out)

        # INVARIANT: HEAD MUST NOT change during preview mode
        head_commit = run_git(str(local_dir), ["rev-parse", "HEAD"]).stdout.strip()
        seed_head = run_git(str(seed_dir), ["rev-parse", "HEAD"]).stdout.strip()
        self.assertNotEqual(head_commit, seed_head)

        # 2. JSON preview mode
        stdout_json = io.StringIO()
        with patch("sys.stdout", stdout_json):
            exit_code = main(["pull", "repo1", "--root", str(self.root), "--preview", "--json"])
        self.assertEqual(exit_code, 0)
        parsed = json.loads(stdout_json.getvalue())
        self.assertTrue(parsed["preview"])
        self.assertEqual(parsed["action"], "pull")
        self.assertEqual(len(parsed["items"]), 1)
        self.assertEqual(parsed["items"][0]["repo_rel"], "repo1")
        self.assertEqual(parsed["items"][0]["behind"], 1)

    def test_cli_pull_confirm_interactive_denied_aborts(self):
        remote_dir, seed_dir, local_dir = self._setup_repos_with_remote()
        run_git(str(local_dir), ["fetch"])
        head_before = run_git(str(local_dir), ["rev-parse", "HEAD"]).stdout.strip()

        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            with patch("builtins.input", return_value="n"):
                exit_code = main(["pull", "repo1", "--root", str(self.root), "--confirm"])

        self.assertEqual(exit_code, 0)
        out = stdout_capture.getvalue()
        self.assertIn("PRÉVIA DE PULL EM LOTE", out)
        self.assertIn("Operação cancelada pelo usuário.", out)

        # INVARIANT: Repository MUST NOT be modified when confirmation is denied
        head_after = run_git(str(local_dir), ["rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(head_before, head_after)

    def test_cli_pull_confirm_interactive_accepted_executes_mutation(self):
        remote_dir, seed_dir, local_dir = self._setup_repos_with_remote()
        run_git(str(local_dir), ["fetch"])
        seed_head = run_git(str(seed_dir), ["rev-parse", "HEAD"]).stdout.strip()

        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            with patch("builtins.input", return_value="yes"):
                exit_code = main(["pull", "repo1", "--root", str(self.root), "--confirm"])

        self.assertEqual(exit_code, 0)
        out = stdout_capture.getvalue()
        self.assertIn("PRÉVIA DE PULL EM LOTE", out)
        self.assertIn("✅ [repo1]", out)

        # INVARIANT: Mutation MUST be applied upon confirmation
        head_after = run_git(str(local_dir), ["rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(head_after, seed_head)

    def test_cli_pull_confirm_yes_flag_bypasses_prompt(self):
        remote_dir, seed_dir, local_dir = self._setup_repos_with_remote()
        run_git(str(local_dir), ["fetch"])
        seed_head = run_git(str(seed_dir), ["rev-parse", "HEAD"]).stdout.strip()

        input_mock = MagicMock(side_effect=Exception("input should not be called"))
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            with patch("builtins.input", input_mock):
                exit_code = main(["pull", "repo1", "--root", str(self.root), "--confirm", "-y"])

        self.assertEqual(exit_code, 0)
        head_after = run_git(str(local_dir), ["rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(head_after, seed_head)
        self.assertEqual(input_mock.call_count, 0)

    def test_cli_pull_confirm_json_without_yes_returns_error(self):
        remote_dir, seed_dir, local_dir = self._setup_repos_with_remote()
        run_git(str(local_dir), ["fetch"])

        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main(["pull", "repo1", "--root", str(self.root), "--confirm", "--json"])

        self.assertEqual(exit_code, 1)
        parsed = json.loads(stdout_capture.getvalue())
        self.assertIn("Interactive confirmation cannot be prompted in JSON mode", parsed["error"])

    def test_cli_push_preview_and_confirm(self):
        remote_dir = self._init_bare_remote(self.root / "origin.git")
        local_dir = self.root / "push_repo"
        run_git(str(self.root), ["clone", str(remote_dir), "push_repo"])
        run_git(str(local_dir), ["config", "user.email", "test@example.com"])
        run_git(str(local_dir), ["config", "user.name", "Test User"])

        # Add local commit
        (local_dir / "new.txt").write_text("push me\n")
        run_git(str(local_dir), ["add", "new.txt"])
        run_git(str(local_dir), ["commit", "-m", "local commit"])

        # 1. Preview push
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main(["push", "push_repo", "--root", str(self.root), "--preview"])
        self.assertEqual(exit_code, 0)
        out = stdout_capture.getvalue()
        self.assertIn("PRÉVIA DE ENVIO EM LOTE", out)
        self.assertIn("push_repo [main] ↳ PUSH", out)

        # 2. Confirm push denied
        stdout_denied = io.StringIO()
        with patch("sys.stdout", stdout_denied):
            with patch("builtins.input", return_value="n"):
                exit_code = main(["push", "push_repo", "--root", str(self.root), "--confirm"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Operação cancelada pelo usuário.", stdout_denied.getvalue())


if __name__ == "__main__":
    unittest.main()
