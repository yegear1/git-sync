"""
Unit tests for git-sync configuration loader, workspace profiles, and flag precedence.
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

from git_sync.cli import main
from git_sync.config import (
    CONFIG_FILENAME,
    GitSyncConfig,
    find_config_file,
    load_config,
    load_config_file,
    resolve_config,
)
from git_sync.core import (
    DEFAULT_ROOT,
    DEFAULT_TIMEOUT,
    DEFAULT_WORKERS,
    discover_repos,
    resolve_default_root,
    run_git,
)


class TestGitSyncConfig(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_find_config_file_explicit_custom_path(self):
        cfg_file = self.root_path / "custom-sync.json"
        cfg_file.write_text("{}", encoding="utf-8")
        found = find_config_file(str(cfg_file))
        self.assertEqual(found, str(cfg_file.resolve()))

    def test_find_config_file_explicit_missing_raises(self):
        missing = self.root_path / "non-existent.json"
        with self.assertRaises(FileNotFoundError):
            find_config_file(str(missing))

    def test_find_config_file_env_var(self):
        cfg_file = self.root_path / "env-sync.json"
        cfg_file.write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, {"GIT_SYNC_CONFIG": str(cfg_file)}):
            found = find_config_file(None)
            self.assertEqual(found, str(cfg_file.resolve()))

    def test_find_config_file_cwd(self):
        cfg_file = self.root_path / CONFIG_FILENAME
        cfg_file.write_text("{}", encoding="utf-8")
        with patch("pathlib.Path.cwd", return_value=self.root_path):
            with patch.dict(os.environ, {}, clear=True):
                found = find_config_file(None)
                self.assertEqual(found, str(cfg_file.resolve()))

    def test_find_config_file_home(self):
        cfg_file = self.root_path / CONFIG_FILENAME
        cfg_file.write_text("{}", encoding="utf-8")
        # Ensure cwd does not contain config
        other_cwd = self.root_path / "empty_cwd"
        other_cwd.mkdir()
        with patch("pathlib.Path.cwd", return_value=other_cwd):
            with patch("pathlib.Path.home", return_value=self.root_path):
                with patch.dict(os.environ, {}, clear=True):
                    found = find_config_file(None)
                    self.assertEqual(found, str(cfg_file.resolve()))

    def test_find_config_file_xdg(self):
        xdg_dir = self.root_path / "xdg_config" / "git-sync"
        xdg_dir.mkdir(parents=True)
        cfg_file = xdg_dir / "config.json"
        cfg_file.write_text("{}", encoding="utf-8")

        other_cwd = self.root_path / "empty_cwd_xdg"
        other_cwd.mkdir()
        other_home = self.root_path / "empty_home_xdg"
        other_home.mkdir()

        with patch("pathlib.Path.cwd", return_value=other_cwd):
            with patch("pathlib.Path.home", return_value=other_home):
                with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root_path / "xdg_config")}):
                    found = find_config_file(None)
                    self.assertEqual(found, str(cfg_file.resolve()))

    def test_find_config_file_none(self):
        other_cwd = self.root_path / "empty_cwd"
        other_home = self.root_path / "empty_home"
        other_cwd.mkdir()
        other_home.mkdir()
        with patch("pathlib.Path.cwd", return_value=other_cwd):
            with patch("pathlib.Path.home", return_value=other_home):
                with patch.dict(os.environ, {}, clear=True):
                    found = find_config_file(None)
                    self.assertIsNone(found)

    def test_load_config_file_valid_and_invalid(self):
        valid = self.root_path / "valid.json"
        valid.write_text(json.dumps({"root": "/tmp/repos", "workers": 4}), encoding="utf-8")
        data = load_config_file(str(valid))
        self.assertEqual(data["root"], "/tmp/repos")
        self.assertEqual(data["workers"], 4)

        invalid_syntax = self.root_path / "bad.json"
        invalid_syntax.write_text("{root: bad json", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config_file(str(invalid_syntax))

        non_dict = self.root_path / "list.json"
        non_dict.write_text("['a', 'b']", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config_file(str(non_dict))

    def test_resolve_config_defaults(self):
        cfg = resolve_config(None)
        self.assertEqual(cfg.root, DEFAULT_ROOT)
        self.assertEqual(cfg.workers, DEFAULT_WORKERS)
        self.assertEqual(cfg.timeout, DEFAULT_TIMEOUT)
        self.assertEqual(cfg.exclude, [])
        self.assertEqual(cfg.groups, [])
        self.assertTrue(cfg.pull_ff_only)
        self.assertTrue(cfg.pull_sync_branches)
        self.assertFalse(cfg.pull_all)
        self.assertFalse(cfg.push_dry_run)
        self.assertFalse(cfg.status_no_fetch)

    def test_resolve_config_custom_values(self):
        raw = {
            "root": "/workspace/repos",
            "workers": 8,
            "timeout": 15,
            "exclude": ["legacy", "temp/*"],
            "groups": ["org-a", "org-b"],
            "pull": {
                "ff_only": False,
                "sync_branches": False,
                "all": True,
            },
            "push": {
                "dry_run": True,
                "all": True,
                "branch": "main",
            },
            "status": {
                "no_fetch": True,
                "pending": True,
            }
        }
        cfg = resolve_config(raw)
        self.assertEqual(cfg.root, "/workspace/repos")
        self.assertEqual(cfg.workers, 8)
        self.assertEqual(cfg.timeout, 15)
        self.assertEqual(cfg.exclude, ["legacy", "temp/*"])
        self.assertEqual(cfg.groups, ["org-a", "org-b"])
        self.assertFalse(cfg.pull_ff_only)
        self.assertFalse(cfg.pull_sync_branches)
        self.assertTrue(cfg.pull_all)
        self.assertTrue(cfg.push_dry_run)
        self.assertTrue(cfg.push_all)
        self.assertEqual(cfg.push_branch, "main")
        self.assertTrue(cfg.status_no_fetch)
        self.assertTrue(cfg.status_pending)

    def test_resolve_config_invalid_types_raise(self):
        with self.assertRaises(ValueError):
            resolve_config({"workers": -5})
        with self.assertRaises(ValueError):
            resolve_config({"workers": "invalid"})
        with self.assertRaises(ValueError):
            resolve_config({"timeout": 0})
        with self.assertRaises(ValueError):
            resolve_config({"exclude": 123})
        with self.assertRaises(ValueError):
            resolve_config({"exclude": [123, "valid"]})
        with self.assertRaises(ValueError):
            resolve_config({"groups": 456})

    def test_resolve_workspace_profiles(self):
        raw = {
            "root": "/default/repos",
            "workers": 6,
            "timeout": 10,
            "exclude": ["global-ignore"],
            "default_profile": "work",
            "profiles": {
                "work": {
                    "root": "/work/repos",
                    "groups": ["team-backend"],
                    "pull": {"ff_only": True}
                },
                "personal": {
                    "root": "/personal/repos",
                    "exclude": ["experiments"],
                    "push": {"dry_run": True}
                }
            }
        }

        # 1. Automatic selection of default_profile ("work")
        cfg_default = resolve_config(raw)
        self.assertEqual(cfg_default.active_profile, "work")
        self.assertEqual(cfg_default.root, "/work/repos")
        self.assertEqual(cfg_default.workers, 6)  # inherited
        self.assertEqual(cfg_default.groups, ["team-backend"])
        self.assertEqual(cfg_default.available_profiles, ["personal", "work"])

        # 2. Explicit selection of "personal" profile
        cfg_personal = resolve_config(raw, profile_name="personal")
        self.assertEqual(cfg_personal.active_profile, "personal")
        self.assertEqual(cfg_personal.root, "/personal/repos")
        self.assertEqual(cfg_personal.exclude, ["experiments"])
        self.assertTrue(cfg_personal.push_dry_run)

        # 3. Requesting non-existent profile raises ValueError
        with self.assertRaises(ValueError):
            resolve_config(raw, profile_name="unknown")

        # 4. Requesting profile when no profiles defined raises ValueError
        with self.assertRaises(ValueError):
            resolve_config({"root": "/repos"}, profile_name="work")


class TestExclusionsAndDiscovery(unittest.TestCase):

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

    def test_discover_repos_with_exclusions(self):
        repo_active1 = self._init_repo(self.root / "ye-dev" / "repo1")
        repo_active2 = self._init_repo(self.root / "ye-sandbox" / "repo2")
        repo_archived = self._init_repo(self.root / "archived" / "repo3")
        repo_vendor = self._init_repo(self.root / "ye-dev" / "vendor" / "repo4")
        repo_temp = self._init_repo(self.root / "temp-audit" / "repo5")

        # 1. Discover all without exclusions
        all_found = discover_repos(str(self.root))
        self.assertEqual(len(all_found), 5)

        # 2. Exclude by exact directory name and relative pattern
        exclusions = ["archived", "ye-dev/vendor", "temp-*"]
        filtered = discover_repos(str(self.root), exclude=exclusions)
        expected = sorted([str(repo_active1.resolve()), str(repo_active2.resolve())])
        self.assertEqual(sorted(filtered), expected)


class TestCliConfigurationPrecedence(unittest.TestCase):

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

    def test_cli_config_subcommand_json(self):
        cfg_file = self.root / ".git-sync.json"
        cfg_data = {
            "root": str(self.root),
            "workers": 5,
            "timeout": 12,
            "groups": ["group-a"],
            "profiles": {
                "dev": {
                    "workers": 8,
                    "groups": ["group-b"]
                }
            }
        }
        cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main(["--config", str(cfg_file), "config", "--json"])

        self.assertEqual(exit_code, 0)
        output = json.loads(stdout_capture.getvalue())
        self.assertEqual(output["root"], str(self.root))
        self.assertEqual(output["workers"], 5)
        self.assertEqual(output["groups"], ["group-a"])

        # Test selecting profile via CLI flag
        stdout_capture_prof = io.StringIO()
        with patch("sys.stdout", stdout_capture_prof):
            exit_code_prof = main(["--config", str(cfg_file), "--profile", "dev", "config", "--json"])

        self.assertEqual(exit_code_prof, 0)
        output_prof = json.loads(stdout_capture_prof.getvalue())
        self.assertEqual(output_prof["active_profile"], "dev")
        self.assertEqual(output_prof["workers"], 8)
        self.assertEqual(output_prof["groups"], ["group-b"])

    def test_cli_flags_override_config_file(self):
        cfg_file = self.root / ".git-sync.json"
        custom_root = self.root / "config_root"
        custom_root.mkdir()
        cfg_data = {
            "root": str(custom_root),
            "workers": 2,
            "timeout": 5,
            "pull": {"ff_only": False},
            "push": {"dry_run": True},
            "status": {"no_fetch": True}
        }
        cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

        # Create repo under an alternative root
        cli_root = self.root / "cli_root"
        repo = self._init_repo(cli_root / "sample-repo")

        # Run status with CLI override --root cli_root
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main([
                "--config", str(cfg_file),
                "--root", str(cli_root),
                "status",
                "--json"
            ])

        self.assertEqual(exit_code, 0)
        output = json.loads(stdout_capture.getvalue())
        self.assertEqual(output["root"], str(cli_root))
        self.assertEqual(output["total"], 1)
        self.assertEqual(output["items"][0]["name"], "sample-repo")

    def test_cli_boolean_flag_overrides(self):
        # Config with pull.ff_only = False
        cfg_file = self.root / ".git-sync.json"
        cfg_data = {
            "root": str(self.root),
            "pull": {"ff_only": False},
            "push": {"dry_run": True}
        }
        cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

        # Check config inspection with CLI overrides
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main([
                "--config", str(cfg_file),
                "config"
            ])
        self.assertEqual(exit_code, 0)
        text_out = stdout_capture.getvalue()
        self.assertIn("Pull (ff-only):      False", text_out)
        self.assertIn("Push (dry-run):      True", text_out)

    def test_cli_invalid_config_path_returns_error(self):
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            exit_code = main(["--config", "/non/existent/path.json", "status"])
        self.assertEqual(exit_code, 1)
        self.assertIn("Configuration error", stderr_capture.getvalue())

    def test_resolve_config_with_pull_strategy(self):
        data = {
            "pull": {"strategy": "rebase", "ff_only": False},
            "profiles": {
                "fast": {"pull": {"strategy": "merge"}}
            }
        }
        cfg_default = resolve_config(data)
        self.assertEqual(cfg_default.pull_strategy, "rebase")
        self.assertEqual(cfg_default.to_dict()["pull"]["strategy"], "rebase")

        cfg_profile = resolve_config(data, profile_name="fast")
        self.assertEqual(cfg_profile.pull_strategy, "merge")
        self.assertEqual(cfg_profile.to_dict()["pull"]["strategy"], "merge")

    def test_resolve_default_root_hierarchy(self):
        # 1. GIT_SYNC_ROOT env var has highest precedence
        with patch.dict(os.environ, {"GIT_SYNC_ROOT": "/custom/env/root"}):
            self.assertEqual(resolve_default_root(), "/custom/env/root")

        # 2. When GIT_SYNC_ROOT is absent/empty, checks ~/github first
        fake_home = self.root / "fake_home"
        fake_github_lower = fake_home / "github"
        fake_github_upper = fake_home / "Github"

        fake_home.mkdir(parents=True, exist_ok=True)
        fake_github_lower.mkdir(parents=True, exist_ok=True)
        fake_github_upper.mkdir(parents=True, exist_ok=True)

        expected_cwd = str(Path.cwd().resolve())

        mock_pw = patch("pwd.getpwuid")
        with patch.dict(os.environ, {"GIT_SYNC_ROOT": "", "USER": ""}, clear=False):
            with patch.object(Path, "home", return_value=fake_home):
                try:
                    import pwd
                    with patch("pwd.getpwuid", return_value=type("PW", (), {"pw_dir": str(fake_home)})):
                        self.assertEqual(resolve_default_root(), str(fake_github_lower))

                        # 3. When ~/github is absent but ~/Github exists
                        fake_github_lower.rmdir()
                        self.assertEqual(resolve_default_root(), str(fake_github_upper))

                        # 4. When neither exists, returns current working directory fallback
                        fake_github_upper.rmdir()
                        self.assertEqual(resolve_default_root(), expected_cwd)
                except ImportError:
                    self.assertEqual(resolve_default_root(), str(fake_github_lower))
                    fake_github_lower.rmdir()
                    self.assertEqual(resolve_default_root(), str(fake_github_upper))
                    fake_github_upper.rmdir()
                    self.assertEqual(resolve_default_root(), expected_cwd)

    def test_config_status_compact_setting(self):
        cfg_default = GitSyncConfig()
        self.assertFalse(cfg_default.status_compact)
        self.assertFalse(cfg_default.to_dict()["status"]["compact"])

        custom_data = {
            "status": {
                "compact": True
            }
        }
        cfg_custom = resolve_config(custom_data)
        self.assertTrue(cfg_custom.status_compact)
        self.assertTrue(cfg_custom.to_dict()["status"]["compact"])

    def test_cli_config_displays_compact_status(self):
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main(["config"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Status (compact):", stdout_capture.getvalue())


if __name__ == "__main__":
    unittest.main()
