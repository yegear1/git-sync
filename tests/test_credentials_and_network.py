"""
Unit tests for Git credential sanity diagnostics, network error classification,
and transient timeout recovery mechanics.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.config import GitSyncConfig, resolve_config
from git_sync.core import (
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_MAX_BACKOFF,
    DEFAULT_RETRIES,
    GRADE_A_THRESHOLD_SECONDS,
    GRADE_B_THRESHOLD_SECONDS,
    GRADE_C_THRESHOLD_SECONDS,
    TelemetryBenchmark,
    audit_ssh_multiplexing,
    benchmark_ssh_multiplexing,
    check_credentials_sanity,
    close_ssh_control_socket,
    compute_latency_grade,
    diagnose_git_error,
    find_ssh_control_sockets,
    inspect_github_cli,
    inspect_repo,
    inspect_ssh_config,
    inspect_ssh_control_socket,
    is_transient_network_error,
    probe_remote_reachability,
    prune_stale_ssh_control_sockets,
    run_git_with_retry,
)


class TestCredentialDiagnostics(unittest.TestCase):
    """Test Git error classification and actionable diagnostic suggestions."""

    def test_diagnose_ssh_permission_denied(self):
        err = "git@github.com: Permission denied (publickey)."
        diag = diagnose_git_error(err, "git@github.com:ye-sandbox/git-sync.git")
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "SSH_PERMISSION_DENIED")
        self.assertEqual(diag["action_required"], "check_ssh_keys")
        self.assertIn("ssh-add", diag["suggestion"])

    def test_diagnose_ssh_no_agent(self):
        err = "Could not open a connection to your authentication agent."
        diag = diagnose_git_error(err)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "SSH_NO_AGENT")
        self.assertEqual(diag["action_required"], "start_ssh_agent")
        self.assertIn("eval $(ssh-agent -s)", diag["suggestion"])

    def test_diagnose_ssh_agent_refused(self):
        err = "sign_and_send_pubkey: signing failed: agent refused operation"
        diag = diagnose_git_error(err)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "SSH_AGENT_REFUSED")
        self.assertEqual(diag["action_required"], "reload_ssh_agent")

    def test_diagnose_ssh_host_unverified(self):
        err = "Host key verification failed."
        diag = diagnose_git_error(err, "git@github.com:org/repo.git")
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "SSH_HOST_UNVERIFIED")
        self.assertEqual(diag["action_required"], "verify_ssh_host")
        self.assertIn("ssh-keyscan", diag["suggestion"])
        self.assertIn("github.com", diag["suggestion"])

    def test_diagnose_http_auth_required(self):
        err = "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
        diag = diagnose_git_error(err)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "HTTP_AUTH_REQUIRED")
        self.assertEqual(diag["action_required"], "configure_http_credentials")
        self.assertIn("credential.helper", diag["suggestion"])

    def test_diagnose_http_auth_failed(self):
        err = "fatal: Authentication failed for 'https://github.com/org/repo.git'"
        diag = diagnose_git_error(err)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "HTTP_AUTH_FAILED")
        self.assertEqual(diag["action_required"], "update_http_credentials")

    def test_diagnose_network_dns(self):
        err = "fatal: unable to access 'https://github.com/': Could not resolve host: github.com"
        diag = diagnose_git_error(err)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "NETWORK_DNS_FAILURE")
        self.assertEqual(diag["action_required"], "check_network")

    def test_diagnose_network_timeout(self):
        err = "fatal: unable to access 'https://github.com/': Connection timed out after 20000 milliseconds"
        diag = diagnose_git_error(err)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "NETWORK_TIMEOUT")
        self.assertEqual(diag["action_required"], "check_network_timeout")

    def test_diagnose_network_unreachable(self):
        err = "fatal: unable to connect to github.com: Network is unreachable"
        diag = diagnose_git_error(err)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "NETWORK_UNREACHABLE")
        self.assertEqual(diag["action_required"], "check_network")

    def test_diagnose_remote_not_found(self):
        err = "ERROR: Repository not found."
        diag = diagnose_git_error(err)
        self.assertIsNotNone(diag)
        self.assertEqual(diag["category"], "REMOTE_NOT_FOUND")
        self.assertEqual(diag["action_required"], "verify_remote_url")

    def test_diagnose_none_on_clean_or_empty(self):
        self.assertIsNone(diagnose_git_error(""))
        self.assertIsNone(diagnose_git_error("   "))
        self.assertIsNone(diagnose_git_error("Everything up-to-date"))


class TestTransientNetworkRetry(unittest.TestCase):
    """Test transient network error detection and exponential backoff retry mechanics."""

    def test_is_transient_network_error(self):
        self.assertTrue(is_transient_network_error(subprocess.TimeoutExpired(cmd="git", timeout=5), ""))
        self.assertTrue(is_transient_network_error(None, "Operation timed out"))
        self.assertTrue(is_transient_network_error(None, "Could not resolve host: github.com"))
        self.assertTrue(is_transient_network_error(None, "The remote end hung up unexpectedly"))
        self.assertTrue(is_transient_network_error(None, "Connection reset by peer"))
        self.assertTrue(is_transient_network_error(None, "RPC failed; curl 56 GnuTLS recv error"))

        # Permanent errors MUST NOT be treated as transient
        self.assertFalse(is_transient_network_error(None, "Permission denied (publickey)."))
        self.assertFalse(is_transient_network_error(None, "terminal prompts disabled"))
        self.assertFalse(is_transient_network_error(None, "Authentication failed for 'https://github.com'"))
        self.assertFalse(is_transient_network_error(None, "Repository not found."))
        self.assertFalse(is_transient_network_error(None, ""))

    @patch("git_sync.core.run_git")
    def test_run_git_with_retry_success_first_try(self, mock_run_git):
        mock_proc = subprocess.CompletedProcess(args=["git", "fetch"], returncode=0, stdout="ok", stderr="")
        mock_run_git.return_value = mock_proc

        slept = []
        bench = TelemetryBenchmark()
        res = run_git_with_retry("/dummy/repo", ["fetch"], timeout=5, retries=2, benchmark=bench, sleep_func=slept.append)

        self.assertEqual(res.returncode, 0)
        self.assertEqual(mock_run_git.call_count, 1)
        self.assertEqual(len(slept), 0)
        self.assertEqual(bench.retries_count, 0)
        self.assertEqual(bench.retries_recovered, 0)

    @patch("git_sync.core.run_git")
    def test_run_git_with_retry_recovers_after_transient(self, mock_run_git):
        proc_fail = subprocess.CompletedProcess(
            args=["git", "fetch"], returncode=128, stdout="", stderr="fatal: Connection timed out"
        )
        proc_ok = subprocess.CompletedProcess(args=["git", "fetch"], returncode=0, stdout="ok", stderr="")
        mock_run_git.side_effect = [proc_fail, proc_ok]

        slept = []
        bench = TelemetryBenchmark()
        res = run_git_with_retry(
            "/dummy/repo",
            ["fetch"],
            timeout=5,
            retries=2,
            backoff_factor=0.5,
            benchmark=bench,
            sleep_func=slept.append,
        )

        self.assertEqual(res.returncode, 0)
        self.assertEqual(mock_run_git.call_count, 2)
        self.assertEqual(len(slept), 1)
        self.assertEqual(slept[0], 0.5)  # 0.5 * 2^0
        self.assertEqual(bench.retries_count, 2)  # 1 failure + 1 recovery recorded
        self.assertEqual(bench.retries_recovered, 1)

    @patch("git_sync.core.run_git")
    def test_run_git_with_retry_fails_fast_on_auth_error(self, mock_run_git):
        proc_auth_fail = subprocess.CompletedProcess(
            args=["git", "fetch"], returncode=128, stdout="", stderr="Permission denied (publickey)."
        )
        mock_run_git.return_value = proc_auth_fail

        slept = []
        bench = TelemetryBenchmark()
        res = run_git_with_retry(
            "/dummy/repo",
            ["fetch"],
            timeout=5,
            retries=2,
            benchmark=bench,
            sleep_func=slept.append,
        )

        self.assertEqual(res.returncode, 128)
        self.assertEqual(mock_run_git.call_count, 1)  # No retry on auth errors
        self.assertEqual(len(slept), 0)
        self.assertEqual(bench.retries_count, 0)

    @patch("git_sync.core.run_git")
    def test_run_git_with_retry_timeout_expired_exhausts(self, mock_run_git):
        mock_run_git.side_effect = subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=2)

        slept = []
        bench = TelemetryBenchmark()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_git_with_retry(
                "/dummy/repo",
                ["fetch"],
                timeout=2,
                retries=2,
                backoff_factor=0.25,
                benchmark=bench,
                sleep_func=slept.append,
            )

        self.assertEqual(mock_run_git.call_count, 3)  # Initial + 2 retries
        self.assertEqual(slept, [0.25, 0.5])
        self.assertEqual(bench.retries_count, 2)
        self.assertEqual(bench.retries_recovered, 0)


class TestCredentialSanityChecks(unittest.TestCase):
    """Test proactive pre-flight sanity checks for SSH agent, keys, and remote probing."""

    @patch("git_sync.core.get_repo_remotes")
    @patch("git_sync.core.discover_repos")
    @patch("subprocess.run")
    def test_check_credentials_sanity_healthy_agent(self, mock_run, mock_discover, mock_remotes):
        mock_discover.return_value = ["/dummy/repo1"]
        mock_remotes.return_value = {"origin": "git@github.com:org/repo1.git"}

        def side_effect(cmd, **kwargs):
            if cmd[0] == "ssh-add":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="3072 SHA256:abc /home/user/.ssh/id_ed25519 (ED25519)\n", stderr=""
                )
            elif cmd[0] == "git" and "credential.helper" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="cache\n", stderr="")
            elif cmd[0] == "git" and "ls-remote" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="hash\trefs/heads/main\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        with patch.dict(os.environ, {"SSH_AUTH_SOCK": "/run/user/1000/ssh.socket"}):
            sanity = check_credentials_sanity("/dummy/root", timeout=5, test_remotes=True)

        self.assertTrue(sanity["ok"])
        self.assertEqual(sanity["ssh_agent"]["status"], "ACTIVE")
        self.assertEqual(sanity["ssh_agent"]["identities_count"], 1)
        self.assertEqual(sanity["credential_helpers"], ["cache"])
        self.assertIn("git@github.com:org/repo1.git", sanity["remotes"])
        self.assertTrue(sanity["remotes"]["git@github.com:org/repo1.git"]["ok"])
        self.assertEqual(len(sanity["errors"]), 0)

    @patch("git_sync.core.get_repo_remotes")
    @patch("git_sync.core.discover_repos")
    @patch("subprocess.run")
    def test_check_credentials_sanity_unauthenticated_remote(self, mock_run, mock_discover, mock_remotes):
        mock_discover.return_value = ["/dummy/repo1"]
        mock_remotes.return_value = {"origin": "git@github.com:org/repo1.git"}

        def side_effect(cmd, **kwargs):
            if cmd[0] == "ssh-add":
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="The agent has no identities.")
            elif cmd[0] == "git" and "credential.helper" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
            elif cmd[0] == "git" and "ls-remote" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=128, stdout="", stderr="git@github.com: Permission denied (publickey)."
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        with patch.dict(os.environ, {"SSH_AUTH_SOCK": "/run/user/1000/ssh.socket"}, clear=False):
            sanity = check_credentials_sanity("/dummy/root", timeout=5, test_remotes=True)

        self.assertFalse(sanity["ok"])
        self.assertEqual(sanity["ssh_agent"]["status"], "NO_IDENTITIES")
        self.assertGreater(len(sanity["errors"]), 0)
        self.assertGreater(len(sanity["suggestions"]), 0)
        self.assertIn("git@github.com:org/repo1.git", sanity["remotes"])
        self.assertFalse(sanity["remotes"]["git@github.com:org/repo1.git"]["ok"])
        self.assertEqual(
            sanity["remotes"]["git@github.com:org/repo1.git"]["diagnostic"]["category"],
            "SSH_PERMISSION_DENIED",
        )


class TestCliDoctorIntegration(unittest.TestCase):
    """Test CLI doctor subcommand and configuration options."""

    @patch("git_sync.cli.check_credentials_sanity")
    def test_cli_doctor_json_healthy(self, mock_sanity):
        mock_sanity.return_value = {
            "ok": True,
            "ssh_agent": {"status": "ACTIVE", "socket": "/sock", "identities_count": 1, "identities": ["key1"]},
            "ssh_keys": ["id_ed25519"],
            "credential_helpers": ["store"],
            "has_token_env": True,
            "remotes": {"git@github.com:org/repo.git": {"ok": True, "protocol": "ssh", "host": "github.com", "error": None, "diagnostic": None}},
            "warnings": [],
            "errors": [],
            "suggestions": [],
        }

        with patch("sys.stdout") as mock_stdout:
            exit_code = main(["doctor", "--json"])
            self.assertEqual(exit_code, 0)
            mock_stdout.write.assert_called()

    @patch("git_sync.cli.check_credentials_sanity")
    def test_cli_doctor_json_unhealthy(self, mock_sanity):
        mock_sanity.return_value = {
            "ok": False,
            "ssh_agent": {"status": "NO_AGENT", "socket": None, "identities_count": 0, "identities": []},
            "ssh_keys": [],
            "credential_helpers": [],
            "has_token_env": False,
            "remotes": {},
            "warnings": ["No SSH agent"],
            "errors": ["SSH authentication failed"],
            "suggestions": ["Run ssh-agent"],
        }

        with patch("sys.stdout"):
            exit_code = main(["doctor", "--json"])
            self.assertEqual(exit_code, 1)

    def test_config_retries_and_preflight(self):
        cfg = resolve_config({
            "root": "/tmp/test",
            "retries": 4,
            "preflight": True,
        })
        self.assertEqual(cfg.retries, 4)
        self.assertTrue(cfg.preflight)
        d = cfg.to_dict()
        self.assertEqual(d["retries"], 4)
        self.assertTrue(d["preflight"])


class TestSshMultiplexingDiagnostics(unittest.TestCase):
    """Test SSH ControlMaster configuration inspection, latency audit, and fallback mechanics."""

    def test_inspect_ssh_config_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_config = os.path.join(tmp_dir, "nonexistent_config")
            info = inspect_ssh_config(config_path=fake_config, host="github.com", query_ssh_g=False)
            self.assertFalse(info["exists"])
            self.assertFalse(info["is_enabled"])
            self.assertIsNone(info["control_master"])
            self.assertIsNotNone(info["recommendation"])
            self.assertIn("ControlMaster auto", info["recommendation"])

    def test_inspect_ssh_config_enabled_with_valid_socket_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = os.path.join(tmp_dir, "config")
            sock_dir = os.path.join(tmp_dir, "sockets")
            os.makedirs(sock_dir, exist_ok=True)
            content = f"""
Host github.com
    ControlMaster auto
    ControlPath {sock_dir}/%C
    ControlPersist 10m
"""
            with open(config_file, "w") as f:
                f.write(content)

            info = inspect_ssh_config(config_path=config_file, host="github.com", query_ssh_g=False)
            self.assertTrue(info["exists"])
            self.assertTrue(info["is_enabled"])
            self.assertEqual(info["control_master"], "auto")
            self.assertEqual(info["control_persist"], "10m")
            self.assertEqual(info["socket_dir"], sock_dir)
            self.assertTrue(info["socket_dir_exists"])
            self.assertIsNone(info["recommendation"])

    def test_inspect_ssh_config_missing_socket_dir_warning(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = os.path.join(tmp_dir, "config")
            missing_sock_dir = os.path.join(tmp_dir, "nonexistent_sockets")
            content = f"""
Host *
    ControlMaster yes
    ControlPath {missing_sock_dir}/%C
"""
            with open(config_file, "w") as f:
                f.write(content)

            info = inspect_ssh_config(config_path=config_file, host="github.com", query_ssh_g=False)
            self.assertTrue(info["is_enabled"])
            self.assertFalse(info["socket_dir_exists"])
            self.assertIsNotNone(info["recommendation"])
            self.assertIn("does not exist", info["recommendation"])

    def test_inspect_ssh_config_first_match_wins_precedence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = os.path.join(tmp_dir, "config")
            content = """
Host github.com
    ControlMaster auto
    ControlPath /tmp/cm-%C
    ControlPersist 60s

Host *
    ControlMaster no
"""
            with open(config_file, "w") as f:
                f.write(content)

            info_gh = inspect_ssh_config(config_path=config_file, host="github.com", query_ssh_g=False)
            self.assertTrue(info_gh["is_enabled"])
            self.assertEqual(info_gh["control_master"], "auto")

            info_gl = inspect_ssh_config(config_path=config_file, host="gitlab.com", query_ssh_g=False)
            self.assertFalse(info_gl["is_enabled"])
            self.assertEqual(info_gl["control_master"], "no")

    def test_benchmark_ssh_multiplexing_non_ssh_url(self):
        res = benchmark_ssh_multiplexing(remote_url="https://github.com/org/repo.git", host="github.com")
        self.assertFalse(res["supported"])
        self.assertIn("not an SSH URL", res["error"])

    @patch("subprocess.run")
    def test_benchmark_ssh_multiplexing_success(self, mock_run):
        mock_proc_ok = subprocess.CompletedProcess(
            args=["git", "ls-remote"], returncode=0, stdout="hash\trefs/heads/main\n", stderr=""
        )
        mock_run.return_value = mock_proc_ok

        res = benchmark_ssh_multiplexing(
            remote_url="git@github.com:org/repo.git",
            host="github.com",
            timeout=5,
        )

        self.assertTrue(res["supported"])
        self.assertIsNone(res["error"])
        self.assertIsNotNone(res["without_multiplexing_seconds"])
        self.assertIsNotNone(res["with_multiplexing_initial_seconds"])
        self.assertIsNotNone(res["with_multiplexing_reused_seconds"])
        self.assertIsNotNone(res["speedup_factor"])
        self.assertIsNotNone(res["savings_pct"])

    @patch("subprocess.run")
    def test_benchmark_ssh_multiplexing_auth_failure(self, mock_run):
        mock_proc_fail = subprocess.CompletedProcess(
            args=["git", "ls-remote"],
            returncode=128,
            stdout="",
            stderr="git@github.com: Permission denied (publickey).",
        )
        mock_run.return_value = mock_proc_fail

        res = benchmark_ssh_multiplexing(
            remote_url="git@github.com:org/repo.git",
            host="github.com",
            timeout=5,
        )

        self.assertFalse(res["supported"])
        self.assertIn("Permission denied", res["error"])
        self.assertIsNotNone(res["diagnostic"])
        self.assertEqual(res["diagnostic"]["category"], "SSH_PERMISSION_DENIED")

    @patch("subprocess.run")
    def test_benchmark_ssh_multiplexing_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git", "ls-remote"], timeout=5)

        res = benchmark_ssh_multiplexing(
            remote_url="git@github.com:org/repo.git",
            host="github.com",
            timeout=5,
        )

        self.assertFalse(res["supported"])
        self.assertIn("Timeout", res["error"])
        self.assertIsNotNone(res["diagnostic"])
        self.assertEqual(res["diagnostic"]["category"], "NETWORK_TIMEOUT")

    @patch("git_sync.core.get_repo_remotes")
    @patch("git_sync.core.discover_repos")
    @patch("git_sync.core.benchmark_ssh_multiplexing")
    def test_audit_ssh_multiplexing_end_to_end(self, mock_bench, mock_discover, mock_remotes):
        mock_discover.return_value = ["/dummy/repo"]
        mock_remotes.return_value = {"origin": "git@github.com:org/repo.git"}
        mock_bench.return_value = {
            "host": "github.com",
            "remote_url": "git@github.com:org/repo.git",
            "supported": True,
            "error": None,
            "diagnostic": None,
            "without_multiplexing_seconds": 0.45,
            "with_multiplexing_initial_seconds": 0.46,
            "with_multiplexing_reused_seconds": 0.05,
            "speedup_factor": 9.0,
            "savings_pct": 88.9,
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = os.path.join(tmp_dir, "config")
            with open(cfg_file, "w") as f:
                f.write("Host github.com\n    ControlMaster auto\n    ControlPath /tmp/%C\n")

            audit = audit_ssh_multiplexing(
                root_dir="/dummy/root",
                config_path=cfg_file,
                run_benchmark=True,
            )

            self.assertTrue(audit["ok"])
            self.assertEqual(audit["primary_host"], "github.com")
            self.assertEqual(audit["ssh_remotes_found"], 1)
            self.assertTrue(audit["benchmark"]["supported"])
            self.assertEqual(audit["benchmark"]["speedup_factor"], 9.0)

    @patch("git_sync.cli.check_credentials_sanity")
    @patch("git_sync.cli.audit_ssh_multiplexing")
    def test_cli_doctor_ssh_flag(self, mock_audit, mock_sanity):
        mock_sanity.return_value = {
            "ok": True,
            "ssh_agent": {"status": "ACTIVE", "socket": "/sock", "identities_count": 1, "identities": ["key1"]},
            "ssh_keys": ["id_ed25519"],
            "ssh_multiplexing": {"is_enabled": True, "control_master": "auto"},
            "credential_helpers": ["store"],
            "has_token_env": True,
            "remotes": {},
            "warnings": [],
            "errors": [],
            "suggestions": [],
        }
        mock_audit.return_value = {
            "ok": True,
            "config": {
                "config_path": "/home/user/.ssh/config",
                "exists": True,
                "host": "github.com",
                "control_master": "auto",
                "control_path": "~/.ssh/control-%C",
                "control_persist": "10m",
                "is_enabled": True,
                "socket_dir_exists": True,
            },
            "primary_host": "github.com",
            "benchmark": {
                "supported": True,
                "without_multiplexing_seconds": 0.50,
                "with_multiplexing_initial_seconds": 0.52,
                "with_multiplexing_reused_seconds": 0.05,
                "speedup_factor": 10.0,
                "savings_pct": 90.0,
            },
            "warnings": [],
            "suggestions": [],
        }

        with patch("sys.stdout"):
            exit_code = main(["doctor", "--ssh"])
            self.assertEqual(exit_code, 0)
            mock_audit.assert_called_once()

    @patch("git_sync.cli.check_credentials_sanity")
    @patch("git_sync.cli.audit_ssh_multiplexing")
    def test_cli_doctor_ssh_json_flag(self, mock_audit, mock_sanity):
        mock_sanity.return_value = {
            "ok": True,
            "ssh_agent": {"status": "ACTIVE", "socket": "/sock", "identities_count": 1, "identities": ["key1"]},
            "ssh_keys": ["id_ed25519"],
            "ssh_multiplexing": {"is_enabled": True, "control_master": "auto"},
            "credential_helpers": ["store"],
            "has_token_env": True,
            "remotes": {},
            "warnings": [],
            "errors": [],
            "suggestions": [],
        }
        mock_audit.return_value = {
            "ok": True,
            "config": {
                "config_path": "/home/user/.ssh/config",
                "exists": True,
                "host": "github.com",
                "control_master": "auto",
                "control_path": "~/.ssh/control-%C",
                "control_persist": "10m",
                "is_enabled": True,
                "socket_dir_exists": True,
            },
            "primary_host": "github.com",
            "benchmark": {
                "supported": True,
                "without_multiplexing_seconds": 0.50,
                "with_multiplexing_initial_seconds": 0.52,
                "with_multiplexing_reused_seconds": 0.05,
                "speedup_factor": 10.0,
                "savings_pct": 90.0,
            },
            "warnings": [],
            "suggestions": [],
        }

        captured_output = []
        with patch("sys.stdout.write", side_effect=captured_output.append):
            exit_code = main(["doctor", "--ssh", "--json"])
            self.assertEqual(exit_code, 0)
            mock_audit.assert_called_once()
            joined = "".join(captured_output)
            data = json.loads(joined)
            self.assertIn("ssh_audit", data)
            self.assertTrue(data["ssh_audit"]["ok"])
            self.assertEqual(data["ssh_audit"]["benchmark"]["speedup_factor"], 10.0)

    def test_inspect_ssh_control_socket_missing(self):
        info = inspect_ssh_control_socket("/tmp/nonexistent_socket_path_xyz")
        self.assertEqual(info["status"], "MISSING")
        self.assertFalse(info["is_socket"])

    def test_inspect_ssh_control_socket_active_and_stale(self):
        with tempfile.TemporaryDirectory(prefix="gs_sock_") as tmp_dir:
            sock_path = os.path.join(tmp_dir, "control-active.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            srv.listen(1)
            try:
                # 1. Active socket inspection
                info_active = inspect_ssh_control_socket(sock_path)
                self.assertEqual(info_active["status"], "ACTIVE")
                self.assertTrue(info_active["is_socket"])
                self.assertIsNone(info_active["error"])
            finally:
                srv.close()

            # 2. Stale socket inspection (inode remains on disk, but server closed)
            info_stale = inspect_ssh_control_socket(sock_path)
            self.assertEqual(info_stale["status"], "STALE")
            self.assertTrue(info_stale["is_socket"])
            self.assertIn("Connection refused", str(info_stale["error"]))

    def test_inspect_ssh_control_socket_corrupted_stub(self):
        with tempfile.TemporaryDirectory(prefix="gs_sock_") as tmp_dir:
            stub_path = os.path.join(tmp_dir, "control-corrupt.sock")
            Path(stub_path).touch()  # 0-byte regular file
            info = inspect_ssh_control_socket(stub_path)
            self.assertEqual(info["status"], "CORRUPTED")
            self.assertFalse(info["is_socket"])
            self.assertIn("Not a valid UNIX domain socket", str(info["error"]))

    def test_find_ssh_control_sockets_filtering(self):
        with tempfile.TemporaryDirectory(prefix="gs_sock_") as tmp_dir:
            # Create a regular file that should NEVER be pruned or treated as a socket
            key_file = os.path.join(tmp_dir, "id_ed25519")
            with open(key_file, "w") as f:
                f.write("DUMMY_KEY")
            config_file = os.path.join(tmp_dir, "config")
            with open(config_file, "w") as f:
                f.write("Host *")

            # Create an active socket
            act_sock = os.path.join(tmp_dir, "control-master-1")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(act_sock)
            srv.listen(1)

            # Create a stale socket
            stale_sock = os.path.join(tmp_dir, "control-master-2")
            srv_stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv_stale.bind(stale_sock)
            srv_stale.close()

            # Create a 0-byte corrupted socket stub
            corrupt_sock = os.path.join(tmp_dir, "control-corrupted")
            Path(corrupt_sock).touch()

            try:
                found = find_ssh_control_sockets(socket_dir=tmp_dir)
                paths = [f["path"] for f in found]
                self.assertIn(act_sock, paths)
                self.assertIn(stale_sock, paths)
                self.assertIn(corrupt_sock, paths)
                self.assertNotIn(key_file, paths)
                self.assertNotIn(config_file, paths)
            finally:
                srv.close()

    def test_prune_stale_ssh_control_sockets_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix="gs_sock_") as tmp_dir:
            act_sock = os.path.join(tmp_dir, "control-act")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(act_sock)
            srv.listen(1)

            stale_sock = os.path.join(tmp_dir, "control-stale")
            s_stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s_stale.bind(stale_sock)
            s_stale.close()

            corrupt_sock = os.path.join(tmp_dir, "control-corrupt")
            Path(corrupt_sock).touch()

            try:
                # 1. Dry run
                dry_res = prune_stale_ssh_control_sockets(socket_dir=tmp_dir, dry_run=True)
                self.assertEqual(len(dry_res["active"]), 1)
                self.assertEqual(len(dry_res["stale"]), 1)
                self.assertEqual(len(dry_res["corrupted"]), 1)
                self.assertIn(stale_sock, dry_res["pruned"])
                self.assertIn(corrupt_sock, dry_res["pruned"])
                self.assertTrue(os.path.exists(stale_sock))
                self.assertTrue(os.path.exists(corrupt_sock))

                # 2. Live prune
                prune_res = prune_stale_ssh_control_sockets(socket_dir=tmp_dir, dry_run=False)
                self.assertEqual(len(prune_res["pruned"]), 2)
                self.assertFalse(os.path.exists(stale_sock))
                self.assertFalse(os.path.exists(corrupt_sock))
                self.assertTrue(os.path.exists(act_sock))  # Active socket was preserved!
            finally:
                srv.close()

    def test_diagnose_git_error_control_socket(self):
        diag1 = diagnose_git_error("Control socket connect(/home/user/.ssh/control-abc): Connection refused")
        self.assertIsNotNone(diag1)
        self.assertEqual(diag1["category"], "SSH_CONTROL_SOCKET_ERROR")
        self.assertEqual(diag1["action_required"], "prune_ssh_sockets")

        diag2 = diagnose_git_error("Corrupted control socket /tmp/ssh-master")
        self.assertIsNotNone(diag2)
        self.assertEqual(diag2["category"], "SSH_CONTROL_SOCKET_ERROR")

    @patch("git_sync.engine.git.prune_stale_ssh_control_sockets")
    @patch("git_sync.engine.git.run_git")
    def test_run_git_with_retry_control_socket_recovery(self, mock_run_git, mock_prune):
        p_fail = subprocess.CompletedProcess(
            args=["git", "fetch"],
            returncode=255,
            stdout="",
            stderr="Control socket connect(/home/.ssh/control-123): Connection refused\nfatal: Could not read from remote",
        )
        p_ok = subprocess.CompletedProcess(
            args=["git", "fetch"],
            returncode=0,
            stdout="Fetched successfully",
            stderr="",
        )
        mock_run_git.side_effect = [p_fail, p_ok]

        res = run_git_with_retry("/dummy/repo", ["fetch"], retries=1, sleep_func=lambda s: None)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(mock_run_git.call_count, 2)
        mock_prune.assert_called_once()

    @patch("git_sync.cli.check_credentials_sanity")
    def test_cli_doctor_prune_ssh_flag(self, mock_sanity):
        mock_sanity.return_value = {
            "ok": True,
            "ssh_agent": {"status": "ACTIVE", "socket": "/sock", "identities_count": 1, "identities": ["key1"]},
            "ssh_keys": ["id_ed25519"],
            "ssh_multiplexing": {
                "is_enabled": True,
                "control_master": "auto",
                "stale_sockets_pruned": ["/home/user/.ssh/control-stale1", "/home/user/.ssh/control-stale2"],
                "active_sockets": ["/home/user/.ssh/control-active"],
            },
            "credential_helpers": [],
            "has_token_env": True,
            "remotes": {},
            "warnings": [],
            "errors": [],
            "suggestions": [],
        }

        captured = []
        with patch("sys.stdout.write", side_effect=captured.append):
            exit_code = main(["doctor", "--prune-ssh"])
            self.assertEqual(exit_code, 0)
            output = "".join(captured)
            self.assertIn("podado(s)", output)
            self.assertIn("ativa(s)", output)


class TestGitHubCliInspection(unittest.TestCase):
    """Test proactive GitHub CLI (gh) diagnostics and credential inspection."""

    @patch("shutil.which")
    def test_inspect_github_cli_not_installed(self, mock_which):
        mock_which.return_value = None
        info = inspect_github_cli()
        self.assertFalse(info["installed"])
        self.assertFalse(info["logged_in"])
        self.assertIn("not found on PATH", info["error"])

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_inspect_github_cli_authenticated(self, mock_which, mock_run):
        mock_which.return_value = "/usr/bin/gh"
        sample_output = """github.com
  ✓ Logged in to github.com account yegear1 (/home/user/.config/gh/hosts.yml)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_secrettoken123
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'
"""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh", "auth", "status"], returncode=0, stdout=sample_output, stderr=""
        )

        info = inspect_github_cli()
        self.assertTrue(info["installed"])
        self.assertTrue(info["logged_in"])
        self.assertEqual(info["active_account"], "yegear1")
        self.assertEqual(info["host"], "github.com")
        self.assertEqual(info["protocol"], "https")
        self.assertEqual(info["token_scopes"], ["gist", "read:org", "repo", "workflow"])
        self.assertIsNone(info["error"])

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_inspect_github_cli_not_logged_in(self, mock_which, mock_run):
        mock_which.return_value = "/usr/bin/gh"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh", "auth", "status"],
            returncode=1,
            stdout="",
            stderr="You are not logged into any GitHub hosts. Run gh auth login to authenticate.",
        )

        info = inspect_github_cli()
        self.assertTrue(info["installed"])
        self.assertFalse(info["logged_in"])
        self.assertIn("not logged", info["error"].lower())
        self.assertIn("gh auth login", info["suggestion"])

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_inspect_github_cli_timeout(self, mock_which, mock_run):
        mock_which.return_value = "/usr/bin/gh"
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gh", "auth", "status"], timeout=3)

        info = inspect_github_cli(timeout=3)
        self.assertTrue(info["installed"])
        self.assertFalse(info["logged_in"])
        self.assertIn("Timeout", info["error"])


class TestOfflineAndRemediationDiagnostics(unittest.TestCase):
    """Test differentiation of transient network drops from permanently unconfigured remotes."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _init_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "tester@test.local"], cwd=path, check=True)
        (path / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return path

    def test_inspect_repo_no_remote_semantics(self):
        repo = self._init_repo(self.root / "local_repo")
        info = inspect_repo(str(repo), str(self.root), do_fetch=False)

        self.assertEqual(info["status"], "NO_REMOTE")
        self.assertEqual(info["network_state"], "unconfigured")
        self.assertEqual(info["action_required"], "configure_remote")
        self.assertIn("remote add origin", info["remediation"])

    @patch("git_sync.engine.git.run_git_with_retry")
    def test_inspect_repo_transient_network_drop_classified_offline(self, mock_fetch):
        repo = self._init_repo(self.root / "network_drop_repo")
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/org/repo.git"], cwd=repo, check=True)

        mock_fetch.return_value = subprocess.CompletedProcess(
            args=["git", "fetch"],
            returncode=128,
            stdout="",
            stderr="fatal: unable to access 'https://github.com/org/repo.git/': Could not resolve host: github.com",
        )

        info = inspect_repo(str(repo), str(self.root), do_fetch=True)
        self.assertEqual(info["status"], "FETCH_ERROR")
        self.assertEqual(info["network_state"], "offline")
        self.assertEqual(info["action_required"], "check_network")
        self.assertIn("--no-fetch", info["remediation"])
        self.assertEqual(info["diagnostic"]["category"], "NETWORK_DNS_FAILURE")

    @patch("git_sync.engine.git.run_git_with_retry")
    def test_inspect_repo_auth_failure_classified_auth_error(self, mock_fetch):
        repo = self._init_repo(self.root / "auth_fail_repo")
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/org/repo.git"], cwd=repo, check=True)

        mock_fetch.return_value = subprocess.CompletedProcess(
            args=["git", "fetch"],
            returncode=128,
            stdout="",
            stderr="fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        )

        info = inspect_repo(str(repo), str(self.root), do_fetch=True)
        self.assertEqual(info["status"], "FETCH_ERROR")
        self.assertEqual(info["network_state"], "auth_error")
        self.assertEqual(info["diagnostic"]["category"], "HTTP_AUTH_REQUIRED")
        self.assertIsNotNone(info["remediation"])

    @patch("git_sync.engine.git.run_git_with_retry")
    def test_status_json_remediation_and_offline_segregation(self, mock_fetch):
        repo_ok = self._init_repo(self.root / "repo_offline")
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/org/repo.git"], cwd=repo_ok, check=True)

        repo_no_remote = self._init_repo(self.root / "repo_no_remote")

        mock_fetch.return_value = subprocess.CompletedProcess(
            args=["git", "fetch"],
            returncode=128,
            stdout="",
            stderr="fatal: unable to access 'https://github.com/': Could not resolve host: github.com",
        )

        captured = []
        with patch("sys.stdout.write", side_effect=captured.append):
            exit_code = main(["--root", str(self.root), "status", "--json", "--compact"])
            self.assertEqual(exit_code, 0)

        data = json.loads("".join(captured))
        self.assertIn("offline", data)
        self.assertIn("remediations", data)
        self.assertIn("repo_offline", data["offline"])
        self.assertIn("repo_no_remote", data["no_remote"])
        # Both repos have actionable remediations attached
        remed_repos = [r["repo"] for r in data["remediations"]]
        self.assertIn("repo_offline", remed_repos)
        self.assertIn("repo_no_remote", remed_repos)


class TestRemoteLatencyGradingAndDoctor(unittest.TestCase):
    """Test remote health check telemetry, concurrent probing, latency grading (A/B/C/F), and doctor formatting."""

    def test_compute_latency_grade_thresholds(self):
        # Grade A: < 0.50s
        self.assertEqual(compute_latency_grade(0.01), "A")
        self.assertEqual(compute_latency_grade(0.499), "A")

        # Grade B: 0.50s <= RTT < 1.50s
        self.assertEqual(compute_latency_grade(0.50), "B")
        self.assertEqual(compute_latency_grade(1.499), "B")

        # Grade C: 1.50s <= RTT < 3.00s
        self.assertEqual(compute_latency_grade(1.50), "C")
        self.assertEqual(compute_latency_grade(2.999), "C")

        # Grade F: >= 3.00s
        self.assertEqual(compute_latency_grade(3.00), "F")
        self.assertEqual(compute_latency_grade(12.5), "F")

        # Failure or invalid values unconditionally Grade F
        self.assertEqual(compute_latency_grade(0.2, ok=False), "F")
        self.assertEqual(compute_latency_grade(None, ok=True), "F")
        self.assertEqual(compute_latency_grade(-1.0, ok=True), "F")

    @patch("git_sync.engine.git.run_git")
    def test_probe_remote_reachability_success(self, mock_run_git):
        mock_run_git.return_value = subprocess.CompletedProcess(
            args=["git", "ls-remote"], returncode=0, stdout="hash\trefs/heads/main\n", stderr=""
        )

        res = probe_remote_reachability("git@github.com:org/repo.git", sample_repo="/dummy/repo", timeout=5)
        self.assertTrue(res["ok"])
        self.assertEqual(res["protocol"], "ssh")
        self.assertEqual(res["host"], "github.com")
        self.assertIsInstance(res["latency_seconds"], float)
        self.assertIn(res["grade"], ("A", "B", "C", "F"))
        self.assertIsNone(res["error"])
        self.assertIsNone(res["diagnostic"])

    @patch("git_sync.engine.git.run_git")
    def test_probe_remote_reachability_auth_failure(self, mock_run_git):
        mock_run_git.return_value = subprocess.CompletedProcess(
            args=["git", "ls-remote"],
            returncode=128,
            stdout="",
            stderr="git@github.com: Permission denied (publickey).",
        )

        res = probe_remote_reachability("git@github.com:org/repo.git", timeout=5)
        self.assertFalse(res["ok"])
        self.assertEqual(res["grade"], "F")
        self.assertIsNotNone(res["diagnostic"])
        self.assertEqual(res["diagnostic"]["category"], "SSH_PERMISSION_DENIED")

    @patch("git_sync.engine.git.run_git")
    def test_probe_remote_reachability_timeout(self, mock_run_git):
        mock_run_git.side_effect = subprocess.TimeoutExpired(cmd=["git", "ls-remote"], timeout=3)

        res = probe_remote_reachability("https://github.com/org/repo.git", timeout=3)
        self.assertFalse(res["ok"])
        self.assertEqual(res["grade"], "F")
        self.assertIn("Timeout", res["error"])
        self.assertEqual(res["diagnostic"]["category"], "NETWORK_TIMEOUT")

    @patch("git_sync.engine.git.probe_remote_reachability")
    @patch("git_sync.core.get_repo_remotes")
    @patch("git_sync.core.discover_repos")
    @patch("subprocess.run")
    def test_check_credentials_sanity_concurrent_grading(self, mock_run, mock_discover, mock_remotes, mock_probe):
        mock_discover.return_value = ["/dummy/repo1", "/dummy/repo2"]
        mock_remotes.side_effect = [
            {"origin": "git@github.com:org/repo1.git"},
            {"origin": "https://gitlab.com/org/repo2.git"},
        ]

        def probe_side_effect(remote_url, sample_repo=None, timeout=5):
            if "github.com" in remote_url:
                return {
                    "ok": True,
                    "protocol": "ssh",
                    "host": "github.com",
                    "latency_seconds": 0.150,
                    "grade": "A",
                    "error": None,
                    "diagnostic": None,
                }
            else:
                return {
                    "ok": True,
                    "protocol": "https",
                    "host": "gitlab.com",
                    "latency_seconds": 1.850,
                    "grade": "C",
                    "error": None,
                    "diagnostic": None,
                }

        mock_probe.side_effect = probe_side_effect
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        sanity = check_credentials_sanity("/dummy/root", timeout=5, test_remotes=True, workers=4)
        self.assertTrue(sanity["ok"])
        self.assertIn("git@github.com:org/repo1.git", sanity["remotes"])
        self.assertIn("https://gitlab.com/org/repo2.git", sanity["remotes"])

        gh_data = sanity["remotes"]["git@github.com:org/repo1.git"]
        self.assertEqual(gh_data["grade"], "A")
        self.assertEqual(gh_data["latency_seconds"], 0.150)

        gl_data = sanity["remotes"]["https://gitlab.com/org/repo2.git"]
        self.assertEqual(gl_data["grade"], "C")
        self.assertEqual(gl_data["latency_seconds"], 1.850)

        # Warning emitted for Grade C HTTPS remote (degraded DNS / slow route)
        self.assertTrue(any("degraded handshake latency" in w and "gitlab.com" in w for w in sanity["warnings"]))
        self.assertTrue(any("DNS lookup latency" in s for s in sanity["suggestions"]))

    @patch("git_sync.engine.git.probe_remote_reachability")
    @patch("git_sync.core.get_repo_remotes")
    @patch("git_sync.core.discover_repos")
    @patch("subprocess.run")
    def test_check_credentials_sanity_slow_ssh_diagnostics(self, mock_run, mock_discover, mock_remotes, mock_probe):
        mock_discover.return_value = ["/dummy/repo1"]
        mock_remotes.return_value = {"origin": "git@github.com:org/repo1.git"}
        mock_probe.return_value = {
            "ok": True,
            "protocol": "ssh",
            "host": "github.com",
            "latency_seconds": 2.200,
            "grade": "C",
            "error": None,
            "diagnostic": None,
        }
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        # 1. Slow SSH without multiplexing
        with patch("git_sync.engine.git.inspect_ssh_config", return_value={"is_enabled": False, "socket_dir_exists": False}):
            sanity_no_mux = check_credentials_sanity("/dummy/root", timeout=5, test_remotes=True)
            self.assertTrue(any("SSH multiplexing is disabled" in w for w in sanity_no_mux["warnings"]))

        # 2. Slow SSH with multiplexing enabled
        with patch("git_sync.engine.git.inspect_ssh_config", return_value={"is_enabled": True, "socket_dir": "/tmp/ssh", "socket_dir_exists": True}):
            sanity_mux = check_credentials_sanity("/dummy/root", timeout=5, test_remotes=True)
            self.assertTrue(any("despite SSH multiplexing" in w for w in sanity_mux["warnings"]))
            self.assertTrue(any("recreate SSH control sockets" in s for s in sanity_mux["suggestions"]))

    @patch("git_sync.cli.check_credentials_sanity")
    def test_cli_doctor_formats_latency_grades(self, mock_sanity):
        mock_sanity.return_value = {
            "ok": True,
            "ssh_agent": {"status": "ACTIVE", "socket": "/sock", "identities_count": 1, "identities": ["key1"]},
            "ssh_keys": ["id_ed25519"],
            "ssh_multiplexing": {"is_enabled": True, "control_master": "auto"},
            "credential_helpers": ["store"],
            "has_token_env": True,
            "remotes": {
                "git@github.com:org/fast-repo.git": {
                    "ok": True,
                    "protocol": "ssh",
                    "host": "github.com",
                    "latency_seconds": 0.124,
                    "grade": "A",
                    "error": None,
                    "diagnostic": None,
                },
                "https://gitlab.com/org/slow-repo.git": {
                    "ok": True,
                    "protocol": "https",
                    "host": "gitlab.com",
                    "latency_seconds": 2.450,
                    "grade": "C",
                    "error": None,
                    "diagnostic": None,
                },
            },
            "warnings": [],
            "errors": [],
            "suggestions": [],
        }

        # Test Text Formatting Table
        captured = []
        with patch("sys.stdout.write", side_effect=captured.append):
            exit_code = main(["doctor"])
            self.assertEqual(exit_code, 0)

        out_text = "".join(captured)
        self.assertIn("Grade A (0.124s)", out_text)
        self.assertIn("Grade C (2.450s)", out_text)
        self.assertIn("VERIFICAÇÃO DE REMOTES TESTADOS", out_text)

        # Test JSON Output
        captured_json = []
        with patch("sys.stdout.write", side_effect=captured_json.append):
            exit_code_json = main(["doctor", "--json"])
            self.assertEqual(exit_code_json, 0)

        data = json.loads("".join(captured_json))
        self.assertIn("remotes", data)
        self.assertEqual(data["remotes"]["git@github.com:org/fast-repo.git"]["grade"], "A")
        self.assertEqual(data["remotes"]["git@github.com:org/fast-repo.git"]["latency_seconds"], 0.124)
        self.assertEqual(data["remotes"]["https://gitlab.com/org/slow-repo.git"]["grade"], "C")
        self.assertEqual(data["remotes"]["https://gitlab.com/org/slow-repo.git"]["latency_seconds"], 2.450)


if __name__ == "__main__":
    unittest.main()
