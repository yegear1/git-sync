"""
Unit tests for dynamic worker auto-tuning, adaptive concurrency control,
and latency threshold telemetry alerts.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import _parse_workers_arg, main
from git_sync.config import GitSyncConfig, resolve_config
from git_sync.core import (
    DEFAULT_DISK_LATENCY_THRESHOLD,
    DEFAULT_NETWORK_LATENCY_THRESHOLD,
    DEFAULT_WORKERS,
    AdaptiveConcurrencyController,
    BulkCache,
    TelemetryBenchmark,
    calculate_optimal_workers,
    find_diverged_repos,
    find_push_pending_repos,
    run_git,
)


class TestAdaptiveConcurrencyController(unittest.TestCase):
    """Test dynamic concurrency auto-tuning based on core count and repo count."""

    def test_calculate_optimal_workers_zero_or_negative(self):
        self.assertEqual(calculate_optimal_workers(0), 1)
        self.assertEqual(calculate_optimal_workers(-5), 1)

    def test_calculate_optimal_workers_core_scaling(self):
        # 4 cores, network I/O (is_fetch=True): min(4 * 3, 32) = 12
        self.assertEqual(calculate_optimal_workers(repo_count=50, is_fetch=True, cpu_count=4), 12)
        # 4 cores, disk I/O (is_fetch=False): min(4 * 2, 16, 32) = 8
        self.assertEqual(calculate_optimal_workers(repo_count=50, is_fetch=False, cpu_count=4), 8)

        # 8 cores, network I/O: min(8 * 3, 32) = 24
        self.assertEqual(calculate_optimal_workers(repo_count=50, is_fetch=True, cpu_count=8), 24)
        # 8 cores, disk I/O: min(8 * 2, 16, 32) = 16
        self.assertEqual(calculate_optimal_workers(repo_count=50, is_fetch=False, cpu_count=8), 16)

    def test_calculate_optimal_workers_clamped_to_repo_count(self):
        # Even with 16 cores, 3 repos should only spawn 3 workers
        self.assertEqual(calculate_optimal_workers(repo_count=3, is_fetch=True, cpu_count=16), 3)
        self.assertEqual(calculate_optimal_workers(repo_count=1, is_fetch=False, cpu_count=16), 1)

    def test_controller_determine_workers_explicit_override(self):
        ctrl = AdaptiveConcurrencyController(cpu_count=8)
        # Explicit worker override of 6 across 20 repos -> 6
        self.assertEqual(ctrl.determine_workers(repo_count=20, configured_workers=6), 6)
        # Explicit worker override of 16 across 3 repos -> clamped to 3
        self.assertEqual(ctrl.determine_workers(repo_count=3, configured_workers=16), 3)

    def test_controller_determine_workers_auto_tuning(self):
        ctrl = AdaptiveConcurrencyController(cpu_count=4)
        # When configured_workers is "auto" or None, auto-tunes
        self.assertEqual(ctrl.determine_workers(repo_count=30, is_fetch=True, configured_workers="auto"), 12)
        self.assertEqual(ctrl.determine_workers(repo_count=30, is_fetch=False, configured_workers="auto"), 8)
        self.assertEqual(ctrl.determine_workers(repo_count=30, is_fetch=True, configured_workers=None), 12)

    def test_controller_tune_workers_for_latency_degradation(self):
        ctrl = AdaptiveConcurrencyController(
            cpu_count=8,
            disk_latency_threshold=0.20,
            network_latency_threshold=2.0,
        )

        # 1. Disk contention degrades concurrency
        initial_workers = 12
        tuned = ctrl.tune_workers_for_latency(
            current_workers=initial_workers,
            avg_disk_latency=0.35,  # > 0.20 threshold
            avg_network_latency=0.10,
            repo_count=30,
        )
        self.assertLess(tuned, initial_workers)
        self.assertEqual(tuned, 8)  # 12 - 12//3 = 8

        # 2. Network saturation degrades concurrency
        tuned_net = ctrl.tune_workers_for_latency(
            current_workers=initial_workers,
            avg_disk_latency=0.05,
            avg_network_latency=3.2,  # > 2.0 threshold
            repo_count=30,
        )
        self.assertLess(tuned_net, initial_workers)
        self.assertEqual(tuned_net, 8)

        # 3. Minimum floor preservation
        tuned_floor = ctrl.tune_workers_for_latency(
            current_workers=1,
            avg_disk_latency=1.5,
            avg_network_latency=5.0,
            repo_count=10,
        )
        self.assertEqual(tuned_floor, 1)


class TestLatencyThresholdTelemetry(unittest.TestCase):
    """Test telemetry latency threshold evaluation and alert generation."""

    def test_healthy_telemetry_no_alerts(self):
        bench = TelemetryBenchmark(workers=8, total_repos=20)
        bench.record_stage("inspection", 0.30)
        bench.record_disk_latency(0.20)  # avg 0.01s << 0.20s threshold
        bench.record_network_latency(1.0)  # avg 0.05s << 2.0s threshold

        alerts = bench.evaluate_thresholds()
        self.assertEqual(len(alerts), 0)
        self.assertEqual(len(bench.alerts), 0)

        data = bench.to_dict()
        self.assertEqual(len(data["alerts"]), 0)
        self.assertIn("avg_disk_seconds", data["latencies"])
        self.assertIn("avg_network_seconds", data["latencies"])
        self.assertEqual(data["latencies"]["disk_threshold_seconds"], DEFAULT_DISK_LATENCY_THRESHOLD)
        self.assertEqual(data["latencies"]["network_threshold_seconds"], DEFAULT_NETWORK_LATENCY_THRESHOLD)

    def test_disk_contention_alert_generation(self):
        bench = TelemetryBenchmark(workers=12, total_repos=10)
        # Total disk latency of 3.5s across 10 repos -> avg 0.35s > 0.20s
        bench.record_disk_latency(3.5)
        bench.record_network_latency(0.5)

        ctrl = AdaptiveConcurrencyController(disk_latency_threshold=0.20)
        alerts = bench.evaluate_thresholds(ctrl)

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["category"], "DISK_CONTENTION")
        self.assertEqual(alert["severity"], "WARNING")
        self.assertEqual(alert["threshold_seconds"], 0.20)
        self.assertEqual(alert["observed_seconds"], 0.35)
        self.assertIn("I/O contention", alert["message"])
        self.assertEqual(alert["recommended_workers"], 6)

        # Summary line formats alert indicator
        summary = bench.summary_line()
        self.assertIn("⚠️ Alerts: DISK_CONTENTION", summary)

    def test_network_saturation_alert_generation(self):
        bench = TelemetryBenchmark(workers=16, total_repos=5)
        # Total network latency of 15.0s across 5 repos -> avg 3.0s > 2.0s
        bench.record_disk_latency(0.1)
        bench.record_network_latency(15.0)

        ctrl = AdaptiveConcurrencyController(network_latency_threshold=2.0)
        alerts = bench.evaluate_thresholds(ctrl)

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["category"], "NETWORK_SATURATION")
        self.assertEqual(alert["threshold_seconds"], 2.0)
        self.assertEqual(alert["observed_seconds"], 3.0)
        self.assertIn("network saturation", alert["message"])

        summary = bench.summary_line()
        self.assertIn("⚠️ Alerts: NETWORK_SATURATION", summary)

    def test_worker_saturation_thread_contention_alert(self):
        # 32 workers on a 4-core machine (32 > 4 * 4 = 16) with elevated disk latency
        ctrl = AdaptiveConcurrencyController(cpu_count=4)
        bench = TelemetryBenchmark(workers=32, total_repos=20)
        bench.active_workers = 32
        bench.record_disk_latency(2.0)  # avg 0.10s > 0.08s worker saturation trigger

        alerts = ctrl.evaluate_telemetry(bench)
        categories = [a["category"] for a in alerts]
        self.assertIn("WORKER_SATURATION", categories)
        worker_alert = next(a for a in alerts if a["category"] == "WORKER_SATURATION")
        self.assertIn("exceeds host core capacity", worker_alert["message"])
        self.assertEqual(worker_alert["recommended_workers"], 8)  # 4 cores * 2 = 8

    def test_telemetry_to_dict_concurrency_metadata(self):
        bench = TelemetryBenchmark(workers="auto", total_repos=15)
        bench.active_workers = 6
        bench.optimal_workers = 6
        bench.auto_tuned = True

        data = bench.to_dict()
        concurrency = data["concurrency"]
        self.assertEqual(concurrency["configured_workers"], "auto")
        self.assertEqual(concurrency["active_workers"], 6)
        self.assertEqual(concurrency["optimal_workers"], 6)
        self.assertTrue(concurrency["auto_tuned"])

        summary = bench.summary_line()
        self.assertIn("(auto-tuned)", summary)


class TestCLIAndConfigAutoTuning(unittest.TestCase):
    """Test CLI options --workers auto, --auto-tune, and workspace profile configuration."""

    def test_parse_workers_arg_valid(self):
        self.assertEqual(_parse_workers_arg("auto"), "auto")
        self.assertEqual(_parse_workers_arg("AUTO"), "auto")
        self.assertEqual(_parse_workers_arg("8"), 8)
        self.assertEqual(_parse_workers_arg(" 16 "), 16)

    def test_parse_workers_arg_invalid(self):
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_workers_arg("0")
        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_workers_arg("-4")
        with self.assertRaises(argparse.ArgumentTypeError):
            _parse_workers_arg("invalid")

    def test_config_loader_auto_workers(self):
        cfg = resolve_config({"workers": "auto"})
        self.assertEqual(cfg.workers, "auto")
        self.assertTrue(cfg.auto_tune)

        cfg_dict = cfg.to_dict()
        self.assertEqual(cfg_dict["workers"], "auto")
        self.assertTrue(cfg_dict["auto_tune"])

    def test_config_loader_explicit_workers(self):
        cfg = resolve_config({"workers": 6, "auto_tune": False})
        self.assertEqual(cfg.workers, 6)
        self.assertFalse(cfg.auto_tune)

    def test_find_push_pending_and_diverged_auto_workers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "test-repo"
            repo.mkdir()
            run_git(str(repo), ["init", "-b", "main"])
            run_git(str(repo), ["config", "user.email", "test@example.com"])
            run_git(str(repo), ["config", "user.name", "Test User"])
            (repo / "f.txt").write_text("ok")
            run_git(str(repo), ["add", "f.txt"])
            run_git(str(repo), ["commit", "-m", "init"])

            # Call with workers="auto"
            pending = find_push_pending_repos([str(repo)], str(root), do_fetch=False, workers="auto")
            self.assertEqual(len(pending), 0)

            diverged = find_diverged_repos([str(repo)], str(root), do_fetch=False, workers="auto")
            self.assertEqual(len(diverged), 0)

    def test_cli_status_with_auto_tune_and_json_benchmark(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo-a"
            repo.mkdir()
            run_git(str(repo), ["init", "-b", "main"])
            run_git(str(repo), ["config", "user.email", "test@example.com"])
            run_git(str(repo), ["config", "user.name", "Test User"])
            (repo / "f.txt").write_text("hello")
            run_git(str(repo), ["add", "f.txt"])
            run_git(str(repo), ["commit", "-m", "first"])

            out = io.StringIO()
            with redirect_stdout(out):
                exit_code = main(["status", "--root", str(root), "--no-fetch", "--workers", "auto", "--benchmark", "--json"])

            self.assertEqual(exit_code, 0)
            data = json.loads(out.getvalue())
            self.assertIn("telemetry", data)
            telemetry = data["telemetry"]
            self.assertEqual(telemetry["concurrency"]["configured_workers"], "auto")
            self.assertTrue(telemetry["concurrency"]["auto_tuned"])
            self.assertGreaterEqual(telemetry["concurrency"]["active_workers"], 1)
            self.assertIn("alerts", telemetry)
            self.assertIsInstance(telemetry["alerts"], list)

    def test_cli_config_displays_auto_tuned_workers(self):
        out = io.StringIO()
        with redirect_stdout(out):
            exit_code = main(["config", "--workers", "auto"])
        self.assertEqual(exit_code, 0)
        output = out.getvalue()
        self.assertIn("Workers:             auto (auto-tuned)", output)
        self.assertIn("Auto-tune:           True", output)


if __name__ == "__main__":
    unittest.main()
