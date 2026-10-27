"""
Automated unit tests for git-sync installation, symlink hygiene, and shell autocompletion.
Verifies idempotent symlink deployment, PATH detection, script generation, and global execution.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure src is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.installer import (
    generate_completion_script,
    get_canonical_skill_dir,
    get_default_completion_dir,
    get_default_install_dir,
    get_default_skill_dirs,
    get_launcher_path,
    install_agent_skills,
    install_completion,
    install_symlink,
    is_in_path,
    uninstall_agent_skills,
    uninstall_symlink,
)


class TestInstallerAndCompletion(unittest.TestCase):
    """Test suite for global installer and autocompletion generators."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target_dir = Path(self.temp_dir.name)
        # Create a mock launcher script
        self.mock_launcher = self.target_dir / "mock-git-sync"
        self.mock_launcher.write_text("#!/bin/sh\necho 'mock launcher'\n", encoding="utf-8")
        self.mock_launcher.chmod(0o755)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_get_default_install_dir_respects_xdg(self) -> None:
        with patch.dict(os.environ, {"XDG_BIN_HOME": "/custom/bin"}):
            self.assertEqual(get_default_install_dir(), Path("/custom/bin").resolve())

        with patch.dict(os.environ, {}, clear=True):
            expected = Path.home() / ".local" / "bin"
            self.assertEqual(get_default_install_dir(), expected)

    def test_is_in_path(self) -> None:
        test_dir = Path("/opt/custom/bin")
        with patch.dict(os.environ, {"PATH": f"/usr/bin{os.pathsep}/opt/custom/bin{os.pathsep}/bin"}):
            self.assertTrue(is_in_path(test_dir))

        with patch.dict(os.environ, {"PATH": f"/usr/bin{os.pathsep}/bin"}):
            self.assertFalse(is_in_path(test_dir))

    def test_install_symlink_success_and_idempotency(self) -> None:
        dest_dir = self.target_dir / "bin"

        # First install: should succeed with action "installed"
        res1 = install_symlink(
            target_dir=str(dest_dir),
            launcher_path=str(self.mock_launcher),
            force=False,
        )
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["action"], "installed")
        dest_link = dest_dir / "git-sync"
        self.assertTrue(dest_link.is_symlink())
        self.assertEqual(dest_link.resolve(), self.mock_launcher.resolve())

        # Second install: idempotent, should return "already_installed"
        res2 = install_symlink(
            target_dir=str(dest_dir),
            launcher_path=str(self.mock_launcher),
            force=False,
        )
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["action"], "already_installed")

    def test_install_symlink_conflict_and_force(self) -> None:
        dest_dir = self.target_dir / "bin"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_link = dest_dir / "git-sync"

        # Create conflicting file
        dest_link.write_text("conflicting binary", encoding="utf-8")

        # Without force: must refuse
        res_fail = install_symlink(
            target_dir=str(dest_dir),
            launcher_path=str(self.mock_launcher),
            force=False,
        )
        self.assertFalse(res_fail["ok"])
        self.assertIn("already exists", res_fail["error"])

        # With force: must overwrite atomically
        res_force = install_symlink(
            target_dir=str(dest_dir),
            launcher_path=str(self.mock_launcher),
            force=True,
        )
        self.assertTrue(res_force["ok"])
        self.assertEqual(res_force["action"], "installed")
        self.assertTrue(dest_link.is_symlink())
        self.assertEqual(dest_link.resolve(), self.mock_launcher.resolve())

    def test_uninstall_symlink(self) -> None:
        dest_dir = self.target_dir / "bin"
        install_symlink(target_dir=str(dest_dir), launcher_path=str(self.mock_launcher))
        dest_link = dest_dir / "git-sync"
        self.assertTrue(dest_link.exists())

        # Uninstall
        un_res = uninstall_symlink(target_dir=str(dest_dir))
        self.assertTrue(un_res["ok"])
        self.assertEqual(un_res["action"], "uninstalled")
        self.assertFalse(dest_link.exists())

        # Uninstall again: should succeed gracefully
        un_res2 = uninstall_symlink(target_dir=str(dest_dir))
        self.assertTrue(un_res2["ok"])
        self.assertEqual(un_res2["action"], "not_installed")

    def test_generate_completion_script_bash_and_zsh(self) -> None:
        bash_script = generate_completion_script("bash")
        self.assertIn("complete -F _git_sync_completions git-sync", bash_script)
        self.assertIn("--compact", bash_script)
        self.assertIn("--complete-repos", bash_script)

        zsh_script = generate_completion_script("zsh")
        self.assertIn("#compdef git-sync", zsh_script)
        self.assertIn("doctor:Run proactive credential", zsh_script)
        self.assertIn("--complete-repos", zsh_script)

        with self.assertRaises(ValueError):
            generate_completion_script("fish")

    def test_install_completion(self) -> None:
        comp_dir = self.target_dir / "completions"

        # Bash
        res_bash = install_completion("bash", target_dir=str(comp_dir))
        self.assertTrue(res_bash["ok"])
        self.assertTrue((comp_dir / "git-sync").is_file())

        # Zsh
        res_zsh = install_completion("zsh", target_dir=str(comp_dir))
        self.assertTrue(res_zsh["ok"])
        self.assertTrue((comp_dir / "_git-sync").is_file())

    @patch("git_sync.cli.uninstall_agent_skills", return_value=[])
    @patch("git_sync.cli.install_agent_skills", return_value=[])
    def test_cli_install_subcommand_json(self, mock_install_skills, mock_uninstall_skills) -> None:
        dest_dir = self.target_dir / "cli_bin"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = main(["install", "--target-dir", str(dest_dir), "--json"])
        self.assertEqual(exit_code, 0)
        self.assertTrue((dest_dir / "git-sync").is_symlink())
        mock_install_skills.assert_called_once()

        # Test uninstall via CLI
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            exit_un = main(["install", "--target-dir", str(dest_dir), "--uninstall", "--json"])
        self.assertEqual(exit_un, 0)
        self.assertFalse((dest_dir / "git-sync").exists())
        mock_uninstall_skills.assert_called_once()

    def test_cli_completion_subcommand(self) -> None:
        dest_dir = self.target_dir / "cli_comp"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["completion", "bash"])
        self.assertEqual(code, 0)

        # Test completion installation via CLI
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            code_install = main(["completion", "bash", "--install", "--target-dir", str(dest_dir), "--json"])
        self.assertEqual(code_install, 0)
        self.assertTrue((dest_dir / "git-sync").is_file())

    def test_install_agent_skills_success_and_idempotency(self) -> None:
        """Verify agent skills symlink deployment across target directories."""
        mock_skills_root = self.target_dir / "skills_repo" / "git-sync"
        mock_skills_root.mkdir(parents=True, exist_ok=True)
        (mock_skills_root / "SKILL.md").write_text("---\nname: git-sync\n---\n# Skill\n", encoding="utf-8")

        target_skill_dir = self.target_dir / "gemini_skills"

        # 1. First install
        results = install_agent_skills(
            target_dirs=[str(target_skill_dir)],
            force=False,
            skill_src=str(mock_skills_root),
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["action"], "installed")
        symlink_path = target_skill_dir / "git-sync"
        self.assertTrue(symlink_path.is_symlink())
        self.assertEqual(symlink_path.resolve(), mock_skills_root.resolve())

        # 2. Idempotent second install
        results2 = install_agent_skills(
            target_dirs=[str(target_skill_dir)],
            force=False,
            skill_src=str(mock_skills_root),
        )
        self.assertEqual(len(results2), 1)
        self.assertTrue(results2[0]["ok"])
        self.assertEqual(results2[0]["action"], "already_installed")

    def test_install_agent_skills_conflict_and_force(self) -> None:
        """Verify conflict resolution when a regular file or different symlink exists."""
        mock_skills_root = self.target_dir / "skills_repo" / "git-sync"
        mock_skills_root.mkdir(parents=True, exist_ok=True)
        (mock_skills_root / "SKILL.md").write_text("---\nname: git-sync\n---\n", encoding="utf-8")

        target_skill_dir = self.target_dir / "gemini_skills"
        target_skill_dir.mkdir(parents=True, exist_ok=True)
        dest_link = target_skill_dir / "git-sync"
        dest_link.write_text("conflict file", encoding="utf-8")

        # Refusal without force
        results = install_agent_skills(
            target_dirs=[str(target_skill_dir)],
            force=False,
            skill_src=str(mock_skills_root),
        )
        self.assertFalse(results[0]["ok"])
        self.assertIn("exists", results[0]["error"])

        # Overwrite with force
        results_force = install_agent_skills(
            target_dirs=[str(target_skill_dir)],
            force=True,
            skill_src=str(mock_skills_root),
        )
        self.assertTrue(results_force[0]["ok"])
        self.assertEqual(results_force[0]["action"], "installed")
        self.assertTrue(dest_link.is_symlink())

    def test_uninstall_agent_skills(self) -> None:
        """Verify uninstallation of agent skill symlinks."""
        mock_skills_root = self.target_dir / "skills_repo" / "git-sync"
        mock_skills_root.mkdir(parents=True, exist_ok=True)
        (mock_skills_root / "SKILL.md").write_text("---\nname: git-sync\n---\n", encoding="utf-8")

        target_skill_dir = self.target_dir / "gemini_skills"
        install_agent_skills(target_dirs=[str(target_skill_dir)], skill_src=str(mock_skills_root))
        dest_link = target_skill_dir / "git-sync"
        self.assertTrue(dest_link.exists())

        # Uninstall
        un_results = uninstall_agent_skills(
            target_dirs=[str(target_skill_dir)],
            skill_src=str(mock_skills_root),
        )
        self.assertTrue(un_results[0]["ok"])
        self.assertEqual(un_results[0]["action"], "uninstalled")
        self.assertFalse(dest_link.exists())

    def test_global_execution_from_arbitrary_cwd(self) -> None:
        """
        Test that running the real repository launcher via symlink from outside
        the repository (e.g. from /tmp) works seamlessly and loads git_sync module.
        """
        real_launcher = get_launcher_path()
        dest_dir = self.target_dir / "global_bin"
        install_symlink(target_dir=str(dest_dir), launcher_path=str(real_launcher))

        symlink_bin = dest_dir / "git-sync"
        self.assertTrue(symlink_bin.is_symlink())

        # Execute symlinked binary from /tmp with --no-fetch and --json
        cmd = [str(symlink_bin), "status", "--no-fetch", "--compact", "--json"]
        proc = subprocess.run(
            cmd,
            cwd="/tmp",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, f"Stderr: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertIn("total", data)
        self.assertIn("pull_pending", data)


if __name__ == "__main__":
    unittest.main()
