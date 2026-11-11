import unittest

from git_sync.ui.formatters import build_status_summary

CORE_KEYS = {
    "root",
    "total",
    "pull_pending",
    "push_pending",
    "dirty",
    "diverged",
    "worktrees",
    "has_submodules",
    "no_remote",
    "errors",
}

PRUNABLE_KEYS = {
    "stale_worktrees",
    "submodule_drift",
    "shallow_submodules",
    "has_lfs",
    "lfs_pointers",
    "lfs_warnings",
    "sparse_checkout",
    "sparse_cone_warnings",
    "no_upstream",
    "offline",
    "multi_remote",
    "worktree_health",
    "remediations",
}


def _repo(rel_path: str, status: str = "UP_TO_DATE", **extra):
    rec = {"rel_path": rel_path, "path": f"/ws/{rel_path}", "status": status, "is_dirty": False}
    rec.update(extra)
    return rec


class StatusSummaryTests(unittest.TestCase):
    def test_push_pending_includes_aux_push_pending(self):
        results = [
            _repo("a", "PUSH_PENDING"),
            _repo("b", "AUX_PUSH_PENDING"),
            _repo("c", "UP_TO_DATE"),
        ]
        for compact in (True, False):
            summary = build_status_summary(results, root="/ws", compact=compact)
            self.assertEqual(summary["push_pending"], ["a", "b"])

    def test_compact_prunes_empty_secondary_keys_and_keeps_core(self):
        results = [_repo("a"), _repo("b", "PUSH_PENDING")]
        summary = build_status_summary(results, root="/ws", compact=True)
        self.assertTrue(CORE_KEYS <= set(summary))
        self.assertFalse(PRUNABLE_KEYS & set(summary))
        self.assertNotIn("items", summary)

    def test_compact_empty_results_keeps_core_keys(self):
        summary = build_status_summary([], root="/ws", compact=True)
        self.assertEqual(set(summary), CORE_KEYS)

    def test_compact_retains_populated_secondary_keys(self):
        results = [
            _repo("a", "NO_UPSTREAM"),
            _repo("b", "UP_TO_DATE", stale_worktrees=["/ws/b-wt"]),
            _repo("c", "AUX_PUSH_PENDING", remediation="git-sync push c --sync-branches", action_required="push_aux"),
        ]
        summary = build_status_summary(results, root="/ws", compact=True)
        self.assertEqual(summary["no_upstream"], ["a"])
        self.assertEqual(summary["stale_worktrees"], ["b"])
        self.assertIn("worktree_health", summary)
        self.assertEqual(summary["worktree_health"]["stale_worktrees"], ["b"])
        self.assertTrue(any(r["repo"] == "c" for r in summary["remediations"]))
        self.assertNotIn("offline", summary)
        self.assertNotIn("has_lfs", summary)

    def test_full_includes_all_keys_and_items(self):
        results = [_repo("a")]
        summary = build_status_summary(results, root="/ws", compact=False)
        self.assertTrue((CORE_KEYS | PRUNABLE_KEYS) <= set(summary))
        self.assertEqual(summary["items"], results)


if __name__ == "__main__":
    unittest.main()
