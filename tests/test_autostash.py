"""
Unit tests for git-sync autostash automation, dirty tree preservation, and unstash conflict safeguards.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.config import GitSyncConfig, load_config
from git_sync.core import (
    check_repo_dirty,
    inspect_repo,
    pull_repo,
    restore_stash,
    run_git,
    stash_changes,
)


class TestGitAutostash(unittest.TestCase):

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

    def test_check_repo_dirty_states(self):
        repo = self._init_repo(self.root / "dirty_check_repo")
        (repo / "f.txt").write_text("initial\n")
        run_git(str(repo), ["add", "f.txt"])
        run_git(str(repo), ["commit", "-m", "init"])

        # 1. Clean
        is_dirty, files = check_repo_dirty(str(repo))
        self.assertFalse(is_dirty)
        self.assertEqual(files, [])

        # 2. Untracked
        (repo / "untracked.txt").write_text("untracked\n")
        is_dirty, files = check_repo_dirty(str(repo))
        self.assertTrue(is_dirty)
        self.assertTrue(any("untracked.txt" in f for f in files))

        # 3. Staged
        run_git(str(repo), ["add", "untracked.txt"])
        is_dirty, files = check_repo_dirty(str(repo))
        self.assertTrue(is_dirty)
        self.assertTrue(any("untracked.txt" in f for f in files))

        # 4. Unstaged
        (repo / "f.txt").write_text("modified\n")
        is_dirty, files = check_repo_dirty(str(repo))
        self.assertTrue(is_dirty)
        self.assertEqual(len(files), 2)

    def test_check_repo_dirty_filters_nested_worktrees(self):
        repo = self._init_repo(self.root / "nested_wt_repo")
        (repo / "f.txt").write_text("initial\n")
        run_git(str(repo), ["add", "f.txt"])
        run_git(str(repo), ["commit", "-m", "init"])

        nested_wt = repo / "worktrees" / "wt1"
        run_git(str(repo), ["worktree", "add", "-b", "feat", str(nested_wt)])

        # Status porcelain has ?? worktrees/, but check_repo_dirty filters it
        is_dirty, files = check_repo_dirty(str(repo))
        self.assertFalse(is_dirty)
        self.assertEqual(files, [])

    def test_stash_and_restore_preserves_staged_unstaged_and_untracked(self):
        repo = self._init_repo(self.root / "stash_preserve_repo")
        (repo / "tracked1.txt").write_text("line1\n")
        (repo / "tracked2.txt").write_text("line2\n")
        run_git(str(repo), ["add", "."])
        run_git(str(repo), ["commit", "-m", "init"])

        # Stage tracked1 modification
        (repo / "tracked1.txt").write_text("line1\nstaged\n")
        run_git(str(repo), ["add", "tracked1.txt"])
        # Add unstaged change to tracked1 as well
        (repo / "tracked1.txt").write_text("line1\nstaged\nunstaged\n")

        # Unstaged modification on tracked2
        (repo / "tracked2.txt").write_text("line2\nmodified\n")

        # Untracked new file
        (repo / "new_untracked.txt").write_text("new file\n")

        # Verify dirty
        is_dirty, files = check_repo_dirty(str(repo))
        self.assertTrue(is_dirty)

        # Stash
        stash_ok, tag, err = stash_changes(str(repo))
        self.assertTrue(stash_ok)
        self.assertIsNone(err)

        # Worktree must now be clean
        is_dirty_after, _ = check_repo_dirty(str(repo))
        self.assertFalse(is_dirty_after)

        # Restore
        res = restore_stash(str(repo))
        self.assertTrue(res["success"])
        self.assertFalse(res["conflict"])
        self.assertTrue(res["index_restored"])

        # Verify exact status restoration
        p_status = run_git(str(repo), ["status", "--porcelain"])
        status_lines = [l.rstrip() for l in p_status.stdout.splitlines() if l.strip()]
        self.assertIn("MM tracked1.txt", status_lines)
        self.assertIn(" M tracked2.txt", status_lines)
        self.assertIn("?? new_untracked.txt", status_lines)

    def test_pull_repo_autostash_clean_success(self):
        bare = self._init_bare_remote(self.root / "remote_autostash.git")
        local1 = self._init_repo(self.root / "local1")
        (local1 / "shared.txt").write_text("v1\n")
        run_git(str(local1), ["add", "shared.txt"])
        run_git(str(local1), ["commit", "-m", "v1"])
        run_git(str(local1), ["remote", "add", "origin", str(bare)])
        run_git(str(local1), ["push", "-u", "origin", "main"])

        # Clone local2 to push upstream commit
        local2 = self.root / "local2"
        run_git(str(self.root), ["clone", str(bare), str(local2)])
        run_git(str(local2), ["config", "user.email", "test2@example.com"])
        run_git(str(local2), ["config", "user.name", "Tester 2"])
        (local2 / "remote_file.txt").write_text("remote data\n")
        run_git(str(local2), ["add", "remote_file.txt"])
        run_git(str(local2), ["commit", "-m", "remote commit"])
        run_git(str(local2), ["push", "origin", "main"])

        # Make local uncommitted changes in local1
        (local1 / "local_work.txt").write_text("my local progress\n")
        (local1 / "staged_work.txt").write_text("staged progress\n")
        run_git(str(local1), ["add", "staged_work.txt"])

        # Execute pull_repo with autostash enabled (default)
        run_git(str(local1), ["fetch", "origin"])
        res = pull_repo(str(local1), ff_only=True, autostash=True)

        self.assertTrue(res["success"])
        self.assertTrue(res["autostash"]["stashed"])
        self.assertTrue(res["autostash"]["restored"])
        self.assertFalse(res["autostash"]["conflict"])

        # Check that remote changes are pulled
        self.assertTrue((local1 / "remote_file.txt").exists())
        self.assertEqual((local1 / "remote_file.txt").read_text(), "remote data\n")

        # Check that local uncommitted changes are intact
        self.assertTrue((local1 / "local_work.txt").exists())
        self.assertEqual((local1 / "local_work.txt").read_text(), "my local progress\n")
        self.assertTrue((local1 / "staged_work.txt").exists())
        p_status = run_git(str(local1), ["status", "--porcelain"])
        self.assertIn("A  staged_work.txt", p_status.stdout)
        self.assertIn("?? local_work.txt", p_status.stdout)

    def test_pull_repo_autostash_conflict_preserves_stash_without_data_loss(self):
        bare = self._init_bare_remote(self.root / "remote_conflict.git")
        local1 = self._init_repo(self.root / "local_conf1")
        f = local1 / "conflict_target.txt"
        f.write_text("line1\nline2\n")
        run_git(str(local1), ["add", "conflict_target.txt"])
        run_git(str(local1), ["commit", "-m", "init"])
        run_git(str(local1), ["remote", "add", "origin", str(bare)])
        run_git(str(local1), ["push", "-u", "origin", "main"])

        # Push conflicting change from local2
        local2 = self.root / "local_conf2"
        run_git(str(self.root), ["clone", str(bare), str(local2)])
        run_git(str(local2), ["config", "user.email", "test2@example.com"])
        run_git(str(local2), ["config", "user.name", "Tester 2"])
        (local2 / "conflict_target.txt").write_text("line1\nremote_edit\n")
        run_git(str(local2), ["commit", "-am", "remote conflicting edit"])
        run_git(str(local2), ["push", "origin", "main"])

        # Make local conflicting edit in local1
        f.write_text("line1\nlocal_edit\n")

        # Pull with autostash
        run_git(str(local1), ["fetch", "origin"])
        res = pull_repo(str(local1), ff_only=True, autostash=True)

        # Must report failure due to unstash conflict
        self.assertFalse(res["success"])
        self.assertTrue(res["autostash"]["stashed"])
        self.assertFalse(res["autostash"]["restored"])
        self.assertTrue(res["autostash"]["conflict"])
        self.assertIn("conflict_target.txt", res["autostash"]["conflict_files"])
        self.assertEqual(res["autostash"]["stash_ref"], "stash@{0}")

        # INVARIANT: Stash entry MUST NOT be lost
        p_stash_list = run_git(str(local1), ["stash", "list"])
        self.assertIn("stash@{0}", p_stash_list.stdout)
        self.assertIn("git-sync-autostash", p_stash_list.stdout)

    def test_pull_repo_with_no_autostash_flag(self):
        bare = self._init_bare_remote(self.root / "remote_no_autostash.git")
        local1 = self._init_repo(self.root / "local_no_auto")
        (local1 / "shared.txt").write_text("v1\n")
        run_git(str(local1), ["add", "shared.txt"])
        run_git(str(local1), ["commit", "-m", "v1"])
        run_git(str(local1), ["remote", "add", "origin", str(bare)])
        run_git(str(local1), ["push", "-u", "origin", "main"])

        # Remote change on shared.txt
        local2 = self.root / "local_no_auto2"
        run_git(str(self.root), ["clone", str(bare), str(local2)])
        run_git(str(local2), ["config", "user.email", "test2@example.com"])
        run_git(str(local2), ["config", "user.name", "Tester 2"])
        (local2 / "shared.txt").write_text("v2\n")
        run_git(str(local2), ["commit", "-am", "v2"])
        run_git(str(local2), ["push", "origin", "main"])

        # Modify shared.txt locally in local1 without committing
        (local1 / "shared.txt").write_text("local uncommitted\n")

        # Pull with autostash=False: Git refuses merge because local changes would be overwritten
        run_git(str(local1), ["fetch", "origin"])
        res = pull_repo(str(local1), ff_only=True, autostash=False)
        self.assertFalse(res["success"])
        self.assertFalse(res["autostash"]["stashed"])
        self.assertIn("overwritten by merge", res["stderr"])

    def test_pull_repo_restores_stash_if_pull_fails(self):
        bare = self._init_bare_remote(self.root / "remote_fail_pull.git")
        local1 = self._init_repo(self.root / "local_fail")
        (local1 / "f.txt").write_text("1\n")
        run_git(str(local1), ["add", "f.txt"])
        run_git(str(local1), ["commit", "-m", "1"])
        run_git(str(local1), ["remote", "add", "origin", str(bare)])
        run_git(str(local1), ["push", "-u", "origin", "main"])

        # Create divergence on both ends
        local2 = self.root / "local_fail2"
        run_git(str(self.root), ["clone", str(bare), str(local2)])
        run_git(str(local2), ["config", "user.email", "test2@example.com"])
        run_git(str(local2), ["config", "user.name", "Tester 2"])
        (local2 / "f2.txt").write_text("remote\n")
        run_git(str(local2), ["add", "f2.txt"])
        run_git(str(local2), ["commit", "-m", "remote divergent"])
        run_git(str(local2), ["push", "origin", "main"])

        # Local commit in local1 making it diverged
        (local1 / "local_commit.txt").write_text("local\n")
        run_git(str(local1), ["add", "local_commit.txt"])
        run_git(str(local1), ["commit", "-m", "local divergent"])

        # Plus dirty uncommitted change
        (local1 / "wip.txt").write_text("work in progress\n")

        run_git(str(local1), ["fetch", "origin"])
        # ff-only pull will fail because branch diverged
        res = pull_repo(str(local1), ff_only=True, autostash=True)

        self.assertFalse(res["success"])
        self.assertTrue(res["autostash"]["stashed"])
        self.assertTrue(res["autostash"]["restored"])

        # Uncommitted work in progress MUST be restored safely
        self.assertTrue((local1 / "wip.txt").exists())
        self.assertEqual((local1 / "wip.txt").read_text(), "work in progress\n")

    def test_cli_pull_autostash_flags_and_config(self):
        # Test config loading
        cfg = GitSyncConfig()
        self.assertTrue(cfg.pull_autostash)
        cfg_dict = cfg.to_dict()
        self.assertIn("autostash", cfg_dict["pull"])
        self.assertTrue(cfg_dict["pull"]["autostash"])

        # Test custom config with pull.autostash = false
        custom_cfg_file = self.root / ".git-sync.json"
        custom_cfg_file.write_text(json.dumps({
            "root": str(self.root),
            "pull": {
                "autostash": False
            }
        }))

        loaded = load_config(config_path=str(custom_cfg_file))
        self.assertFalse(loaded.pull_autostash)

    def test_cli_pull_autostash_end_to_end(self):
        bare = self._init_bare_remote(self.root / "remote_cli_auto.git")
        local1 = self._init_repo(self.root / "cli_auto_repo")
        (local1 / "file.txt").write_text("v1\n")
        run_git(str(local1), ["add", "file.txt"])
        run_git(str(local1), ["commit", "-m", "v1"])
        run_git(str(local1), ["remote", "add", "origin", str(bare)])
        run_git(str(local1), ["push", "-u", "origin", "main"])

        # Push v2 from another clone
        clone = self.root / "cli_clone"
        run_git(str(self.root), ["clone", str(bare), str(clone)])
        run_git(str(clone), ["config", "user.email", "c@example.com"])
        run_git(str(clone), ["config", "user.name", "Clone"])
        (clone / "f_clone.txt").write_text("clone\n")
        run_git(str(clone), ["add", "f_clone.txt"])
        run_git(str(clone), ["commit", "-m", "v2"])
        run_git(str(clone), ["push", "origin", "main"])

        # Dirty local1
        (local1 / "wip.txt").write_text("wip\n")

        # Capture JSON stdout via CLI main
        import io
        from contextlib import redirect_stdout

        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            code = main(["pull", "cli_auto_repo", "--root", str(self.root), "--autostash", "--json"])

        self.assertEqual(code, 0)
        output = json.loads(out_buf.getvalue())
        self.assertEqual(len(output), 1)
        res = output[0]
        self.assertTrue(res["success"])
        self.assertTrue(res["autostash"]["stashed"])
        self.assertTrue(res["autostash"]["restored"])
        self.assertTrue((local1 / "wip.txt").exists())


if __name__ == "__main__":
    unittest.main()
