"""
Unit tests for git-sync core functionality.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from git_sync.cli import main
from git_sync.core import (
    ClassifiedCommit,
    classify_commit,
    discover_repos,
    find_push_pending_repos,
    format_changelog_summary,
    get_repo_commits,
    get_repo_pending_changelog,
    get_submodules_status,
    get_worktree_info,
    inspect_repo,
    match_repo,
    parse_commit_log,
    pull_repo,
    push_repo,
    run_git,
    summarize_commit_categories,
)
from git_sync.ui.dashboard import InteractiveDashboard


class TestGitSyncCore(unittest.TestCase):

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

    def test_discover_repos_empty(self):
        repos = discover_repos(str(self.root))
        self.assertEqual(repos, [])

    def test_discover_repos_with_git(self):
        # Create 2 fake repos
        repo1 = self.root / "org" / "repo1"
        (repo1 / ".git").mkdir(parents=True)

        repo2 = self.root / "repo2"
        (repo2 / ".git").mkdir(parents=True)

        # Create ignored dirs
        (self.root / "node_modules" / "sub" / ".git").mkdir(parents=True)
        (self.root / ".venv" / ".git").mkdir(parents=True)

        discovered = discover_repos(str(self.root))
        expected = [str(repo2.resolve()), str(repo1.resolve())]
        self.assertEqual(sorted(discovered), sorted(expected))

    def test_match_repo(self):
        root = "/workspace/repos"
        all_repos = [
            f"{root}/group-a/template-agent",
            f"{root}/group-a/hydra-proxy",
            f"{root}/group-b/taskctl",
        ]

        # Exact match
        self.assertEqual(
            match_repo(f"{root}/group-b/taskctl", all_repos, root),
            f"{root}/group-b/taskctl",
        )

        # Relative path match
        self.assertEqual(
            match_repo("group-a/hydra-proxy", all_repos, root),
            f"{root}/group-a/hydra-proxy",
        )

        # Basename match
        self.assertEqual(
            match_repo("template-agent", all_repos, root),
            f"{root}/group-a/template-agent",
        )

        # Non-existent
        self.assertIsNone(match_repo("non-existent-repo", all_repos, root))

    def test_run_git_timeout_handling(self):
        res = run_git(str(self.root), ["--version"], timeout=5)
        self.assertEqual(res.returncode, 0)
        self.assertIn("git version", res.stdout)

    def test_push_repo_no_remote(self):
        repo = self._init_repo(self.root / "no_remote_repo")
        res = push_repo(str(repo))
        self.assertFalse(res["success"])
        self.assertIn("No remote configured", res["stderr"])

    def test_push_repo_dry_run_and_real_push(self):
        bare_remote = self._init_bare_remote(self.root / "remote.git")
        local_repo = self._init_repo(self.root / "local_repo")

        # Initial commit & push
        (local_repo / "README.md").write_text("# Test Repo\n")
        run_git(str(local_repo), ["add", "README.md"])
        run_git(str(local_repo), ["commit", "-m", "Initial commit"])
        run_git(str(local_repo), ["remote", "add", "origin", str(bare_remote)])
        run_git(str(local_repo), ["push", "-u", "origin", "main"])

        # Second commit (unpushed)
        (local_repo / "file.txt").write_text("Pending push\n")
        run_git(str(local_repo), ["add", "file.txt"])
        run_git(str(local_repo), ["commit", "-m", "Second commit"])

        # Verify ahead detection
        info = inspect_repo(str(local_repo), str(self.root), do_fetch=False)
        self.assertEqual(info["ahead"], 1)
        self.assertEqual(info["status"], "PUSH_PENDING")

        # Dry-run push
        res_dry = push_repo(str(local_repo), dry_run=True)
        self.assertTrue(res_dry["success"])
        self.assertTrue(res_dry["dry_run"])
        self.assertEqual(res_dry["ahead"], 1)

        # Confirm remote has not received second commit yet
        p_count = run_git(str(bare_remote), ["rev-list", "--count", "main"])
        self.assertEqual(p_count.stdout.strip(), "1")

        # Real push
        res_real = push_repo(str(local_repo), dry_run=False)
        self.assertTrue(res_real["success"])
        self.assertFalse(res_real["dry_run"])

        # Confirm remote has received second commit
        p_count_after = run_git(str(bare_remote), ["rev-list", "--count", "main"])
        self.assertEqual(p_count_after.stdout.strip(), "2")

    def test_push_repo_diverged_safety_guard(self):
        bare_remote = self._init_bare_remote(self.root / "remote_diverged.git")

        # Clone A: initial commit
        repo_a = self._init_repo(self.root / "repo_a")
        (repo_a / "README.md").write_text("# Initial\n")
        run_git(str(repo_a), ["add", "README.md"])
        run_git(str(repo_a), ["commit", "-m", "Initial"])
        run_git(str(repo_a), ["remote", "add", "origin", str(bare_remote)])
        run_git(str(repo_a), ["push", "-u", "origin", "main"])

        # Clone B: clone from remote
        repo_b = self._init_repo(self.root / "repo_b")
        run_git(str(repo_b), ["remote", "add", "origin", str(bare_remote)])
        run_git(str(repo_b), ["pull", "origin", "main"])
        run_git(str(repo_b), ["branch", "-u", "origin/main", "main"])

        # Repo A makes commit and pushes
        (repo_a / "file_a.txt").write_text("from A\n")
        run_git(str(repo_a), ["add", "file_a.txt"])
        run_git(str(repo_a), ["commit", "-m", "commit from A"])
        run_git(str(repo_a), ["push", "origin", "main"])

        # Repo B makes independent local commit without pulling
        (repo_b / "file_b.txt").write_text("from B\n")
        run_git(str(repo_b), ["add", "file_b.txt"])
        run_git(str(repo_b), ["commit", "-m", "commit from B"])

        # Fetch remote on Repo B so it knows it is both ahead and behind
        run_git(str(repo_b), ["fetch", "origin"])

        info = inspect_repo(str(repo_b), str(self.root), do_fetch=False)
        self.assertEqual(info["status"], "DIVERGED")
        self.assertEqual(info["ahead"], 1)
        self.assertEqual(info["behind"], 1)

        # Attempt push on diverged repo -> must be rejected by safety guard
        res = push_repo(str(repo_b))
        self.assertFalse(res["success"])
        self.assertIn("diverged", res["stderr"])

    def test_find_push_pending_repos(self):
        bare = self._init_bare_remote(self.root / "bare.git")
        r1 = self._init_repo(self.root / "r1")
        r2 = self._init_repo(self.root / "r2")

        # Initial commits
        (r1 / "f.txt").write_text("r1\n")
        run_git(str(r1), ["add", "f.txt"])
        run_git(str(r1), ["commit", "-m", "init"])
        run_git(str(r1), ["remote", "add", "origin", str(bare)])
        run_git(str(r1), ["push", "-u", "origin", "main"])

        # r1 has pending commit
        (r1 / "f2.txt").write_text("r1 pending\n")
        run_git(str(r1), ["add", "f2.txt"])
        run_git(str(r1), ["commit", "-m", "r1 pending"])

        # r2 is up to date with its remote
        bare2 = self._init_bare_remote(self.root / "bare2.git")
        (r2 / "f.txt").write_text("r2\n")
        run_git(str(r2), ["add", "f.txt"])
        run_git(str(r2), ["commit", "-m", "init"])
        run_git(str(r2), ["remote", "add", "origin", str(bare2)])
        run_git(str(r2), ["push", "-u", "origin", "main"])

        pending = find_push_pending_repos([str(r1), str(r2)], str(self.root), do_fetch=False)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["path"], str(r1))

    def test_discover_repos_worktree_and_submodule_filtering(self):
        main = self._init_repo(self.root / "main")
        (main / "f.txt").write_text("main\n")
        run_git(str(main), ["add", "f.txt"])
        run_git(str(main), ["commit", "-m", "init"])

        # Add linked worktree as sibling
        wt = self.root / "main-wt"
        run_git(str(main), ["worktree", "add", str(wt), "-b", "feature"])

        # Add submodule repo
        sub = self._init_repo(self.root / "sub_source")
        (sub / "s.txt").write_text("sub\n")
        run_git(str(sub), ["add", "s.txt"])
        run_git(str(sub), ["commit", "-m", "sub init"])

        run_git(str(main), ["-c", "protocol.file.allow=always", "submodule", "add", str(sub), "vendor/sub"])
        run_git(str(main), ["commit", "-m", "add submodule"])

        discovered = discover_repos(str(self.root))
        # main, main-wt, and sub_source should be discovered
        # vendor/sub (nested submodule) MUST NOT be discovered as top-level repo
        expected = sorted([str(main.resolve()), str(wt.resolve()), str(sub.resolve())])
        self.assertEqual(discovered, expected)

    def test_inspect_worktree_no_false_dirty_nested(self):
        main = self._init_repo(self.root / "main_nested")
        (main / "f.txt").write_text("main\n")
        run_git(str(main), ["add", "f.txt"])
        run_git(str(main), ["commit", "-m", "init"])

        # Create nested worktree inside main_nested
        nested_wt = main / "worktrees" / "feature-wt"
        run_git(str(main), ["worktree", "add", str(nested_wt), "-b", "nested-feat"])

        info = inspect_repo(str(main), str(self.root), do_fetch=False)
        # Should not be flagged as dirty due to untracked worktrees/ folder
        self.assertFalse(info["is_dirty"])
        self.assertEqual(info["dirty_files"], [])
        self.assertFalse(info["is_worktree"])
        self.assertEqual(len(info["worktrees"]), 2)

    def test_inspect_worktree_remote_inheritance_and_topology(self):
        bare = self._init_bare_remote(self.root / "remote_wt.git")
        main = self._init_repo(self.root / "main_remote")
        (main / "f.txt").write_text("main\n")
        run_git(str(main), ["add", "f.txt"])
        run_git(str(main), ["commit", "-m", "init"])
        run_git(str(main), ["remote", "add", "origin", str(bare)])
        run_git(str(main), ["push", "-u", "origin", "main"])

        # Add linked worktree
        wt = self.root / "wt_remote"
        run_git(str(main), ["worktree", "add", str(wt), "-b", "wt-branch"])

        info_wt = inspect_repo(str(wt), str(self.root), do_fetch=False)
        self.assertTrue(info_wt["is_worktree"])
        self.assertEqual(info_wt["worktree_main_path"], str(main.resolve()))
        self.assertEqual(info_wt["remote_url"], str(bare.resolve()))
        self.assertNotEqual(info_wt["status"], "NO_REMOTE")

    def test_inspect_submodules_status(self):
        sub = self._init_repo(self.root / "sub_pkg")
        (sub / "lib.py").write_text("print('lib')\n")
        run_git(str(sub), ["add", "lib.py"])
        run_git(str(sub), ["commit", "-m", "sub init"])

        parent = self._init_repo(self.root / "parent_pkg")
        (parent / "main.py").write_text("print('main')\n")
        run_git(str(parent), ["add", "main.py"])
        run_git(str(parent), ["commit", "-m", "parent init"])
        run_git(str(parent), ["-c", "protocol.file.allow=always", "submodule", "add", str(sub), "vendor/pkg"])
        run_git(str(parent), ["commit", "-m", "add vendor/pkg"])

        info = inspect_repo(str(parent), str(self.root), do_fetch=False)
        self.assertEqual(len(info["submodules"]), 1)
        sub_info = info["submodules"][0]
        self.assertEqual(sub_info["path"], "vendor/pkg")
        self.assertEqual(sub_info["status"], "UP_TO_DATE")

        # Make commit in sub without committing pointer in parent
        (parent / "vendor" / "pkg" / "lib.py").write_text("print('lib v2')\n")
        run_git(str(parent / "vendor" / "pkg"), ["commit", "-am", "sub update"])

        info_modified = inspect_repo(str(parent), str(self.root), do_fetch=False)
        self.assertEqual(len(info_modified["submodules"]), 1)
        self.assertEqual(info_modified["submodules"][0]["status"], "MODIFIED")

    def test_pull_repo_skips_active_worktree_branch(self):
        bare = self._init_bare_remote(self.root / "pull_wt_remote.git")
        main = self._init_repo(self.root / "main_pull_wt")
        (main / "f.txt").write_text("1\n")
        run_git(str(main), ["add", "f.txt"])
        run_git(str(main), ["commit", "-m", "1"])
        run_git(str(main), ["remote", "add", "origin", str(bare)])
        run_git(str(main), ["push", "-u", "origin", "main"])

        # Create feat branch and push
        run_git(str(main), ["checkout", "-b", "feat"])
        (main / "f2.txt").write_text("feat1\n")
        run_git(str(main), ["add", "f2.txt"])
        run_git(str(main), ["commit", "-m", "feat1"])
        run_git(str(main), ["push", "-u", "origin", "feat"])
        run_git(str(main), ["checkout", "main"])

        # Create linked worktree with feat
        wt = self.root / "wt_pull"
        run_git(str(main), ["worktree", "add", str(wt), "feat"])

        # Commit directly to remote via secondary clone
        clone = self.root / "secondary_clone"
        run_git(str(self.root), ["clone", "-b", "feat", str(bare), str(clone)])
        run_git(str(clone), ["config", "user.email", "test@example.com"])
        run_git(str(clone), ["config", "user.name", "Test User"])
        (clone / "f3.txt").write_text("feat2\n")
        run_git(str(clone), ["add", "f3.txt"])
        run_git(str(clone), ["commit", "-m", "feat2"])
        run_git(str(clone), ["push", "origin", "feat"])

        # Fetch in main
        run_git(str(main), ["fetch", "origin"])

        # Dry run in main should isolate feat
        res_dry = pull_repo(str(main), ff_only=True, sync_branches=True, dry_run=True)
        self.assertTrue(res_dry["success"])
        self.assertIn("feat", res_dry.get("worktree_isolated", {}))
        self.assertNotIn("feat", res_dry["aux_branches"])

        # Pull in main should not crash trying to fetch into feat checked out in wt
        res = pull_repo(str(main), ff_only=True, sync_branches=True)
        self.assertTrue(res["success"])
        # feat should be safely skipped from aux_branches and recorded in worktree_isolated
        self.assertNotIn("feat", res["aux_branches"])
        self.assertIn("feat", res.get("worktree_isolated", {}))
        self.assertEqual(res["worktree_isolated"]["feat"], str(wt.resolve()))
        self.assertFalse(res.get("is_worktree"))

        # Pull directly in the secondary worktree should succeed and update feat
        res_wt = pull_repo(str(wt), ff_only=True)
        self.assertTrue(res_wt["success"])
        self.assertTrue(res_wt["is_worktree"])
        self.assertEqual(res_wt["branch"], "feat")
        self.assertEqual(res_wt["worktree_main_path"], str(main.resolve()))

    def test_worktree_auxiliary_branch_conflict_isolation_and_granular_progress(self):
        """Verify bulk pull isolates auxiliary branches active in secondary worktrees and reports granular progress."""
        bare = self._init_bare_remote(self.root / "granular_wt_remote.git")
        main_repo = self._init_repo(self.root / "main_repo")
        (main_repo / "base.txt").write_text("base\n")
        run_git(str(main_repo), ["add", "base.txt"])
        run_git(str(main_repo), ["commit", "-m", "base commit"])
        run_git(str(main_repo), ["remote", "add", "origin", str(bare)])
        run_git(str(main_repo), ["push", "-u", "origin", "main"])

        # Create topic-a branch
        run_git(str(main_repo), ["checkout", "-b", "topic-a"])
        (main_repo / "topic_a.txt").write_text("topic-a v1\n")
        run_git(str(main_repo), ["add", "topic_a.txt"])
        run_git(str(main_repo), ["commit", "-m", "topic-a v1"])
        run_git(str(main_repo), ["push", "-u", "origin", "topic-a"])
        run_git(str(main_repo), ["checkout", "main"])

        # Create secondary worktree for topic-a
        wt = self.root / "wt_topic_a"
        run_git(str(main_repo), ["worktree", "add", str(wt), "topic-a"])

        # Remote commits on topic-a via separate clone
        clone = self.root / "tmp_clone"
        run_git(str(self.root), ["clone", "-b", "topic-a", str(bare), str(clone)])
        run_git(str(clone), ["config", "user.email", "test@example.com"])
        run_git(str(clone), ["config", "user.name", "Test User"])
        (clone / "topic_a_update.txt").write_text("topic-a v2\n")
        run_git(str(clone), ["add", "topic_a_update.txt"])
        run_git(str(clone), ["commit", "-m", "topic-a v2"])
        run_git(str(clone), ["push", "origin", "topic-a"])

        # Also push a commit on main
        run_git(str(clone), ["checkout", "main"])
        (clone / "main_update.txt").write_text("main update\n")
        run_git(str(clone), ["add", "main_update.txt"])
        run_git(str(clone), ["commit", "-m", "main update"])
        run_git(str(clone), ["push", "origin", "main"])

        # Fetch in main_repo so remotes/origin/* are updated
        run_git(str(main_repo), ["fetch", "origin"])

        # 1. Test JSON output for worktree isolation metadata in dry-run mode
        buf_json = io.StringIO()
        with patch("sys.stdout", buf_json):
            ret_json = main(["--root", str(self.root), "pull", "--all", "--sync-branches", "--dry-run", "--json"])
        self.assertEqual(ret_json, 0)
        data = json.loads(buf_json.getvalue())
        self.assertTrue(isinstance(data, list))
        # Find entry for main_repo
        main_entry = next((e for e in data if e.get("repo_rel") == "main_repo"), None)
        self.assertIsNotNone(main_entry)
        self.assertIn("topic-a", main_entry.get("worktree_isolated", {}))
        self.assertFalse(main_entry.get("is_worktree", True))

        # Find entry for wt_topic_a
        wt_entry = next((e for e in data if e.get("repo_rel") == "wt_topic_a"), None)
        self.assertIsNotNone(wt_entry)
        self.assertTrue(wt_entry.get("is_worktree", False))
        self.assertEqual(wt_entry.get("worktree_main_path"), str(main_repo.resolve()))

        # 2. Test CLI cmd_pull actual execution text output
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            ret = main(["--root", str(self.root), "pull", "--all", "--sync-branches"])
        self.assertEqual(ret, 0)
        output = buf.getvalue()
        # Output must report worktree tag and branch isolation
        self.assertIn("isolada", output)
        self.assertIn("topic-a", output)
        self.assertIn("[wt_topic_a] [worktree]", output)

    def test_discover_repos_pruning_tool_dirs(self):
        # 1. Normal repos
        r1 = self._init_repo(self.root / "r1")
        (r1 / "file.txt").write_text("r1")
        run_git(str(r1), ["add", "file.txt"])
        run_git(str(r1), ["commit", "-m", "init"])

        # 2. Fake repos inside tool directories under root
        for tool_dir in [".multigravity", ".agent", ".claude", ".git-sync", ".gemini", ".idea", ".vscode"]:
            tool_repo = self.root / tool_dir / "hidden_repo"
            tool_repo.mkdir(parents=True, exist_ok=True)
            run_git(str(tool_repo), ["init", "-b", "main"])

        # 3. Worktree nested inside a tool directory within a repo
        mg_wt = r1 / ".multigravity" / "worktrees" / "ghost_wt"
        mg_wt.parent.mkdir(parents=True, exist_ok=True)
        run_git(str(r1), ["worktree", "add", str(mg_wt), "-b", "ghost_branch"])

        # 4. Valid sibling worktree outside tool dirs
        valid_wt = self.root / "r1_valid_wt"
        run_git(str(r1), ["worktree", "add", str(valid_wt), "-b", "valid_branch"])

        discovered = discover_repos(str(self.root))
        expected = sorted([str(r1.resolve()), str(valid_wt.resolve())])
        self.assertEqual(discovered, expected)
        # Ensure no tool directories were crawled
        for path in discovered:
            self.assertNotIn(".multigravity", path)
            self.assertNotIn(".agent", path)
            self.assertNotIn(".claude", path)
            self.assertNotIn(".git-sync", path)
            self.assertNotIn(".gemini", path)

    def test_status_json_compact_and_summary_flags(self):
        repo = self._init_repo(self.root / "active_repo")
        (repo / "f.txt").write_text("hello\n")
        run_git(str(repo), ["add", "f.txt"])
        run_git(str(repo), ["commit", "-m", "initial"])

        # 1. Default --json has 'items'
        buf_full = io.StringIO()
        with patch("sys.stdout", buf_full):
            ret = main(["--root", str(self.root), "status", "--no-fetch", "--json"])
        self.assertEqual(ret, 0)
        data_full = json.loads(buf_full.getvalue())
        self.assertIn("items", data_full)
        self.assertEqual(len(data_full["items"]), 1)
        self.assertEqual(data_full["total"], 1)
        self.assertIn("pull_pending", data_full)
        self.assertIn("push_pending", data_full)
        self.assertIn("dirty", data_full)
        self.assertIn("diverged", data_full)

        # 2. --compact --json omits 'items'
        buf_compact = io.StringIO()
        with patch("sys.stdout", buf_compact):
            ret = main(["--root", str(self.root), "status", "--no-fetch", "--compact", "--json"])
        self.assertEqual(ret, 0)
        data_compact = json.loads(buf_compact.getvalue())
        self.assertNotIn("items", data_compact)
        self.assertEqual(data_compact["total"], 1)
        self.assertIn("pull_pending", data_compact)
        self.assertIn("push_pending", data_compact)
        self.assertIn("dirty", data_compact)
        self.assertIn("diverged", data_compact)
        self.assertIn("worktrees", data_compact)
        self.assertIn("has_submodules", data_compact)
        self.assertIn("no_remote", data_compact)
        self.assertIn("errors", data_compact)

        # 3. --summary --json alias omits 'items'
        buf_summary = io.StringIO()
        with patch("sys.stdout", buf_summary):
            ret = main(["--root", str(self.root), "status", "--no-fetch", "--summary", "--json"])
        self.assertEqual(ret, 0)
        data_summary = json.loads(buf_summary.getvalue())
        self.assertNotIn("items", data_summary)
        self.assertEqual(data_summary, data_compact)

        # 4. Text mode with --compact outputs single summary line
        buf_text_compact = io.StringIO()
        with patch("sys.stdout", buf_text_compact):
            ret = main(["--root", str(self.root), "status", "--no-fetch", "--compact"])
        self.assertEqual(ret, 0)
        out_text = buf_text_compact.getvalue()
        self.assertIn("Total: 1 repositórios", out_text)
        self.assertNotIn("STATUS          REPOSITÓRIO", out_text)

    def test_status_compact_json_empty_root(self):
        empty_dir = self.root / "empty_workspace"
        empty_dir.mkdir(parents=True, exist_ok=True)

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            ret = main(["--root", str(empty_dir), "status", "--no-fetch", "--compact", "--json"])
        self.assertEqual(ret, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["total"], 0)
        self.assertNotIn("items", data)
        self.assertEqual(data["pull_pending"], [])
        self.assertEqual(data["push_pending"], [])
        self.assertEqual(data["dirty"], [])
        self.assertEqual(data["no_remote"], [])
        self.assertEqual(data["errors"], [])

    def test_inspect_no_remote_preserves_branch_and_dirty(self):
        """Verify repositories without remote still capture local branch and dirty status."""
        local_repo = self._init_repo(self.root / "local_unconfigured")
        run_git(str(local_repo), ["checkout", "-b", "feature/offline"])
        (local_repo / "offline_note.txt").write_text("work in progress\n")

        # Repository has no remote, custom branch, and untracked file
        info = inspect_repo(str(local_repo), str(self.root), do_fetch=False)
        self.assertEqual(info["status"], "NO_REMOTE")
        self.assertEqual(info["branch"], "feature/offline")
        self.assertTrue(info["is_dirty"])
        self.assertTrue(any("offline_note.txt" in f for f in info["dirty_files"]))

    def test_status_cli_table_and_json_no_remote_semantics(self):
        """Verify CLI table avoids 'sincronizado' oxymoron and JSON segregates no_remote from errors."""
        unconfigured_repo = self._init_repo(self.root / "pkg_offline")
        run_git(str(unconfigured_repo), ["checkout", "-b", "main"])

        # 1. Text table formatting
        buf_text = io.StringIO()
        with patch("sys.stdout", buf_text):
            ret = main(["--root", str(self.root), "status", "--no-fetch"])
        self.assertEqual(ret, 0)
        output = buf_text.getvalue()
        self.assertIn("sem remote configurado", output)
        self.assertNotIn("❌ SEM REMOTE   pkg_offline              main         sincronizado", output)

        # 2. JSON summary segregation
        buf_json = io.StringIO()
        with patch("sys.stdout", buf_json):
            ret_json = main(["--root", str(self.root), "status", "--no-fetch", "--compact", "--json"])
        self.assertEqual(ret_json, 0)
        data = json.loads(buf_json.getvalue())
        self.assertIn("pkg_offline", data["no_remote"])
        self.assertEqual(data["errors"], [])

    def test_conventional_commit_classifier_categories(self):
        """Verify deterministic Conventional Commit classification into Keep-a-Changelog buckets."""
        # Added (feat, feature, add)
        c1 = classify_commit("a1b2c3d", "feat(api): add v2 telemetry endpoint")
        self.assertEqual(c1.type, "feat")
        self.assertEqual(c1.scope, "api")
        self.assertEqual(c1.description, "add v2 telemetry endpoint")
        self.assertEqual(c1.category, "Added")
        self.assertFalse(c1.is_breaking)

        # Fixed (fix, bugfix, hotfix)
        c2 = classify_commit("b2c3d4e", "fix(engine): handle empty diff in hunk chunker")
        self.assertEqual(c2.type, "fix")
        self.assertEqual(c2.scope, "engine")
        self.assertEqual(c2.category, "Fixed")
        self.assertFalse(c2.is_breaking)

        # Changed (perf, refactor, docs, chore, style, build, ci, test)
        c3 = classify_commit("c3d4e5f", "perf(cli): speed up repo discovery walk")
        self.assertEqual(c3.category, "Changed")

        c4 = classify_commit("d4e5f6a", "refactor(config): simplify source provenance tracking")
        self.assertEqual(c4.category, "Changed")

        c5 = classify_commit("e5f6a7b", "docs(agent): update NOTES with changelog schema")
        self.assertEqual(c5.category, "Changed")

        c6 = classify_commit("f6a7b8c", "chore(deps): update test fixtures")
        self.assertEqual(c6.category, "Changed")

        # Removed (revert, remove, deprecate, delete)
        c7 = classify_commit("0123456", "revert(core): rollback broken auth interceptor")
        self.assertEqual(c7.category, "Removed")

        c8 = classify_commit("1234567", "deprecate(api): mark v1 endpoint as obsolete")
        self.assertEqual(c8.category, "Removed")

        # Security (sec, security, vuln)
        c9 = classify_commit("2345678", "sec(auth): sanitize bearer token in stdout logs")
        self.assertEqual(c9.category, "Security")
        self.assertEqual(c9.scope, "auth")

    def test_conventional_commit_breaking_changes(self):
        """Verify breaking changes are flagged via '!' in prefix or body footer."""
        # 1. Breaking via '!' in header
        c1 = classify_commit("3456789", "feat!: drop python 3.9 support")
        self.assertTrue(c1.is_breaking)
        self.assertEqual(c1.category, "Breaking")
        self.assertEqual(c1.type, "feat")

        # 2. Breaking via '!' with scope
        c2 = classify_commit("4567890", "fix(auth)!: require signed tokens for all routes")
        self.assertTrue(c2.is_breaking)
        self.assertEqual(c2.category, "Breaking")
        self.assertEqual(c2.scope, "auth")

        # 3. Breaking via body BREAKING CHANGE: footer
        body3 = "Refactors response schemas.\n\nBREAKING CHANGE: payload key 'user_id' renamed to 'id'."
        c3 = classify_commit("5678901", "feat(api): overhaul user response payload", body=body3)
        self.assertTrue(c3.is_breaking)
        self.assertEqual(c3.category, "Breaking")
        self.assertIn("payload key 'user_id' renamed to 'id'", c3.breaking_note or "")

        # 4. Breaking via body BREAKING-CHANGE: footer
        body4 = "Removes old flag.\n\nBREAKING-CHANGE: --legacy-mode is no longer supported."
        c4 = classify_commit("6789012", "refactor(cli): remove legacy mode", body=body4)
        self.assertTrue(c4.is_breaking)
        self.assertEqual(c4.category, "Breaking")
        self.assertIn("--legacy-mode is no longer supported", c4.breaking_note or "")

    def test_non_conventional_commit_heuristic_fallback(self):
        """Verify non-conventional commit messages fall back to intuitive heuristics."""
        c1 = classify_commit("7890123", "Add support for dark mode theme")
        self.assertEqual(c1.category, "Added")

        c2 = classify_commit("8901234", "Fix race condition in socket multiplexer")
        self.assertEqual(c2.category, "Fixed")

        c3 = classify_commit("9012345", "Remove obsolete helper functions")
        self.assertEqual(c3.category, "Removed")

        c4 = classify_commit("0123456", "Security patch for credential leak")
        self.assertEqual(c4.category, "Security")

        c5 = classify_commit("1234567", "Update README.md with badges")
        self.assertEqual(c5.category, "Changed")

        c6 = classify_commit("2345678", "Breaking change: drop deprecated endpoints")
        self.assertTrue(c6.is_breaking)
        self.assertEqual(c6.category, "Breaking")

    def test_parse_commit_log_unit_separator(self):
        """Verify parsing git log output delimited with null and unit separator."""
        log_output = (
            "a1b2c3d\x00feat(cli): add --changelog flag\x00Adds Conventional Commit triage\x1f"
            "b2c3d4e\x00fix!: prevent panic on null ref\x00BREAKING CHANGE: null no longer accepted\x1f"
            "c3d4e5f\x00docs: update changelog guide\x00\x1f"
        )
        commits = parse_commit_log(log_output)
        self.assertEqual(len(commits), 3)

        self.assertEqual(commits[0].hash, "a1b2c3d")
        self.assertEqual(commits[0].category, "Added")
        self.assertFalse(commits[0].is_breaking)

        self.assertEqual(commits[1].hash, "b2c3d4e")
        self.assertEqual(commits[1].category, "Breaking")
        self.assertTrue(commits[1].is_breaking)

        self.assertEqual(commits[2].hash, "c3d4e5f")
        self.assertEqual(commits[2].category, "Changed")
        self.assertFalse(commits[2].is_breaking)

        # Empty or whitespace output
        self.assertEqual(parse_commit_log(""), [])
        self.assertEqual(parse_commit_log("\n  \n"), [])

    def test_summarize_and_format_changelog(self):
        """Verify category summarization and canonical ordering."""
        commits = [
            classify_commit("1", "feat!: breaking api change"),
            classify_commit("2", "feat: new feature 1"),
            classify_commit("3", "feat: new feature 2"),
            classify_commit("4", "fix: resolve bug 1"),
            classify_commit("5", "docs: update docs"),
            classify_commit("6", "sec: patch leak"),
        ]
        summary = summarize_commit_categories(commits)
        self.assertEqual(summary["Breaking"], 1)
        self.assertEqual(summary["Security"], 1)
        self.assertEqual(summary["Added"], 2)
        self.assertEqual(summary["Fixed"], 1)
        self.assertEqual(summary["Changed"], 1)

        summary_str = format_changelog_summary(summary)
        self.assertEqual(summary_str, "Breaking: 1, Security: 1, Added: 2, Fixed: 1, Changed: 1")
        self.assertEqual(format_changelog_summary({}), "")

    def test_get_repo_pending_changelog_real_git(self):
        """Verify live git commit classification between local and upstream."""
        origin = self._init_repo(self.root / "origin_changelog")
        run_git(str(origin), ["checkout", "-b", "main"])
        (origin / "init.txt").write_text("initial\n")
        run_git(str(origin), ["add", "init.txt"])
        run_git(str(origin), ["commit", "-m", "chore: initial commit"])

        # Clone to local
        local = self.root / "local_changelog"
        run_git(str(self.root), ["clone", str(origin), str(local)])

        # Upstream adds incoming commits
        (origin / "feat1.txt").write_text("feature 1\n")
        run_git(str(origin), ["add", "feat1.txt"])
        run_git(str(origin), ["commit", "-m", "feat(api): add endpoint v2"])

        (origin / "fix1.txt").write_text("fix 1\n")
        run_git(str(origin), ["add", "fix1.txt"])
        run_git(str(origin), ["commit", "-m", "fix(db): reconnect on timeout"])

        # Local adds outgoing commits (diverged)
        (local / "feat_local.txt").write_text("local feature\n")
        run_git(str(local), ["add", "feat_local.txt"])
        run_git(str(local), ["commit", "-m", "feat!: breaking schema revamp"])

        (local / "docs_local.txt").write_text("local docs\n")
        run_git(str(local), ["add", "docs_local.txt"])
        run_git(str(local), ["commit", "-m", "docs: update install steps"])

        # Fetch remote refs in local repo
        run_git(str(local), ["fetch", "origin"])

        # Inspect repo with changelog=True
        info = inspect_repo(str(local), str(self.root), do_fetch=False, changelog=True)
        self.assertEqual(info["status"], "DIVERGED")
        self.assertEqual(info["ahead"], 2)
        self.assertEqual(info["behind"], 2)

        cl = info["changelog"]
        self.assertIsNotNone(cl)
        self.assertEqual(len(cl["incoming"]), 2)
        self.assertEqual(len(cl["outgoing"]), 2)
        self.assertEqual(cl["incoming_categories"], {"Added": 1, "Fixed": 1})
        self.assertEqual(cl["outgoing_categories"], {"Breaking": 1, "Changed": 1})
        self.assertTrue(cl["has_breaking"])
        self.assertEqual(cl["outgoing_breaking"], 1)
        self.assertEqual(cl["incoming_breaking"], 0)

    def test_status_cli_changelog_table_and_json(self):
        """Verify CLI status --changelog renders table summaries and structured JSON."""
        origin = self._init_repo(self.root / "srv_changelog")
        run_git(str(origin), ["checkout", "-b", "main"])
        (origin / "base.txt").write_text("base\n")
        run_git(str(origin), ["add", "base.txt"])
        run_git(str(origin), ["commit", "-m", "chore: base"])

        local = self.root / "app_changelog"
        run_git(str(self.root), ["clone", str(origin), str(local)])

        # Add commit in local
        (local / "patch.txt").write_text("patch\n")
        run_git(str(local), ["add", "patch.txt"])
        run_git(str(local), ["commit", "-m", "feat(ui): add compact dashboard"])

        # 1. Text table with --changelog
        buf_text = io.StringIO()
        with patch("sys.stdout", buf_text):
            ret = main(["--root", str(self.root), "status", "--no-fetch", "--changelog"])
        self.assertEqual(ret, 0)
        table_output = buf_text.getvalue()
        self.assertIn("app_changelog", table_output)
        self.assertIn("outgoing[Added: 1]", table_output)
        self.assertIn("feat(ui): add compact dashboard", table_output)

        # 2. JSON compact with --changelog
        buf_json = io.StringIO()
        with patch("sys.stdout", buf_json):
            ret_json = main(["--root", str(self.root), "status", "--no-fetch", "--changelog", "--compact", "--json"])
        self.assertEqual(ret_json, 0)
        compact_data = json.loads(buf_json.getvalue())
        self.assertIn("app_changelog", compact_data["changelogs"])
        self.assertEqual(compact_data["changelogs"]["app_changelog"]["outgoing_categories"], {"Added": 1})

        # 3. JSON full items with --changelog
        buf_items = io.StringIO()
        with patch("sys.stdout", buf_items):
            ret_items = main(["--root", str(self.root), "status", "--no-fetch", "--changelog", "--json"])
        self.assertEqual(ret_items, 0)
        items_data = json.loads(buf_items.getvalue())
        app_item = next(item for item in items_data["items"] if item["rel_path"] == "app_changelog")
        self.assertIn("changelog", app_item)
        self.assertEqual(len(app_item["changelog"]["outgoing"]), 1)
        self.assertEqual(app_item["changelog"]["outgoing"][0]["type"], "feat")
        self.assertEqual(app_item["changelog"]["outgoing"][0]["scope"], "ui")

    def test_dashboard_changelog_action(self):
        """Verify InteractiveDashboard action_inspect_changelog renders changelog details."""
        repo_dict = {
            "name": "mock-repo",
            "rel_path": "mock-repo",
            "path": str(self.root),
            "branch": "main",
            "status": "PUSH_PENDING",
            "ahead": 1,
            "behind": 0,
            "upstream": "origin/main",
            "changelog": {
                "incoming": [],
                "outgoing": [
                    {
                        "hash": "1a2b3c4",
                        "subject": "feat(core)!: major breaking redesign",
                        "type": "feat",
                        "scope": "core",
                        "description": "major breaking redesign",
                        "is_breaking": True,
                        "category": "Breaking",
                    }
                ],
                "incoming_categories": {},
                "outgoing_categories": {"Breaking": 1},
                "incoming_breaking": 0,
                "outgoing_breaking": 1,
                "has_breaking": True,
            },
        }
        buf = io.StringIO()
        dashboard = InteractiveDashboard(
            root=str(self.root),
            repos=[repo_dict],
            stream=buf,
            key_reader=lambda: "q",
        )
        report = dashboard.action_inspect_changelog()
        self.assertIn("Changelog: mock-repo", report)
        self.assertIn("major breaking redesign", report)
        self.assertIn("[BREAKING]", report)
        self.assertIn("Changelog de 'mock-repo' inspecionado", dashboard.last_message)


if __name__ == "__main__":
    unittest.main()
