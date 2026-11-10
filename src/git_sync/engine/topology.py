"""
Repository topology, worktrees, submodules, sparse-checkout, and lock file management.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from git_sync.telemetry import BulkCache

DEFAULT_TIMEOUT: int = 5
DEFAULT_WORKERS: int = 8


def _call_run_git(repo_path: str, args: List[str], timeout: int = DEFAULT_TIMEOUT) -> Any:
    from git_sync.engine.git import run_git
    return run_git(repo_path, args, timeout=timeout)


def _call_run_git_with_retry(repo_path: str, args: List[str], timeout: int = DEFAULT_TIMEOUT, retries: int = 2) -> Any:
    from git_sync.engine.git import run_git_with_retry
    return run_git_with_retry(repo_path, args, timeout=timeout, retries=retries)


def _call_check_repo_dirty(repo_path: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[bool, List[str]]:
    from git_sync.engine.git import check_repo_dirty
    return check_repo_dirty(repo_path, timeout=timeout)


def _call_discover_repos(root_dir: str) -> List[str]:
    from git_sync.engine.git import discover_repos
    return discover_repos(root_dir)


def get_repo_git_dir(repo_path: str) -> Path:
    """
    Resolve the actual Git directory (.git directory or worktree/submodule gitdir).
    # INVARIANT: Supports standard repositories (.git/), worktrees, and submodules (.git file).
    """
    p = Path(repo_path) / ".git"
    if p.is_dir():
        return p
    if p.is_file():
        try:
            content = p.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                gitdir_rel = content.split(":", 1)[1].strip()
                gitdir = Path(repo_path) / gitdir_rel
                if gitdir.exists():
                    return gitdir.resolve()
        except Exception:
            pass
    return p


def is_shallow_repository(repo_path: str) -> bool:
    """
    Check whether repo_path is a shallow clone by inspecting the shallow boundary file in gitdir.
    # INVARIANT: Directly inspecting git_dir/shallow eliminates subprocess overhead during bulk audits.
    """
    git_dir = get_repo_git_dir(repo_path)
    if not git_dir.is_dir():
        return False
    if (git_dir / "shallow").is_file():
        return True
    # If git_dir has commondir (linked worktree), check the common gitdir for shallow file
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        try:
            target = commondir_file.read_text(encoding="utf-8").strip()
            common_dir = (git_dir / target).resolve()
            if (common_dir / "shallow").is_file():
                return True
        except Exception:
            pass
    return False


def is_path_in_cone(path: str, cone_patterns: List[str]) -> bool:
    """
    Check if a relative file path matches Git cone mode sparse-checkout rules:
    - Root-level files (no '/' in normalized path) are always included in cone.
    - A file is included if it resides within a configured cone directory,
      or within an intermediate directory leading to a configured sub-cone.
    # INVARIANT: Accurately reflects Git cone mode sparse pattern directory matching.
    """
    norm = path.replace("\\", "/").strip("/")
    if not norm or "/" not in norm:
        return True

    norm_parts = norm.split("/")
    file_dir = "/".join(norm_parts[:-1])

    for pat in cone_patterns:
        clean_pat = pat.replace("\\", "/").strip("/")
        if not clean_pat:
            continue
        if norm == clean_pat or norm.startswith(clean_pat + "/"):
            return True
        pat_parts = clean_pat.split("/")
        for i in range(1, len(pat_parts)):
            parent_dir = "/".join(pat_parts[:i])
            if file_dir == parent_dir:
                return True
    return False


def get_sparse_checkout_info(
    repo_path: str,
    dirty_files: Optional[List[str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Query git sparse-checkout configuration and active cone patterns for repo_path.
    Returns:
        enabled: bool (True if core.sparseCheckout is enabled)
        cone_mode: bool (True if core.sparseCheckoutCone is enabled)
        patterns: List[str] (Configured directory cones or patterns)
        pattern_count: int
        sparse_index: bool (True if index.sparse is enabled)
        out_of_cone_files: List[str] (Uncommitted files residing outside cone boundaries)
        warning: Optional[str] (Diagnostic warning if non-cone mode or out-of-cone modifications exist)
    # INVARIANT: Directly inspecting git config core.sparseCheckout avoids spawning unnecessary processes on standard repos.
    """
    p_sc = _call_run_git(repo_path, ["config", "--bool", "--get", "core.sparseCheckout"], timeout=timeout)
    enabled = (p_sc.returncode == 0 and p_sc.stdout.strip().lower() == "true")

    if not enabled:
        return {
            "enabled": False,
            "cone_mode": False,
            "patterns": [],
            "pattern_count": 0,
            "sparse_index": False,
            "out_of_cone_files": [],
            "warning": None,
        }

    p_cone = _call_run_git(repo_path, ["config", "--bool", "--get", "core.sparseCheckoutCone"], timeout=timeout)
    cone_mode = (p_cone.returncode == 0 and p_cone.stdout.strip().lower() == "true")

    p_idx = _call_run_git(repo_path, ["config", "--bool", "--get", "index.sparse"], timeout=timeout)
    sparse_index = (p_idx.returncode == 0 and p_idx.stdout.strip().lower() == "true")

    patterns: List[str] = []
    p_list = _call_run_git(repo_path, ["sparse-checkout", "list"], timeout=timeout)
    if p_list.returncode == 0:
        patterns = [l.strip() for l in p_list.stdout.splitlines() if l.strip()]
    else:
        git_dir = get_repo_git_dir(repo_path)
        sc_file = git_dir / "info" / "sparse-checkout"
        if sc_file.is_file():
            try:
                patterns = [l.strip() for l in sc_file.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            except Exception:
                patterns = []

    out_of_cone_files: List[str] = []
    if cone_mode and dirty_files:
        out_of_cone_files = [f for f in dirty_files if not is_path_in_cone(f, patterns)]

    warning: Optional[str] = None
    if not cone_mode:
        warning = "Sparse checkout is configured in legacy non-cone pattern mode"
    elif out_of_cone_files:
        warning = f"{len(out_of_cone_files)} uncommitted file(s) exist outside sparse cone patterns"
    elif not patterns:
        warning = "Sparse checkout cone pattern is empty (root files only)"

    return {
        "enabled": True,
        "cone_mode": cone_mode,
        "patterns": patterns,
        "pattern_count": len(patterns),
        "sparse_index": sparse_index,
        "out_of_cone_files": out_of_cone_files,
        "warning": warning,
    }


def reapply_sparse_checkout(
    repo_path: str,
    cone: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Safely reapply sparse-checkout filters on repo_path to enforce cone boundaries
    and prevent unbounded working tree checkouts or conflicts.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 prevents interactive prompts during sparse-checkout operations.
    """
    args = ["sparse-checkout", "reapply"]
    if cone:
        args.append("--cone")
    p = _call_run_git_with_retry(repo_path, args, timeout=timeout)
    ok = (p.returncode == 0)
    return {
        "repo": repo_path,
        "success": ok,
        "cone": cone,
        "stdout": p.stdout.strip(),
        "stderr": p.stderr.strip(),
    }


def find_repo_locks(repo_path: str, stale_threshold_seconds: float = 300.0) -> List[Dict[str, Any]]:
    """
    Find all active or stale Git lock files in a repository (.git/*.lock or worktree gitdir).
    # INVARIANT: Safely inspects lock files without altering repository state.
    """
    git_dir = get_repo_git_dir(repo_path)
    if not git_dir.is_dir():
        return []

    locks: List[Dict[str, Any]] = []
    now = time.time()

    candidate_locks = [
        "index.lock",
        "FETCH_HEAD.lock",
        "shallow.lock",
        "HEAD.lock",
        "config.lock",
        "packed-refs.lock",
        "commit-graph.lock",
    ]

    for name in candidate_locks:
        lock_path = git_dir / name
        if lock_path.exists():
            try:
                mtime = os.path.getmtime(lock_path)
                age = max(0.0, now - mtime)
                locks.append({
                    "repo": repo_path,
                    "lock_file": str(lock_path),
                    "lock_name": name,
                    "mtime": mtime,
                    "age_seconds": round(age, 2),
                    "stale": age >= stale_threshold_seconds,
                })
            except OSError:
                pass

    refs_heads = git_dir / "refs" / "heads"
    if refs_heads.is_dir():
        try:
            for ref_lock in refs_heads.glob("*.lock"):
                if ref_lock.is_file():
                    try:
                        mtime = os.path.getmtime(ref_lock)
                        age = max(0.0, now - mtime)
                        locks.append({
                            "repo": repo_path,
                            "lock_file": str(ref_lock),
                            "lock_name": f"refs/heads/{ref_lock.name}",
                            "mtime": mtime,
                            "age_seconds": round(age, 2),
                            "stale": age >= stale_threshold_seconds,
                        })
                    except OSError:
                        pass
        except Exception:
            pass

    return locks


def find_locked_repos(
    root_dir: str,
    repos: Optional[List[str]] = None,
    group: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    stale_threshold_seconds: float = 300.0,
) -> List[Dict[str, Any]]:
    """
    Scan repositories in root_dir for active or stale Git lock files (.git/*.lock).
    # INVARIANT: Identifies stale locks left by crashed or interrupted Git subshells.
    """
    target_repos = repos
    if target_repos is None:
        target_repos = _call_discover_repos(root_dir)
        if group:
            target_repos = [r for r in target_repos if os.path.relpath(r, root_dir).startswith(group)]

    locked: List[Dict[str, Any]] = []
    for r in target_repos:
        repo_locks = find_repo_locks(r, stale_threshold_seconds=stale_threshold_seconds)
        if repo_locks:
            for lk in repo_locks:
                lk["rel_path"] = os.path.relpath(r, root_dir)
                locked.append(lk)
    return locked


def recover_index_lock(
    repo_path: str,
    force: bool = False,
    max_age_seconds: float = 300.0,
) -> Tuple[bool, str]:
    """
    Safely recover from stale or crashed Git index locks (.git/index.lock).
    # INVARIANT: Preserves fresh locks (< max_age_seconds) unless force=True to prevent race conditions with running Git commands.
    """
    git_dir = get_repo_git_dir(repo_path)
    lock_file = git_dir / "index.lock"
    if not lock_file.exists():
        return True, "No index lock found"

    try:
        mtime = os.path.getmtime(lock_file)
        age = max(0.0, time.time() - mtime)
        if not force and age < max_age_seconds:
            return False, f"Index lock is active (age: {age:.1f}s < threshold {max_age_seconds:.1f}s)"

        lock_file.unlink()
        return True, f"Successfully removed {'stale ' if age >= max_age_seconds else ''}index lock '{lock_file.name}' (age: {age:.1f}s)"
    except Exception as exc:
        return False, f"Failed to remove index lock '{lock_file.name}': {exc}"


def recover_repo_locks(
    repo_path: str,
    force: bool = False,
    max_age_seconds: float = 300.0,
) -> Tuple[bool, List[str]]:
    """
    Remove all stale or forced Git lock files in a repository.
    # INVARIANT: Prevents stale lock contention while respecting recent active operations.
    """
    locks = find_repo_locks(repo_path, stale_threshold_seconds=max_age_seconds)
    if not locks:
        return True, ["No locks found"]

    cleared: List[str] = []
    errors: List[str] = []
    for lk in locks:
        lock_path = Path(lk["lock_file"])
        age = lk["age_seconds"]
        if not force and not lk["stale"]:
            errors.append(f"Skipping active lock '{lk['lock_name']}' (age: {age:.1f}s)")
            continue
        try:
            lock_path.unlink()
            cleared.append(f"Removed lock '{lk['lock_name']}' (age: {age:.1f}s)")
        except Exception as exc:
            errors.append(f"Failed to remove '{lk['lock_name']}': {exc}")

    success = len(errors) == 0
    return success, cleared if success else (cleared + errors)


def get_worktree_info(repo_path: str, timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """
    Query worktree topology for the given repository or linked worktree.
    Returns:
        is_worktree: bool (True if this is a secondary linked worktree)
        main_worktree: Optional[str] (path to main worktree)
        worktrees: List[Dict[str, Any]] (list of all worktrees in this repo family)
        checked_out_branches: Dict[str, str] (mapping of branch_name -> worktree_path)
    """
    proc = _call_run_git(repo_path, ["worktree", "list", "--porcelain"], timeout=timeout)
    if proc.returncode != 0:
        return {
            "is_worktree": False,
            "main_worktree": None,
            "worktrees": [],
            "checked_out_branches": {},
            "stale_worktrees": [],
        }

    worktrees: List[Dict[str, Any]] = []
    current_entry: Dict[str, Any] = {}

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            if current_entry:
                worktrees.append(current_entry)
                current_entry = {}
            continue

        if line.startswith("worktree "):
            if current_entry:
                worktrees.append(current_entry)
                current_entry = {}
            wt_path = line[len("worktree "):].strip()
            current_entry = {
                "path": os.path.abspath(wt_path),
                "head": "",
                "branch": "",
                "is_main": (len(worktrees) == 0),
                "is_prunable": False,
                "prunable_reason": None,
                "is_locked": False,
                "lock_reason": None,
            }
        elif line.startswith("HEAD ") and current_entry:
            current_entry["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch ") and current_entry:
            ref = line[len("branch "):].strip()
            current_entry["branch"] = ref.replace("refs/heads/", "")
        elif line == "detached" and current_entry:
            current_entry["branch"] = "HEAD (detached)"
        elif line.startswith("prunable") and current_entry:
            current_entry["is_prunable"] = True
            r = line[len("prunable"):].strip()
            current_entry["prunable_reason"] = r if r else "marked prunable"
        elif line.startswith("locked") and current_entry:
            current_entry["is_locked"] = True
            r = line[len("locked"):].strip()
            current_entry["lock_reason"] = r if r else "locked"

    if current_entry:
        worktrees.append(current_entry)

    # Validate physical existence of worktrees on disk
    for wt in worktrees:
        if not os.path.exists(wt["path"]):
            wt["is_prunable"] = True
            if not wt.get("prunable_reason"):
                wt["prunable_reason"] = "directory does not exist on disk"
        elif not wt.get("is_main"):
            git_file = os.path.join(wt["path"], ".git")
            if not os.path.exists(git_file):
                wt["is_prunable"] = True
                if not wt.get("prunable_reason"):
                    wt["prunable_reason"] = "missing .git file"

    stale_worktrees: List[Dict[str, Any]] = [w for w in worktrees if w.get("is_prunable")]

    norm_repo = os.path.abspath(repo_path)
    main_wt = worktrees[0]["path"] if worktrees else None
    is_secondary = (main_wt is not None and norm_repo != main_wt)

    # Check for orphaned worktree admin directories in primary .git/worktrees
    primary_path = main_wt if main_wt else norm_repo
    git_dir = get_repo_git_dir(primary_path)
    admin_worktrees_dir = Path(git_dir) / "worktrees"
    if admin_worktrees_dir.is_dir():
        known_paths = {w["path"] for w in worktrees}
        try:
            for entry_name in os.listdir(admin_worktrees_dir):
                admin_entry = admin_worktrees_dir / entry_name
                if not admin_entry.is_dir():
                    continue
                gitdir_file = admin_entry / "gitdir"
                if not gitdir_file.exists():
                    orphaned = {
                        "path": str(admin_entry),
                        "head": "",
                        "branch": entry_name,
                        "is_main": False,
                        "is_prunable": True,
                        "prunable_reason": "orphaned worktree admin metadata (missing gitdir)",
                        "is_locked": False,
                        "lock_reason": None,
                    }
                    if not any(sw.get("branch") == entry_name for sw in stale_worktrees):
                        stale_worktrees.append(orphaned)
                else:
                    try:
                        target = gitdir_file.read_text(encoding="utf-8", errors="replace").strip()
                        target_dir = os.path.dirname(target) if target.endswith(".git") else target
                        if target_dir not in known_paths and not os.path.exists(target):
                            orphaned = {
                                "path": target_dir,
                                "head": "",
                                "branch": entry_name,
                                "is_main": False,
                                "is_prunable": True,
                                "prunable_reason": "orphaned worktree admin metadata (target deleted)",
                                "is_locked": False,
                                "lock_reason": None,
                            }
                            if not any(sw.get("branch") == entry_name for sw in stale_worktrees):
                                stale_worktrees.append(orphaned)
                    except OSError:
                        pass
        except OSError:
            pass

    checked_out: Dict[str, str] = {}
    for wt in worktrees:
        if wt.get("branch") and wt["branch"] != "HEAD (detached)":
            checked_out[wt["branch"]] = wt["path"]

    return {
        "is_worktree": is_secondary,
        "main_worktree": main_wt if is_secondary else None,
        "worktrees": worktrees,
        "checked_out_branches": checked_out,
        "stale_worktrees": stale_worktrees,
    }


def prune_repo_worktrees(
    repo_path: str,
    dry_run: bool = False,
    expire: str = "now",
    timeout: int = 10,
    cache: Optional[BulkCache] = None,
    root_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Prune stale and orphaned worktree administrative metadata and audit linked worktrees for dirty state.
    # INVARIANT: Preserves active dirty linked worktrees and warns without deleting working files.
    """
    rel_path = os.path.relpath(repo_path, root_dir) if root_dir else os.path.basename(repo_path)

    # 1. Audit worktree topology and warn on dirty linked worktrees
    wt_info = get_worktree_info(repo_path, timeout=timeout)
    worktrees = wt_info.get("worktrees", [])
    dirty_linked: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for wt in worktrees:
        if not wt.get("is_main") and os.path.exists(wt["path"]):
            is_dirty, dirty_files = _call_check_repo_dirty(wt["path"], timeout=timeout)
            if is_dirty:
                dirty_entry = {
                    "path": wt["path"],
                    "branch": wt.get("branch", ""),
                    "dirty_files": dirty_files,
                }
                dirty_linked.append(dirty_entry)
                warnings.append(
                    f"Linked worktree '{wt['path']}' ({wt.get('branch', 'unknown')}) has uncommitted changes ({len(dirty_files)} dirty files)"
                )

    # 2. Execute git worktree prune
    cmd = ["worktree", "prune", "-v"]
    if expire:
        cmd.extend(["--expire", expire])
    if dry_run:
        cmd.append("--dry-run")

    proc = _call_run_git(repo_path, cmd, timeout=timeout)
    pruned_entries: List[Dict[str, Any]] = []

    combined_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    for line in combined_output.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.search(r"Removing (?:worktrees/)?([^:]+):\s*(.*)", line)
        if m:
            entry_name = m.group(1).strip()
            reason = m.group(2).strip()
            pruned_entries.append({
                "name": entry_name,
                "reason": reason,
                "dry_run": dry_run,
            })

    # 3. Clean any orphaned admin directories that git worktree prune skipped
    primary_wt = wt_info.get("main_worktree") or repo_path
    git_dir = get_repo_git_dir(primary_wt)
    admin_worktrees_dir = Path(git_dir) / "worktrees"
    if admin_worktrees_dir.is_dir():
        known_pruned = {p["name"] for p in pruned_entries}
        try:
            for entry_name in os.listdir(admin_worktrees_dir):
                if entry_name in known_pruned:
                    continue
                admin_entry = admin_worktrees_dir / entry_name
                if not admin_entry.is_dir():
                    continue
                gitdir_file = admin_entry / "gitdir"
                orphaned = False
                reason = ""
                if not gitdir_file.exists():
                    orphaned = True
                    reason = "missing gitdir reference"
                else:
                    try:
                        target = gitdir_file.read_text(encoding="utf-8", errors="replace").strip()
                        if not os.path.exists(target):
                            orphaned = True
                            reason = f"target worktree missing ({target})"
                    except OSError:
                        orphaned = True
                        reason = "unreadable gitdir file"

                if orphaned:
                    if not dry_run:
                        try:
                            shutil.rmtree(admin_entry, ignore_errors=True)
                            pruned_entries.append({
                                "name": entry_name,
                                "reason": reason,
                                "dry_run": False,
                            })
                        except OSError as e:
                            warnings.append(f"Failed to remove orphaned admin directory {admin_entry}: {e}")
                    else:
                        pruned_entries.append({
                            "name": entry_name,
                            "reason": f"[dry-run] {reason}",
                            "dry_run": True,
                        })
        except OSError:
            pass

    # 4. Invalidate cache if repo changed
    if cache and (pruned_entries or not dry_run):
        cache.invalidate(repo_path)
        if primary_wt != repo_path:
            cache.invalidate(primary_wt)

    return {
        "repo": repo_path,
        "rel_path": rel_path,
        "pruned": pruned_entries,
        "warnings": warnings,
        "dirty_worktrees": dirty_linked,
        "dry_run": dry_run,
        "success": proc.returncode == 0,
        "error": proc.stderr if proc.returncode != 0 else None,
    }


def audit_worktree_health_batch(
    root_dir: str,
    repos: Optional[List[str]] = None,
    group: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    workers: int | str = DEFAULT_WORKERS,
) -> Dict[str, Any]:
    """
    Perform parallel batch audit of linked worktree topologies, uncommitted dirty states,
    and stale/orphaned worktree metadata across repositories.
    # INVARIANT: Inspects linked worktree working copies without modifying or deleting developer work.
    """
    eff_workers = max(1, min(32, workers if isinstance(workers, int) else 8))
    target_repos = repos
    if target_repos is None:
        target_repos = _call_discover_repos(root_dir)
        if group:
            target_repos = [r for r in target_repos if os.path.relpath(r, root_dir).startswith(group)]

    def _audit_repo_worktrees(r_path: str) -> Dict[str, Any]:
        rel_p = os.path.relpath(r_path, root_dir) if root_dir else os.path.basename(r_path)
        info = get_worktree_info(r_path, timeout=timeout)
        raw_wts = info.get("worktrees", [])
        stale_wts = info.get("stale_worktrees", [])
        is_secondary = info.get("is_worktree", False)
        main_wt = info.get("main_worktree")

        audited_wts: List[Dict[str, Any]] = []
        dirty_count = 0
        detached_count = 0
        prunable_count = len(stale_wts)

        for wt in raw_wts:
            w_path = wt.get("path", "")
            is_main = wt.get("is_main", False)
            branch = wt.get("branch", "")
            is_detached = (branch == "HEAD (detached)") or wt.get("detached", False)
            is_prunable = wt.get("is_prunable", False)
            prunable_reason = wt.get("prunable_reason")
            is_locked = wt.get("is_locked", False)
            lock_reason = wt.get("lock_reason")

            is_dirty = False
            dirty_files: List[str] = []
            if os.path.exists(w_path):
                try:
                    is_dirty, dirty_files = _call_check_repo_dirty(w_path, timeout=timeout)
                except Exception:
                    pass

            if is_dirty:
                dirty_count += 1
            if is_detached:
                detached_count += 1

            if is_prunable:
                status = "PRUNABLE"
            elif is_locked:
                status = "LOCKED"
            elif is_dirty:
                status = "DIRTY"
            elif is_detached:
                status = "DETACHED"
            else:
                status = "HEALTHY"

            audited_wts.append({
                "path": w_path,
                "branch": branch,
                "is_main": is_main,
                "is_detached": is_detached,
                "is_dirty": is_dirty,
                "dirty_files": dirty_files,
                "is_locked": is_locked,
                "lock_reason": lock_reason,
                "is_prunable": is_prunable,
                "prunable_reason": prunable_reason,
                "status": status,
            })

        is_healthy = (
            prunable_count == 0
            and dirty_count == 0
            and detached_count == 0
            and all(not w["is_locked"] for w in audited_wts)
        )

        return {
            "repo": rel_p,
            "path": r_path,
            "is_secondary_worktree": is_secondary,
            "main_worktree_path": main_wt,
            "total_worktrees": len(raw_wts),
            "linked_worktrees_count": max(0, len(raw_wts) - 1),
            "dirty_worktrees_count": dirty_count,
            "detached_worktrees_count": detached_count,
            "stale_worktrees_count": prunable_count,
            "is_healthy": is_healthy,
            "worktrees": audited_wts,
            "stale_worktrees": stale_wts,
        }

    results: List[Dict[str, Any]] = []
    if target_repos:
        eff_wt_workers = min(len(target_repos), eff_workers)
        if eff_wt_workers > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=eff_wt_workers) as executor:
                results = list(executor.map(_audit_repo_worktrees, target_repos))
        else:
            results = [_audit_repo_worktrees(r) for r in target_repos]

    repos_with_worktrees = [
        r for r in results
        if r["total_worktrees"] > 1 or r["is_secondary_worktree"] or r["stale_worktrees_count"] > 0
    ]
    total_dirty_linked = sum(r["dirty_worktrees_count"] for r in repos_with_worktrees)
    total_stale = sum(r["stale_worktrees_count"] for r in results)
    total_detached = sum(r["detached_worktrees_count"] for r in results)

    alerts: List[str] = []
    for r in repos_with_worktrees:
        if r["dirty_worktrees_count"] > 0:
            alerts.append(f"Repo '{r['repo']}' has {r['dirty_worktrees_count']} dirty linked worktree(s) with uncommitted files.")
        if r["stale_worktrees_count"] > 0:
            alerts.append(f"Repo '{r['repo']}' has {r['stale_worktrees_count']} stale/orphaned worktree metadata entry(ies). Prune with 'git-sync worktree prune'.")
        if r["detached_worktrees_count"] > 0:
            alerts.append(f"Repo '{r['repo']}' has {r['detached_worktrees_count']} worktree(s) with detached HEAD.")

    return {
        "total_repos_audited": len(target_repos),
        "repos_with_linked_worktrees": len(repos_with_worktrees),
        "total_dirty_worktrees": total_dirty_linked,
        "total_stale_worktrees": total_stale,
        "total_detached_worktrees": total_detached,
        "healthy_repos_count": sum(1 for r in results if r["is_healthy"]),
        "alerts": alerts,
        "items": results,
    }


def get_submodules_status(
    repo_path: str,
    timeout: int = DEFAULT_TIMEOUT,
    recursive: bool = True,
    detailed: bool = False,
) -> List[Dict[str, Any]]:
    """
    Query submodule status for repo_path.
    Returns a list of dicts:
        path: str (submodule relative path)
        commit: str (commit SHA)
        describe: str (branch/tag description, e.g. heads/main)
        status: str (UP_TO_DATE, MODIFIED, UNINITIALIZED, CONFLICT)
        out_of_sync: bool (True if status != UP_TO_DATE)
        detached: bool (True if checked out in detached HEAD)
        is_shallow: bool (True if submodule history is shallow/truncated)
    # INVARIANT: If .gitmodules file does not exist, repository has no submodules; skip subprocess entirely.
    """
    if not os.path.isfile(os.path.join(repo_path, ".gitmodules")):
        return []

    cmd = ["submodule", "status"]
    if recursive:
        cmd.append("--recursive")
    proc = _call_run_git(repo_path, cmd, timeout=timeout)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    submodules: List[Dict[str, Any]] = []
    prefix_map = {
        " ": "UP_TO_DATE",
        "+": "MODIFIED",
        "-": "UNINITIALIZED",
        "U": "CONFLICT",
    }
    for line in proc.stdout.splitlines():
        if not line:
            continue
        prefix = line[0]
        rest = line[1:].strip()
        parts = rest.split(None, 2)
        if not parts:
            continue
        commit = parts[0]
        sub_path = parts[1] if len(parts) > 1 else ""
        describe = ""
        if len(parts) > 2:
            desc_raw = parts[2].strip()
            if desc_raw.startswith("(") and desc_raw.endswith(")"):
                describe = desc_raw[1:-1]
            else:
                describe = desc_raw

        status_str = prefix_map.get(prefix, "UNKNOWN")
        is_out_of_sync = (status_str != "UP_TO_DATE")

        is_detached = False
        is_shallow = False
        if status_str != "UNINITIALIZED":
            sub_dir = os.path.join(repo_path, sub_path)
            if os.path.isdir(sub_dir):
                is_shallow = is_shallow_repository(sub_dir)
                if detailed:
                    p_b = _call_run_git(sub_dir, ["branch", "--show-current"], timeout=timeout)
                    if p_b.returncode == 0:
                        is_detached = (not p_b.stdout.strip())

        submodules.append({
            "path": sub_path,
            "commit": commit,
            "describe": describe,
            "status": status_str,
            "out_of_sync": is_out_of_sync,
            "detached": is_detached,
            "is_shallow": is_shallow,
            "shallow": is_shallow,
        })

    return submodules


def unshallow_submodule(
    repo_path: str,
    sub_path: str,
    timeout: int = DEFAULT_TIMEOUT,
    remote: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Safely unshallow a truncated shallow submodule by fetching its complete commit history.
    # INVARIANT: If submodule is not shallow, skip --unshallow to prevent Git fatal error:
    #            'fatal: --unshallow on a complete repository does not make sense'.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 prevents interactive credential hangs.
    """
    return _real_unshallow_submodule(repo_path, sub_path, timeout=timeout, remote=remote)


def _real_unshallow_submodule(
    repo_path: str,
    sub_path: str,
    timeout: int = DEFAULT_TIMEOUT,
    remote: Optional[str] = None,
) -> Dict[str, Any]:
    sub_dir = os.path.join(repo_path, sub_path)
    if not os.path.isdir(sub_dir):
        return {
            "path": sub_path,
            "success": False,
            "was_shallow": False,
            "unshallowed": False,
            "stdout": "",
            "stderr": f"Submodule directory '{sub_path}' does not exist",
            "error": f"Submodule directory '{sub_path}' does not exist",
        }

    if not is_shallow_repository(sub_dir):
        return {
            "path": sub_path,
            "success": True,
            "was_shallow": False,
            "unshallowed": False,
            "stdout": "Submodule is already complete (not shallow)",
            "stderr": "",
            "error": None,
        }

    cmd = ["fetch", "--unshallow"]
    if remote:
        cmd.append(remote)

    proc = _call_run_git(sub_dir, cmd, timeout=timeout)
    still_shallow = is_shallow_repository(sub_dir)
    success = (proc.returncode == 0) and not still_shallow

    return {
        "path": sub_path,
        "success": success,
        "was_shallow": True,
        "unshallowed": success,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "error": proc.stderr.strip() if not success else None,
    }


def update_submodules(
    repo_path: str,
    recursive: bool = True,
    init: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    dry_run: bool = False,
    unshallow: bool = False,
    cache: Optional[BulkCache] = None,
) -> Dict[str, Any]:
    """
    Update git submodules recursively with safe terminal prompt isolation (GIT_TERMINAL_PROMPT=0)
    and optional unshallow recovery for shallow submodules.
    # INVARIANT: GIT_TERMINAL_PROMPT=0 prevents interactive hangs during submodule fetch/checkout.
    # INVARIANT: When dry_run is True, no submodule checkout or unshallow fetch occurs.
    # INVARIANT: Unshallow is only applied to repositories where is_shallow_repository is True.
    """
    return _real_update_submodules(repo_path, recursive=recursive, init=init, timeout=timeout, dry_run=dry_run, unshallow=unshallow, cache=cache)


def _real_update_submodules(
    repo_path: str,
    recursive: bool = True,
    init: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    dry_run: bool = False,
    unshallow: bool = False,
    cache: Optional[BulkCache] = None,
) -> Dict[str, Any]:
    if not os.path.isfile(os.path.join(repo_path, ".gitmodules")):
        return {
            "has_submodules": False,
            "updated": False,
            "success": True,
            "dry_run": dry_run,
            "unshallow": unshallow,
            "unshallowed": [],
            "count": 0,
            "items": [],
            "submodules": [],
            "stdout": "",
            "stderr": "",
            "error": None,
        }

    subs_before = get_submodules_status(repo_path, timeout=timeout, recursive=recursive, detailed=True)
    if not subs_before:
        return {
            "has_submodules": False,
            "updated": False,
            "success": True,
            "dry_run": dry_run,
            "unshallow": unshallow,
            "unshallowed": [],
            "count": 0,
            "items": [],
            "submodules": [],
            "stdout": "",
            "stderr": "",
            "error": None,
        }

    drift_items = [s for s in subs_before if s.get("out_of_sync") or s.get("status") != "UP_TO_DATE"]
    shallow_items = [s for s in subs_before if s.get("is_shallow")]

    if dry_run:
        would_unshallow = [s["path"] for s in shallow_items] if unshallow else []
        return {
            "has_submodules": True,
            "updated": False,
            "success": True,
            "dry_run": True,
            "unshallow": unshallow,
            "unshallowed": [],
            "count": len(subs_before),
            "drift_count": len(drift_items),
            "shallow_count": len(shallow_items),
            "would_unshallow": would_unshallow,
            "pending_updates": drift_items,
            "items": subs_before,
            "submodules": subs_before,
            "stdout": f"[DRY-RUN] Submodules checked ({len(drift_items)} pending update, {len(shallow_items)} shallow)",
            "stderr": "",
            "error": None,
        }

    unshallowed_paths: List[str] = []
    unshallow_errors: List[str] = []

    # If unshallow requested, deepen any currently shallow submodules before checkout
    if unshallow:
        for sub in shallow_items:
            res_u = _real_unshallow_submodule(repo_path, sub["path"], timeout=timeout)
            if res_u.get("unshallowed"):
                unshallowed_paths.append(sub["path"])
            elif not res_u.get("success"):
                unshallow_errors.append(f"{sub['path']}: {res_u.get('error') or 'unshallow failed'}")

    cmd = ["submodule", "update"]
    if init:
        cmd.append("--init")
    if recursive:
        cmd.append("--recursive")

    proc = _call_run_git(repo_path, cmd, timeout=timeout)

    # After update/init, if any submodule was newly initialized as shallow and unshallow requested, unshallow it
    if unshallow:
        subs_mid = get_submodules_status(repo_path, timeout=timeout, recursive=recursive, detailed=False)
        for sub in subs_mid:
            if sub.get("is_shallow") and sub["path"] not in unshallowed_paths:
                res_u = _real_unshallow_submodule(repo_path, sub["path"], timeout=timeout)
                if res_u.get("unshallowed"):
                    unshallowed_paths.append(sub["path"])
                elif not res_u.get("success"):
                    unshallow_errors.append(f"{sub['path']}: {res_u.get('error') or 'unshallow failed'}")

    if cache:
        cache.invalidate(repo_path)

    subs_after = get_submodules_status(repo_path, timeout=timeout, recursive=recursive, detailed=True)
    success = (proc.returncode == 0) and not unshallow_errors

    stderr_combined = proc.stderr.strip()
    if unshallow_errors:
        err_msg = "; ".join(unshallow_errors)
        stderr_combined = f"{stderr_combined}\n{err_msg}".strip() if stderr_combined else err_msg

    return {
        "has_submodules": True,
        "updated": success,
        "success": success,
        "dry_run": False,
        "unshallow": unshallow,
        "unshallowed": unshallowed_paths,
        "count": len(subs_after),
        "items": subs_after,
        "submodules": subs_after,
        "stdout": proc.stdout.strip(),
        "stderr": stderr_combined,
        "error": stderr_combined if not success else None,
    }


__all__ = [
    "audit_worktree_health_batch",
    "find_locked_repos",
    "find_repo_locks",
    "get_repo_git_dir",
    "get_sparse_checkout_info",
    "get_submodules_status",
    "get_worktree_info",
    "is_path_in_cone",
    "is_shallow_repository",
    "prune_repo_worktrees",
    "reapply_sparse_checkout",
    "recover_index_lock",
    "recover_repo_locks",
    "unshallow_submodule",
    "update_submodules",
]
