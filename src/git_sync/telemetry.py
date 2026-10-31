"""
Performance benchmarking, telemetry tracking, and in-memory bulk cache for git-sync.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from git_sync.engine.concurrency import AdaptiveConcurrencyController

DEFAULT_WORKERS = 12
DEFAULT_DISK_LATENCY_THRESHOLD = 0.20
DEFAULT_NETWORK_LATENCY_THRESHOLD = 2.0


def _get_git_fn(name: str) -> Any:
    """Helper to dynamically resolve git functions respecting monkeypatching on git_sync.core."""
    core = sys.modules.get("git_sync.core")
    if core is not None and hasattr(core, name):
        return getattr(core, name)
    from git_sync.engine import git
    return getattr(git, name)


@dataclass
class TelemetryBenchmark:
    """
    Performance telemetry tracker for concurrent Git operations.
    Measures wall-clock execution times, stage breakdowns, network vs disk latencies,
    worker pool concurrency, and operational failure metrics.
    """
    start_time: float = field(default_factory=time.perf_counter)
    end_time: Optional[float] = None
    stages: Dict[str, float] = field(default_factory=dict)
    total_repos: int = 0
    workers: int | str = DEFAULT_WORKERS
    active_workers: int = 0
    optimal_workers: Optional[int] = None
    auto_tuned: bool = False
    cache_hits: int = 0
    cache_misses: int = 0
    network_latency_seconds: float = 0.0
    disk_latency_seconds: float = 0.0
    disk_latency_threshold: float = DEFAULT_DISK_LATENCY_THRESHOLD
    network_latency_threshold: float = DEFAULT_NETWORK_LATENCY_THRESHOLD
    failures_count: int = 0
    failures_by_type: Dict[str, int] = field(default_factory=dict)
    retries_count: int = 0
    retries_recovered: int = 0
    total_stale_remote_refs: int = 0
    total_pruned_remote_refs: int = 0
    total_gone_branches: int = 0
    total_merged_branches: int = 0
    hygiene_details: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_stage(self, stage: str, duration: float) -> None:
        """Record the elapsed time in seconds for a specific operational stage."""
        with self._lock:
            self.stages[stage] = round(duration, 4)

    def record_network_latency(self, duration: float) -> None:
        """Record elapsed time spent on network operations (e.g. fetch, push, remote queries)."""
        with self._lock:
            self.network_latency_seconds += duration

    def record_disk_latency(self, duration: float) -> None:
        """Record elapsed time spent on local filesystem/disk Git operations."""
        with self._lock:
            self.disk_latency_seconds += duration

    def record_failure(self, error_type: str = "general") -> None:
        """Record an operational failure with categorized error type."""
        with self._lock:
            self.failures_count += 1
            self.failures_by_type[error_type] = self.failures_by_type.get(error_type, 0) + 1

    def record_retry(self, recovered: bool = False) -> None:
        """Record a network retry attempt and whether it ultimately recovered."""
        with self._lock:
            self.retries_count += 1
            if recovered:
                self.retries_recovered += 1

    def record_hygiene(
        self,
        repo_rel: str,
        stale_remote_refs: Optional[List[str]] = None,
        pruned_remote_refs: Optional[List[str]] = None,
        gone_branches: Optional[List[str]] = None,
        merged_branches: Optional[List[str]] = None,
    ) -> None:
        """Record branch health and reference pruning metrics for a repository."""
        stale = list(stale_remote_refs or [])
        pruned = list(pruned_remote_refs or [])
        gone = list(gone_branches or [])
        merged = list(merged_branches or [])
        with self._lock:
            self.total_stale_remote_refs += len(stale)
            self.total_pruned_remote_refs += len(pruned)
            self.total_gone_branches += len(gone)
            self.total_merged_branches += len(merged)
            if stale or pruned or gone or merged:
                self.hygiene_details.append({
                    "repo": repo_rel,
                    "stale_remote_refs": stale,
                    "pruned_remote_refs": pruned,
                    "gone_branches": gone,
                    "merged_branches": merged,
                })

    def evaluate_thresholds(
        self,
        controller: Optional[AdaptiveConcurrencyController] = None,
    ) -> List[Dict[str, Any]]:
        """Evaluate recorded metrics against latency and worker saturation thresholds."""
        with self._lock:
            if controller is None:
                from git_sync.engine.concurrency import AdaptiveConcurrencyController
                ctrl = AdaptiveConcurrencyController(
                    disk_latency_threshold=self.disk_latency_threshold,
                    network_latency_threshold=self.network_latency_threshold,
                )
            else:
                ctrl = controller
            self.alerts = ctrl.evaluate_telemetry(self)
            return list(self.alerts)

    def stop(self) -> float:
        """Finalize the benchmark timer and return total elapsed duration."""
        if self.end_time is None:
            self.end_time = time.perf_counter()
        return self.duration_seconds

    @property
    def duration_seconds(self) -> float:
        """Total elapsed seconds from start to stop (or current time if still active)."""
        end = self.end_time if self.end_time is not None else time.perf_counter()
        return round(end - self.start_time, 4)

    @property
    def repos_per_second(self) -> float:
        """Computed throughput in repositories processed per second."""
        dur = self.duration_seconds
        if dur <= 0:
            dur = sum(self.stages.values())
        if dur <= 0 or self.total_repos <= 0:
            return 0.0
        return round(self.total_repos / dur, 2)

    def to_dict(self) -> Dict[str, Any]:
        """Convert telemetry metrics to a structured dictionary."""
        if not self.alerts and (
            self.disk_latency_seconds > 0
            or self.network_latency_seconds > 0
            or self.total_gone_branches > 0
            or self.total_stale_remote_refs > 0
            or self.total_merged_branches > 0
        ):
            self.evaluate_thresholds()

        avg_disk = round(self.disk_latency_seconds / self.total_repos, 4) if self.total_repos > 0 else 0.0
        avg_net = round(self.network_latency_seconds / self.total_repos, 4) if self.total_repos > 0 else 0.0
        active_w = self.active_workers if self.active_workers > 0 else self.workers

        return {
            "duration_seconds": self.duration_seconds,
            "total_repos": self.total_repos,
            "workers": self.workers,
            "repos_per_second": self.repos_per_second,
            "stages": dict(self.stages),
            "cache": {
                "hits": self.cache_hits,
                "misses": self.cache_misses,
            },
            "concurrency": {
                "configured_workers": self.workers,
                "active_workers": active_w,
                "optimal_workers": self.optimal_workers or active_w,
                "auto_tuned": self.auto_tuned,
            },
            "latencies": {
                "network_seconds": round(self.network_latency_seconds, 4),
                "disk_seconds": round(self.disk_latency_seconds, 4),
                "avg_disk_seconds": avg_disk,
                "avg_network_seconds": avg_net,
                "disk_threshold_seconds": self.disk_latency_threshold,
                "network_threshold_seconds": self.network_latency_threshold,
            },
            "hygiene": {
                "stale_remote_refs": self.total_stale_remote_refs,
                "pruned_remote_refs": self.total_pruned_remote_refs,
                "gone_branches": self.total_gone_branches,
                "merged_branches": self.total_merged_branches,
                "unhealthy_repos": len(self.hygiene_details),
                "details": list(self.hygiene_details),
            },
            "failures": {
                "total": self.failures_count,
                "by_type": dict(self.failures_by_type),
            },
            "retries": {
                "total": self.retries_count,
                "recovered": self.retries_recovered,
            },
            "alerts": list(self.alerts),
        }

    def summary_line(self) -> str:
        """Format a human-readable benchmark summary line."""
        if not self.alerts and (
            self.disk_latency_seconds > 0
            or self.network_latency_seconds > 0
            or self.total_gone_branches > 0
            or self.total_stale_remote_refs > 0
            or self.total_merged_branches > 0
        ):
            self.evaluate_thresholds()

        stage_strs = [f"{k}: {v:.2f}s" for k, v in self.stages.items()]
        stage_desc = f" ({', '.join(stage_strs)})" if stage_strs else ""
        cache_desc = f" | cache: {self.cache_hits} hits" if (self.cache_hits > 0 or self.cache_misses > 0) else ""
        latency_desc = ""
        if self.network_latency_seconds > 0 or self.disk_latency_seconds > 0:
            latency_desc = f" | net: {self.network_latency_seconds:.2f}s, disk: {self.disk_latency_seconds:.2f}s"
        fail_desc = f" | {self.failures_count} failure(s)" if self.failures_count > 0 else ""
        retry_desc = f" | retries: {self.retries_count} ({self.retries_recovered} rec)" if self.retries_count > 0 else ""

        hygiene_parts = []
        if self.total_stale_remote_refs > 0:
            hygiene_parts.append(f"{self.total_stale_remote_refs} stale refs")
        if self.total_pruned_remote_refs > 0:
            hygiene_parts.append(f"{self.total_pruned_remote_refs} pruned")
        if self.total_gone_branches > 0:
            hygiene_parts.append(f"{self.total_gone_branches} gone")
        if self.total_merged_branches > 0:
            hygiene_parts.append(f"{self.total_merged_branches} merged")
        hygiene_desc = f" | hygiene: {', '.join(hygiene_parts)}" if hygiene_parts else ""

        alerts_desc = ""
        if self.alerts:
            cat_list = [str(a.get("category", "ALERT")) for a in self.alerts]
            alerts_desc = f" | ⚠️ Alerts: {', '.join(cat_list)}"

        tuned_desc = " (auto-tuned)" if self.auto_tuned else ""

        return (
            f"⚡ Benchmark: {self.duration_seconds:.2f}s total{stage_desc}{latency_desc} | "
            f"{self.total_repos} repos | {self.repos_per_second:.1f} repos/s | "
            f"{self.workers} workers{tuned_desc}{cache_desc}{fail_desc}{retry_desc}{hygiene_desc}{alerts_desc}"
        )


class BulkCache:
    """
    Thread-safe in-memory cache for bulk Git operations.
    Minimizes redundant worktree inspections, remote evaluations, and submodule checks.
    # INVARIANT: Worktree queries return full topology; caching populates member worktrees
    # to eliminate duplicate subshells across sibling linked worktrees.
    """
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._worktrees: Dict[str, Dict[str, Any]] = {}
        self._remotes: Dict[str, Dict[str, str]] = {}
        self._submodules: Dict[str, List[Dict[str, Any]]] = {}
        self._hits = 0
        self._misses = 0

    def get_worktree_info(self, repo_path: str, timeout: int = 5) -> Dict[str, Any]:
        norm = os.path.abspath(repo_path)
        get_wt_info = _get_git_fn("get_worktree_info")
        if not self.enabled:
            return get_wt_info(repo_path, timeout=timeout)

        with self._lock:
            if norm in self._worktrees:
                self._hits += 1
                return self._worktrees[norm]
            self._misses += 1

        info = get_wt_info(repo_path, timeout=timeout)
        with self._lock:
            self._worktrees[norm] = info
            main_path = info.get("main_worktree") or (norm if not info.get("is_worktree") else None)
            all_wts = info.get("worktrees", [])
            for wt in all_wts:
                wt_p = wt.get("path")
                if wt_p and wt_p not in self._worktrees:
                    is_sec = not wt.get("is_main", False)
                    self._worktrees[wt_p] = {
                        "is_worktree": is_sec,
                        "main_worktree": main_path,
                        "worktrees": all_wts,
                        "checked_out_branches": info.get("checked_out_branches", {}),
                        "stale_worktrees": info.get("stale_worktrees", []),
                    }
        return info

    def get_remotes(self, repo_path: str, timeout: int = 5) -> Dict[str, str]:
        norm = os.path.abspath(repo_path)
        get_remotes_fn = _get_git_fn("get_repo_remotes")
        if not self.enabled:
            return get_remotes_fn(repo_path, timeout=timeout)

        with self._lock:
            if norm in self._remotes:
                self._hits += 1
                return self._remotes[norm]
            self._misses += 1

        remotes = get_remotes_fn(repo_path, timeout=timeout)
        with self._lock:
            self._remotes[norm] = remotes
        return remotes

    def get_submodules_status(
        self,
        repo_path: str,
        timeout: int = 5,
        recursive: bool = True,
        detailed: bool = False,
    ) -> List[Dict[str, Any]]:
        # INVARIANT: If .gitmodules file does not exist, repository has no submodules; skip subprocess entirely
        if not os.path.isfile(os.path.join(repo_path, ".gitmodules")):
            return []

        norm = os.path.abspath(repo_path)
        cache_key = f"{norm}:{recursive}:{detailed}"
        get_submodules_fn = _get_git_fn("get_submodules_status")
        if not self.enabled:
            return get_submodules_fn(repo_path, timeout=timeout, recursive=recursive, detailed=detailed)

        with self._lock:
            if cache_key in self._submodules:
                self._hits += 1
                return self._submodules[cache_key]
            self._misses += 1

        subs = get_submodules_fn(repo_path, timeout=timeout, recursive=recursive, detailed=detailed)
        with self._lock:
            self._submodules[cache_key] = subs
        return subs

    def invalidate(self, repo_path: str) -> None:
        """Invalidate all cached data for a repository and its linked worktrees."""
        norm = os.path.abspath(repo_path)
        with self._lock:
            self._remotes.pop(norm, None)
            matching_sub_keys = [k for k in self._submodules if k == norm or k.startswith(f"{norm}:")]
            for k in matching_sub_keys:
                self._submodules.pop(k, None)
            wt_entry = self._worktrees.pop(norm, None)
            if wt_entry:
                for wt in wt_entry.get("worktrees", []):
                    wt_p = wt.get("path")
                    if wt_p:
                        self._worktrees.pop(wt_p, None)

    def clear(self) -> None:
        """Clear the entire cache."""
        with self._lock:
            self._worktrees.clear()
            self._remotes.clear()
            self._submodules.clear()
            self._hits = 0
            self._misses = 0

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "cached_worktrees": len(self._worktrees),
                "cached_remotes": len(self._remotes),
                "cached_submodules": len(self._submodules),
            }


__all__ = [
    "DEFAULT_DISK_LATENCY_THRESHOLD",
    "DEFAULT_NETWORK_LATENCY_THRESHOLD",
    "DEFAULT_WORKERS",
    "BulkCache",
    "TelemetryBenchmark",
]
