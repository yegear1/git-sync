"""
Adaptive concurrency controller and worker pool auto-tuning for git-sync.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from git_sync.telemetry import TelemetryBenchmark

DEFAULT_WORKERS = 12
DEFAULT_MAX_WORKERS = 32
DEFAULT_MIN_WORKERS = 1
DEFAULT_DISK_LATENCY_THRESHOLD = 0.20
DEFAULT_NETWORK_LATENCY_THRESHOLD = 2.0


def calculate_optimal_workers(
    repo_count: int,
    is_fetch: bool = True,
    cpu_count: Optional[int] = None,
    min_workers: int = DEFAULT_MIN_WORKERS,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> int:
    """
    Calculate optimal worker thread count based on host core count and operation nature.
    # INVARIANT: Caps workers to repo_count to avoid idle thread overhead for small workspaces.
    # INVARIANT: Local disk operations use lower concurrency bounds than network fetches to avoid I/O thrashing.
    """
    if repo_count <= 0:
        return min_workers

    cores = max(1, cpu_count if cpu_count is not None else (os.cpu_count() or 4))
    if is_fetch:
        # Network I/O (git fetch/push): benefit from higher concurrency up to max_workers
        target = min(cores * 3, max_workers)
    else:
        # Local disk I/O (git status/rev-parse): bounded by disk I/O and process fork overhead
        target = min(cores * 2, 16, max_workers)

    return max(min_workers, min(target, repo_count))


@dataclass
class AdaptiveConcurrencyController:
    """
    Adaptive concurrency controller scaling worker threads dynamically
    based on host core count, repository count, and observed I/O latency measurements.
    # INVARIANT: Degrades concurrency gracefully when worker saturation is detected.
    # INVARIANT: Flags telemetry latency alerts to warn AI agents of storage or network bottlenecks.
    """
    cpu_count: int = field(default_factory=lambda: os.cpu_count() or 4)
    min_workers: int = DEFAULT_MIN_WORKERS
    max_workers: int = DEFAULT_MAX_WORKERS
    disk_latency_threshold: float = DEFAULT_DISK_LATENCY_THRESHOLD
    network_latency_threshold: float = DEFAULT_NETWORK_LATENCY_THRESHOLD

    def determine_workers(
        self,
        repo_count: int,
        is_fetch: bool = True,
        configured_workers: Optional[int | str] = None,
    ) -> int:
        """
        Determine effective worker count.
        If configured_workers is an explicit positive integer (and not 'auto'):
          clamps concurrency to min(configured_workers, max(1, repo_count)).
        Otherwise, auto-tunes concurrency based on CPU core count and operation type.
        """
        if repo_count <= 0:
            return self.min_workers

        if configured_workers is not None and str(configured_workers).strip().lower() != "auto":
            try:
                w = int(configured_workers)
                if w > 0:
                    return max(self.min_workers, min(w, repo_count))
            except ValueError:
                pass

        return calculate_optimal_workers(
            repo_count=repo_count,
            is_fetch=is_fetch,
            cpu_count=self.cpu_count,
            min_workers=self.min_workers,
            max_workers=self.max_workers,
        )

    def tune_workers_for_latency(
        self,
        current_workers: int,
        avg_disk_latency: float,
        avg_network_latency: float,
        repo_count: int = 0,
    ) -> int:
        """
        Dynamically scale worker count down or up based on observed latencies.
        # INVARIANT: Reduces worker pool when disk or network contention thresholds are breached.
        """
        if current_workers <= self.min_workers:
            return self.min_workers

        tuned = current_workers

        # Severe disk contention: reduce concurrency to reduce context switching and disk queue depth
        if avg_disk_latency > self.disk_latency_threshold:
            reduction = max(1, current_workers // 3)
            tuned = max(self.min_workers, current_workers - reduction)

        # Severe network saturation: reduce concurrent requests to avoid socket exhaustion
        if avg_network_latency > self.network_latency_threshold:
            reduction = max(1, current_workers // 3)
            tuned = max(self.min_workers, min(tuned, current_workers - reduction))

        if repo_count > 0:
            tuned = min(tuned, repo_count)

        return max(self.min_workers, tuned)

    def evaluate_telemetry(self, benchmark: TelemetryBenchmark) -> List[Dict[str, Any]]:
        """
        Evaluate a TelemetryBenchmark against latency thresholds and concurrency saturation.
        Attaches structured alerts to the benchmark.
        """
        alerts: List[Dict[str, Any]] = []
        total = benchmark.total_repos
        if total <= 0:
            return alerts

        avg_disk = round(benchmark.disk_latency_seconds / total, 4)
        avg_net = round(benchmark.network_latency_seconds / total, 4)
        raw_workers = benchmark.active_workers if benchmark.active_workers > 0 else benchmark.workers
        active_w = int(raw_workers) if isinstance(raw_workers, int) else 1

        # 1. Disk contention alert
        if total >= 1 and avg_disk > self.disk_latency_threshold:
            recommended = max(self.min_workers, active_w // 2)
            alerts.append({
                "category": "DISK_CONTENTION",
                "severity": "WARNING",
                "message": (
                    f"High average disk latency ({avg_disk:.3f}s > threshold {self.disk_latency_threshold:.3f}s) "
                    f"indicates I/O contention across {active_w} workers."
                ),
                "threshold_seconds": self.disk_latency_threshold,
                "observed_seconds": avg_disk,
                "recommended_workers": recommended,
            })

        # 2. Network saturation alert
        if total >= 1 and avg_net > self.network_latency_threshold:
            recommended = max(self.min_workers, active_w // 2)
            alerts.append({
                "category": "NETWORK_SATURATION",
                "severity": "WARNING",
                "message": (
                    f"High average network latency ({avg_net:.3f}s > threshold {self.network_latency_threshold:.3f}s) "
                    f"indicates network saturation or remote throttling across {active_w} workers."
                ),
                "threshold_seconds": self.network_latency_threshold,
                "observed_seconds": avg_net,
                "recommended_workers": recommended,
            })

        # 3. Worker over-subscription / thread contention alert
        if active_w > 1 and active_w > (self.cpu_count * 4) and avg_disk > 0.08:
            optimal = min(self.cpu_count * 2, total)
            alerts.append({
                "category": "WORKER_SATURATION",
                "severity": "WARNING",
                "message": (
                    f"Configured workers ({active_w}) exceeds host core capacity ({self.cpu_count} cores), "
                    f"degrading throughput via context switching and thread contention."
                ),
                "threshold_seconds": 0.08,
                "observed_seconds": avg_disk,
                "recommended_workers": max(self.min_workers, optimal),
            })

        # 4. Branch hygiene alert
        if benchmark.total_gone_branches > 0 or benchmark.total_stale_remote_refs > 0 or benchmark.total_merged_branches > 0:
            details_list = []
            if benchmark.total_stale_remote_refs > 0:
                details_list.append(f"{benchmark.total_stale_remote_refs} stale remote refs")
            if benchmark.total_gone_branches > 0:
                details_list.append(f"{benchmark.total_gone_branches} gone local branches")
            if benchmark.total_merged_branches > 0:
                details_list.append(f"{benchmark.total_merged_branches} merged branches")
            alerts.append({
                "category": "BRANCH_HYGIENE",
                "severity": "INFO",
                "message": f"Workspace contains {', '.join(details_list)} across inspected repositories.",
                "recommendation": "Execute 'git-sync prune --all' to prune stale references and safely cleanup obsolete branches.",
            })

        benchmark.alerts = alerts
        return alerts


__all__ = [
    "DEFAULT_DISK_LATENCY_THRESHOLD",
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_MIN_WORKERS",
    "DEFAULT_NETWORK_LATENCY_THRESHOLD",
    "DEFAULT_WORKERS",
    "AdaptiveConcurrencyController",
    "calculate_optimal_workers",
]
