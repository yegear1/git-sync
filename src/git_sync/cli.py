"""
Command Line Interface (CLI) entrypoint for git-sync.
Handles argument parsing, command dispatch, workspace profiles, and output formatting.
"""

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from git_sync import __version__
from git_sync.config import (
    CONFIG_FILENAME,
    GitSyncConfig,
    get_default_config_path,
    init_config_file,
    load_config,
)
from git_sync.engine import (
    DEFAULT_COMPACT_DIFF_TOKENS,
    DEFAULT_PROTECTED_BRANCHES,
    DEFAULT_RETRIES,
    DEFAULT_ROOT,
    DEFAULT_TIMEOUT,
    DEFAULT_WORKERS,
    AdaptiveConcurrencyController,
    DoctorSanityReport,
    PullResult,
    PushResult,
    RepoStatusRecord,
    ResolveResult,
    audit_ssh_multiplexing,
    benchmark_ssh_multiplexing,
    calculate_optimal_workers,
    check_credentials_sanity,
    diagnose_git_error,
    discover_repos,
    find_diverged_repos,
    find_locked_repos,
    find_push_pending_repos,
    format_changelog_summary,
    get_divergence_details,
    get_divergence_diff,
    get_worktree_info,
    inspect_credential_helpers,
    inspect_github_cli,
    inspect_repo,
    inspect_ssh_config,
    inspect_token_env,
    is_branch_protected,
    match_repo,
    preflight_push_check,
    prune_repo_worktrees,
    prune_stale_ssh_control_sockets,
    pull_repo,
    push_repo,
    recover_index_lock,
    recover_repo_locks,
    resolve_divergence,
    run_git_with_retry,
    truncate_diff,
)
from git_sync.telemetry import BulkCache, TelemetryBenchmark
from git_sync.ui import (
    AnsiStyler,
    BulkActionItem,
    InteractiveDashboard,
    ProgressIndicator,
    build_status_summary,
    format_bulk_action_preview,
    format_diff_stat,
    print_benchmark_report,
    print_dirty_report,
    print_doctor_report,
    print_ssh_audit_report,
    print_status_compact,
    print_status_table,
    print_unlock_report,
    print_worktree_list_report,
    print_worktree_prune_report,
    prompt_bulk_confirmation,
    prompt_resolution_choice,
    render_pull_item,
    render_push_item,
)
from git_sync.installer import (
    generate_completion_script,
    install_agent_skills,
    install_completion,
    install_symlink,
    uninstall_agent_skills,
    uninstall_symlink,
)


def _parse_workers_arg(val: str) -> int | str:
    """Parse --workers CLI argument accepting a positive integer or 'auto'."""
    s = val.strip().lower()
    if s == "auto":
        return "auto"
    try:
        n = int(s)
        if n < 1:
            raise ValueError()
        return n
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"Invalid workers value: '{val}'. Must be a positive integer or 'auto'.")


def _filter_by_group(
    repos: List[str],
    root: str,
    group: Optional[str] = None,
    groups: Optional[List[str]] = None,
) -> List[str]:
    """
    Filter repository paths by explicit CLI group or configured preferred workspace groups.
    # INVARIANT: CLI group takes precedence when non-empty; otherwise configured groups list is applied.
    """
    effective_groups = [group] if group else (groups or [])
    if not effective_groups:
        return repos
    return [
        r for r in repos
        if any(os.path.relpath(r, root).startswith(g) for g in effective_groups)
    ]


_print_ssh_audit_output = print_ssh_audit_report


def cmd_doctor(args: argparse.Namespace) -> int:
    """
    Perform proactive credential health diagnostics and network reachability check.
    # INVARIANT: Probing enforces GIT_TERMINAL_PROMPT=0 and never blocks in headless/agent environments.
    """
    root = args.root
    group = getattr(args, "group", None)
    timeout = args.timeout
    ssh_flag = getattr(args, "ssh", False)

    if not args.json:
        print(f"\n🩺 Executando diagnóstico de credenciais e conectividade em {root}...")

    workers = getattr(args, "workers", DEFAULT_WORKERS)
    sanity = check_credentials_sanity(
        root_dir=root,
        group=group,
        timeout=timeout,
        test_remotes=True,
        workers=workers,
    )

    ssh_audit = None
    if ssh_flag:
        ssh_audit = audit_ssh_multiplexing(
            root_dir=root,
            group=group,
            timeout=timeout,
        )
        sanity["ssh_audit"] = ssh_audit

    if args.json:
        print(json.dumps(sanity, indent=2))
        return 0 if sanity["ok"] else 1

    return print_doctor_report(sanity, ssh_audit=ssh_audit)


_print_benchmark_output = print_benchmark_report


def cmd_status(args: argparse.Namespace) -> int:
    root = args.root
    exclude = getattr(args, "exclude", None)
    remote = getattr(args, "remote", None)
    use_cache = not getattr(args, "no_cache", False)
    cache = BulkCache(enabled=use_cache)
    controller = AdaptiveConcurrencyController()
    auto_tuned = getattr(args, "auto_tune", False) or (args.workers == "auto")
    benchmark = TelemetryBenchmark(workers=args.workers, auto_tuned=auto_tuned)

    if getattr(args, "preflight", False):
        if not args.json:
            print("🩺 Executando verificação prévia de credenciais...")
        preflight_res = check_credentials_sanity(root, group=getattr(args, "group", None), timeout=args.timeout, test_remotes=True)
        if not preflight_res["ok"]:
            if args.json:
                print(json.dumps({"error": "Preflight credential sanity check failed", "preflight": preflight_res}, indent=2))
                return 1
            print("❌ Falha na verificação prévia de credenciais:")
            for err in preflight_res["errors"]:
                print(f"  • {err}")
            for sug in preflight_res["suggestions"]:
                print(f"  💡 {sug}")
            return 1
        elif not args.json:
            print("✅ Pre-flight: credenciais verificadas com sucesso.")

    t0 = time.perf_counter()
    all_repos = discover_repos(root, exclude=exclude)
    t_disc = time.perf_counter() - t0
    benchmark.record_stage("discovery", t_disc)

    if not all_repos:
        benchmark.stop()
        if args.json:
            if getattr(args, "compact", False):
                empty_summary = {
                    "root": root,
                    "total": 0,
                    "pull_pending": [],
                    "push_pending": [],
                    "dirty": [],
                    "diverged": [],
                    "worktrees": [],
                    "has_submodules": [],
                    "has_lfs": [],
                    "lfs_pointers": [],
                    "no_remote": [],
                    "errors": [],
                }
                if getattr(args, "benchmark", False):
                    empty_summary["telemetry"] = benchmark.to_dict()
                print(json.dumps(empty_summary, indent=2))
            else:
                resp: Dict[str, Any] = {"error": f"No repositories found in {root}", "results": []}
                if getattr(args, "benchmark", False):
                    resp["telemetry"] = benchmark.to_dict()
                print(json.dumps(resp))
        else:
            if getattr(args, "dashboard", False) or getattr(args, "tui", False):
                cfg_obj = getattr(args, "_config", None)
                dashboard = InteractiveDashboard(
                    root=root,
                    repos=[],
                    do_fetch=not args.no_fetch,
                    workers=args.workers,
                    timeout=args.timeout,
                    retries=getattr(args, "retries", DEFAULT_RETRIES),
                    remote=remote,
                    exclude=exclude,
                    group=getattr(args, "group", None),
                    groups=getattr(args, "groups", None),
                    ff_only=getattr(args, "ff_only", True),
                    sync_branches=getattr(args, "sync_branches", True),
                    autostash=getattr(args, "autostash", True),
                    dry_run=getattr(args, "dry_run", False),
                    config=cfg_obj,
                    active_profile=getattr(cfg_obj, "active_profile", None) if cfg_obj else None,
                    available_profiles=getattr(cfg_obj, "available_profiles", None) if cfg_obj else None,
                    config_path=getattr(cfg_obj, "config_path", None) if cfg_obj else None,
                )
                return dashboard.run()
            print(f"Nenhum repositório encontrado em {root}")
            if getattr(args, "benchmark", False):
                _print_benchmark_output(benchmark, getattr(args, "json", False))
        return 0

    all_repos = _filter_by_group(
        all_repos,
        root,
        group=getattr(args, "group", None),
        groups=getattr(args, "groups", None),
    )

    t1 = time.perf_counter()
    results: List[RepoStatusRecord | Dict[str, Any]] = []
    show_progress = False if (args.json or getattr(args, "compact", False) or getattr(args, "summary", False)) else None
    retries = getattr(args, "retries", DEFAULT_RETRIES)
    is_fetch = not args.no_fetch
    changelog_flag = getattr(args, "changelog", None)
    config_obj = getattr(args, "_config", None)
    changelog_enabled = bool(changelog_flag if changelog_flag is not None else getattr(config_obj, "status_changelog", False))
    configured_w = None if auto_tuned else args.workers
    effective_workers = controller.determine_workers(
        len(all_repos),
        is_fetch=is_fetch,
        configured_workers=configured_w,
    )
    benchmark.active_workers = effective_workers
    benchmark.optimal_workers = effective_workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as executor:
        futures = {
            executor.submit(
                inspect_repo,
                r,
                root,
                not args.no_fetch,
                args.timeout,
                cache,
                remote,
                benchmark,
                retries,
                False,
                False,
                changelog_enabled,
            ): r
            for r in all_repos
        }
        with ProgressIndicator("Inspecionando repositórios...", total=len(all_repos), enabled=show_progress) as progress:
            for future in concurrent.futures.as_completed(futures):
                repo_path = futures[future]
                res_item = future.result()
                results.append(res_item)
                prog_label = os.path.relpath(repo_path, root)
                if res_item.get("is_worktree"):
                    prog_label = f"{prog_label} [worktree]"
                progress.advance(prog_label)
    t_insp = time.perf_counter() - t1
    benchmark.record_stage("inspection", t_insp)
    benchmark.total_repos = len(all_repos)
    benchmark.active_workers = effective_workers
    c_stats = cache.stats
    benchmark.cache_hits = c_stats["hits"]
    benchmark.cache_misses = c_stats["misses"]
    benchmark.stop()
    benchmark.evaluate_thresholds(controller)

    results.sort(key=lambda x: x["rel_path"])

    if args.pending:
        results = [r for r in results if r["status"] != "UP_TO_DATE" or r["is_dirty"]]

    if args.json:
        summary = build_status_summary(
            results,
            root=root,
            changelog_enabled=changelog_enabled,
            compact=getattr(args, "compact", False),
            benchmark=benchmark if getattr(args, "benchmark", False) else None,
        )
        print(json.dumps(summary, indent=2))
        return 0

    if getattr(args, "compact", False):
        print_status_compact(
            results,
            changelog_enabled=changelog_enabled,
            benchmark=benchmark if getattr(args, "benchmark", False) else None,
        )
        return 0

    if getattr(args, "dashboard", False) or getattr(args, "tui", False):
        cfg_obj = getattr(args, "_config", None)
        dashboard = InteractiveDashboard(
            root=root,
            repos=results,
            do_fetch=not args.no_fetch,
            workers=args.workers,
            timeout=args.timeout,
            retries=retries,
            remote=remote,
            exclude=exclude,
            group=getattr(args, "group", None),
            groups=getattr(args, "groups", None),
            ff_only=getattr(args, "ff_only", True),
            sync_branches=getattr(args, "sync_branches", True),
            autostash=getattr(args, "autostash", True),
            dry_run=getattr(args, "dry_run", False),
            config=cfg_obj,
            active_profile=getattr(cfg_obj, "active_profile", None) if cfg_obj else None,
            available_profiles=getattr(cfg_obj, "available_profiles", None) if cfg_obj else None,
            config_path=getattr(cfg_obj, "config_path", None) if cfg_obj else None,
        )
        return dashboard.run()

    print_status_table(
        results,
        changelog_enabled=changelog_enabled,
        show_submodules=getattr(args, "submodules", False),
        benchmark=benchmark if getattr(args, "benchmark", False) else None,
    )
    return 0


_prompt_resolution_choice = prompt_resolution_choice


def cmd_pull(args: argparse.Namespace) -> int:
    root = args.root
    exclude = getattr(args, "exclude", None)
    remote = getattr(args, "remote", None)
    use_cache = not getattr(args, "no_cache", False)
    cache = BulkCache(enabled=use_cache)
    controller = AdaptiveConcurrencyController()
    auto_tuned = getattr(args, "auto_tune", False) or (args.workers == "auto")
    benchmark = TelemetryBenchmark(workers=args.workers, auto_tuned=auto_tuned)
    dry_run = getattr(args, "dry_run", False)
    retries = getattr(args, "retries", DEFAULT_RETRIES)

    if getattr(args, "preflight", False):
        if not args.json:
            print("🩺 Executando verificação prévia de credenciais...")
        preflight_res = check_credentials_sanity(root, group=getattr(args, "group", None), timeout=args.timeout, test_remotes=True)
        if not preflight_res["ok"]:
            if args.json:
                print(json.dumps({"error": "Preflight credential sanity check failed", "preflight": preflight_res}, indent=2))
                return 1
            print("❌ Falha na verificação prévia de credenciais:")
            for err in preflight_res["errors"]:
                print(f"  • {err}")
            for sug in preflight_res["suggestions"]:
                print(f"  💡 {sug}")
            return 1
        elif not args.json:
            print("✅ Pre-flight: credenciais verificadas com sucesso.")

    t0 = time.perf_counter()
    all_repos = discover_repos(root, exclude=exclude)
    all_repos = _filter_by_group(
        all_repos,
        root,
        group=getattr(args, "group", None),
        groups=getattr(args, "groups", None),
    )
    t_disc = time.perf_counter() - t0
    benchmark.record_stage("discovery", t_disc)
    targets: List[str] = []
    show_progress = False if (args.json or getattr(args, "compact", False) or getattr(args, "summary", False)) else None

    if args.all:
        if not args.json:
            mode_prefix = "[DRY-RUN] " if dry_run else ""
            print(f"🔍 {mode_prefix}Identificando repositórios com atualizações pendentes...")
        t1 = time.perf_counter()
        inspections: List[RepoStatusRecord] = []
        configured_w = None if auto_tuned else args.workers
        effective_workers = controller.determine_workers(
            len(all_repos),
            is_fetch=True,
            configured_workers=configured_w,
        )
        benchmark.active_workers = effective_workers
        benchmark.optimal_workers = effective_workers
        with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = {
                executor.submit(inspect_repo, r, root, True, args.timeout, cache, remote, benchmark, retries): r
                for r in all_repos
            }
            with ProgressIndicator("Auditando repositórios...", total=len(all_repos), enabled=show_progress) as progress:
                for future in concurrent.futures.as_completed(futures):
                    res_item = future.result()
                    inspections.append(res_item)
                    prog_label = os.path.relpath(futures[future], root)
                    if res_item.get("is_worktree"):
                        prog_label = f"{prog_label} [worktree]"
                    progress.advance(prog_label)
        t_insp = time.perf_counter() - t1
        benchmark.record_stage("inspection", t_insp)

        def _submodule_out_of_sync(r: RepoStatusRecord) -> bool:
            sub = r.get("submodules")
            sub_list = sub if isinstance(sub, list) else (sub.get("submodules", []) if isinstance(sub, dict) else [])
            return any(isinstance(s, dict) and (s.get("status") != "UP_TO_DATE" or s.get("out_of_sync")) for s in sub_list)

        def _submodule_is_shallow(r: RepoStatusRecord) -> bool:
            sub = r.get("submodules")
            sub_list = sub if isinstance(sub, list) else (sub.get("submodules", []) if isinstance(sub, dict) else [])
            return any(isinstance(s, dict) and s.get("is_shallow") for s in sub_list)

        targets = [
            r["path"] for r in inspections
            if "PULL" in r.get("status", "")
            or r.get("aux_branches_behind")
            or (getattr(args, "submodules", False) and _submodule_out_of_sync(r))
            or (getattr(args, "unshallow", False) and (r.get("is_shallow") or _submodule_is_shallow(r)))
            or (getattr(args, "reapply_sparse", False) and bool((r.get("sparse_checkout") or {}).get("enabled")))
            or (getattr(args, "lfs", False) and (r.get("has_lfs") or bool((r.get("lfs") or {}).get("has_lfs"))))
        ]
        if not targets:
            benchmark.stop()
            if args.json:
                res_obj: Dict[str, Any] = {"success": True, "message": "All repositories are already up to date.", "pulled": [], "dry_run": dry_run}
                if getattr(args, "benchmark", False):
                    res_obj["telemetry"] = benchmark.to_dict()
                print(json.dumps(res_obj))
            else:
                mode_prefix = "[DRY-RUN] " if dry_run else ""
                print(f"✅ {mode_prefix}Todos os repositórios já estão 100% atualizados.")
                if getattr(args, "benchmark", False):
                    _print_benchmark_output(benchmark, getattr(args, "json", False))
            return 0
    else:
        if not args.repos:
            if args.json:
                print(json.dumps({"error": "Specify repository name(s) or pass --all"}))
            else:
                print("Especifique um ou mais repositórios ou use --all")
            return 1
        for q in args.repos:
            matched = match_repo(q, all_repos, root)
            if matched:
                targets.append(matched)
            else:
                if not args.json:
                    print(f"❌ Repositório não encontrado para a busca: '{q}'")

    pull_results = []
    strategy = getattr(args, "strategy", None)
    is_interactive = getattr(args, "interactive", False)

    preview_mode = getattr(args, "preview", False)
    confirm_mode = getattr(args, "confirm", False)

    if (preview_mode or confirm_mode) and targets:
        insp_map = {r["path"]: r for r in inspections} if args.all else {}
        preview_items: List[BulkActionItem] = []
        for r in targets:
            rel = os.path.relpath(r, root)
            if r in insp_map:
                info = insp_map[r]
            else:
                info = inspect_repo(r, root, False, args.timeout, cache, remote, benchmark, retries)
            branch = info.get("branch")
            behind = int(info.get("behind", 0))
            ahead = int(info.get("ahead", 0))
            is_dirty = bool(info.get("is_dirty", False))
            dirty_files_count = len(info.get("dirty_files", []))
            submodules_count = len(info.get("submodules", []))
            warnings: List[str] = []
            if is_dirty:
                warnings.append(
                    f"Árvore suja ({dirty_files_count} arquivo(s)) - autostash ativado"
                    if getattr(args, "autostash", True)
                    else f"Árvore suja ({dirty_files_count} arquivo(s))"
                )
            if info.get("aux_branches_behind"):
                warnings.append(f"{len(info['aux_branches_behind'])} branch(es) secundária(s) pendente(s)")
            if info.get("is_shallow"):
                warnings.append("Repositório raso (shallow)")
            lfs_d = info.get("lfs") or {}
            if (info.get("has_lfs") or lfs_d.get("has_lfs")) and lfs_d.get("missing_objects", 0) > 0:
                warnings.append(f"Git LFS: {lfs_d['missing_objects']} objeto(s) pendente(s) de download")
            action_label = f"PULL ({strategy})" if strategy else "PULL (ff-only)"
            preview_items.append(
                BulkActionItem(
                    repo=r,
                    repo_rel=rel,
                    action=action_label,
                    branch=branch,
                    ahead=ahead,
                    behind=behind,
                    is_dirty=is_dirty,
                    dirty_files_count=dirty_files_count,
                    submodules_count=submodules_count,
                    warnings=warnings,
                    details=info,
                )
            )

        if preview_mode:
            if args.json:
                print(json.dumps({"preview": True, "action": "pull", "total": len(preview_items), "items": [i.to_dict() for i in preview_items]}, indent=2))
            else:
                print(format_bulk_action_preview(preview_items, title="PRÉVIA DE PULL EM LOTE (BULK PULL PREVIEW)"))
            return 0

        if confirm_mode:
            if args.json:
                print(json.dumps({"error": "Interactive confirmation cannot be prompted in JSON mode. Pass --yes to confirm.", "preview": [i.to_dict() for i in preview_items]}, indent=2))
                return 1
            confirmed = prompt_bulk_confirmation(
                preview_items,
                title="PRÉVIA DE PULL EM LOTE (BULK PULL PREVIEW)",
                prompt_text=f"Confirmar atualização em {len(preview_items)} repositório(s)? [y/N]: ",
            )
            if not confirmed:
                return 0

    t2 = time.perf_counter()
    msg_prog = "Simulando pull..." if dry_run else "Atualizando repositórios..."
    with ProgressIndicator(msg_prog, total=len(targets), enabled=show_progress) as progress:
        for r in targets:
            rel = os.path.relpath(r, root)
            res = pull_repo(
                r,
                remote=remote,
                ff_only=args.ff_only,
                sync_branches=args.sync_branches,
                strategy=strategy,
                autostash=getattr(args, "autostash", True),
                timeout=args.timeout,
                cache=cache,
                dry_run=dry_run,
                benchmark=benchmark,
                retries=retries,
                recover_stale_lock=getattr(args, "recover_locks", False),
                submodules=getattr(args, "submodules", False),
                unshallow=getattr(args, "unshallow", False),
                reapply_sparse=getattr(args, "reapply_sparse", False),
                lfs=getattr(args, "lfs", False),
                no_lfs=getattr(args, "no_lfs", False),
            )
            res["repo_rel"] = rel

            # If pull failed and user requested interactive resolution
            if not res["success"] and not dry_run and is_interactive and not args.json and sys.stdin.isatty():
                details = get_divergence_details(r, timeout=args.timeout)
                if details.get("is_diverged"):
                    res_prompt = _prompt_resolution_choice(r, rel, details, timeout=args.timeout)
                    res["resolved"] = res_prompt
                    if res_prompt.get("success") and res_prompt.get("strategy") in ("rebase", "merge"):
                        res["success"] = True

            pull_results.append(res)
            prog_label = f"{rel} [worktree]" if res.get("is_worktree") else rel
            progress.advance(prog_label)

            if not args.json:
                render_pull_item(res, root, dry_run=dry_run)

    t_pull = time.perf_counter() - t2
    benchmark.record_stage("pull", t_pull)
    benchmark.total_repos = len(targets)
    configured_w = None if auto_tuned else args.workers
    effective_workers = controller.determine_workers(
        len(all_repos),
        is_fetch=True,
        configured_workers=configured_w,
    )
    benchmark.active_workers = effective_workers
    c_stats = cache.stats
    benchmark.cache_hits = c_stats["hits"]
    benchmark.cache_misses = c_stats["misses"]
    benchmark.stop()
    benchmark.evaluate_thresholds(controller)

    if args.json:
        if getattr(args, "benchmark", False):
            print(json.dumps({"results": pull_results, "dry_run": dry_run, "telemetry": benchmark.to_dict()}, indent=2))
        else:
            print(json.dumps(pull_results, indent=2))
    else:
        if getattr(args, "benchmark", False):
            _print_benchmark_output(benchmark, getattr(args, "json", False))

    has_failures = any(not r.get("success", True) for r in pull_results)
    return 1 if has_failures else 0


def cmd_resolve(args: argparse.Namespace) -> int:
    """
    Inspect and resolve divergent branches across repositories.
    Supports interactive diagnostics, diff preview, and automated batch strategies.
    # INVARIANT: Conflicts automatically abort to preserve clean git working state.
    # INVARIANT: When --dry-run is active, no rebases, merges, or working tree mutations are performed.
    """
    root = args.root
    exclude = getattr(args, "exclude", None)
    dry_run = getattr(args, "dry_run", False)
    all_repos = discover_repos(root, exclude=exclude)
    all_repos = _filter_by_group(
        all_repos,
        root,
        group=getattr(args, "group", None),
        groups=getattr(args, "groups", None),
    )
    targets: List[str] = []
    show_progress = False if (args.json or getattr(args, "compact", False) or getattr(args, "summary", False)) else None

    if getattr(args, "all", False):
        if not args.json:
            mode_prefix = "[DRY-RUN] " if dry_run else ""
            print(f"🔍 {mode_prefix}Identificando repositórios com branches divergentes...")
        diverged = find_diverged_repos(
            all_repos,
            root,
            do_fetch=not getattr(args, "no_fetch", False),
            workers=args.workers,
            timeout=args.timeout,
        )
        targets = [r["path"] for r in diverged]
        if not targets:
            if args.json:
                print(json.dumps({"success": True, "message": "No diverged repositories found.", "resolved": [], "dry_run": dry_run}))
            else:
                mode_prefix = "[DRY-RUN] " if dry_run else ""
                print(f"✅ {mode_prefix}Nenhum repositório com divergência encontrado.")
            return 0
    else:
        if not getattr(args, "repos", None):
            if args.json:
                print(json.dumps({"error": "Specify repository name(s) or pass --all"}))
            else:
                print("Especifique um ou mais repositórios ou use --all")
            return 1
        for q in args.repos:
            matched = match_repo(q, all_repos, root)
            if matched:
                targets.append(matched)
            else:
                if not args.json:
                    print(f"❌ Repositório não encontrado para a busca: '{q}'")

    results: List[ResolveResult | Dict[str, Any]] = []
    strategy = getattr(args, "strategy", None)
    show_diff_only = getattr(args, "diff", False)
    preview_mode = getattr(args, "preview", False)
    confirm_mode = getattr(args, "confirm", False)

    if (preview_mode or confirm_mode) and strategy and targets:
        preview_items = []
        for r in targets:
            rel = os.path.relpath(r, root)
            details = get_divergence_details(r, timeout=args.timeout)
            is_dirty = bool(details.get("is_dirty", False))
            dirty_count = len(details.get("dirty_files", []))
            warnings = []
            if is_dirty:
                warnings.append(f"Árvore suja: {dirty_count} arquivo(s)")
            preview_items.append(
                BulkActionItem(
                    repo=r,
                    repo_rel=rel,
                    action=f"RESOLVE ({strategy})",
                    branch=details.get("branch"),
                    ahead=details.get("ahead", 0),
                    behind=details.get("behind", 0),
                    is_dirty=is_dirty,
                    dirty_files_count=dirty_count,
                    warnings=warnings,
                    details=details,
                )
            )

        if preview_mode:
            if args.json:
                print(json.dumps({"preview": True, "action": "resolve", "strategy": strategy, "total": len(preview_items), "items": [i.to_dict() for i in preview_items]}, indent=2))
            else:
                print(format_bulk_action_preview(preview_items, title="PRÉVIA DE RESOLUÇÃO EM LOTE"))
            return 0

        if confirm_mode:
            if args.json:
                print(json.dumps({"error": "Interactive confirmation cannot be prompted in JSON mode. Pass --yes to confirm.", "preview": [i.to_dict() for i in preview_items]}, indent=2))
                return 1
            confirmed = prompt_bulk_confirmation(
                preview_items,
                title="PRÉVIA DE RESOLUÇÃO EM LOTE",
                prompt_text=f"Confirmar resolução ({strategy}) em {len(preview_items)} repositório(s)? [y/N]: ",
            )
            if not confirmed:
                return 0

    is_interactive = (not dry_run) and (getattr(args, "interactive", False) or (
        not args.json and not strategy and not show_diff_only and sys.stdin.isatty()
    ))

    msg_prog = "Simulando resolução..." if dry_run else "Resolvendo divergências..."
    with ProgressIndicator(msg_prog, total=len(targets), enabled=show_progress) as progress:
        for r in targets:
            rel = os.path.relpath(r, root)
            details = get_divergence_details(r, timeout=args.timeout)

            if show_diff_only:
                max_tokens = getattr(args, "max_tokens", None)
                if max_tokens is None and (getattr(args, "compact", False) or getattr(args, "summary", False)):
                    max_tokens = DEFAULT_COMPACT_DIFF_TOKENS
                diff_text = get_divergence_diff(
                    r,
                    branch=details.get("branch"),
                    timeout=args.timeout,
                    max_tokens=max_tokens,
                )
                if args.json:
                    results.append({"repo": r, "repo_rel": rel, "details": details, "diff": diff_text, "dry_run": dry_run})
                else:
                    print(f"\n📂 Diff de divergência para {rel} ({details.get('branch')} ↔ {details.get('upstream')}):")
                    print("=" * 60)
                    print(format_diff_stat(diff_text) if diff_text else "(Nenhuma diferença ou sem upstream)")
                progress.advance(rel)
                continue

            res: ResolveResult | Dict[str, Any]
            if strategy:
                res = resolve_divergence(r, strategy=strategy, branch=details.get("branch"), timeout=args.timeout, dry_run=dry_run)
                res["repo_rel"] = rel
                res["details"] = details
                results.append(res)
                progress.advance(rel)
                if not args.json:
                    mode_str = "[DRY-RUN] " if res.get("dry_run") else ""
                    icon = "ℹ️" if res.get("dry_run") else ("✅" if res["success"] else "❌")
                    status_msg = res.get("message") or res.get("stderr") or "OK"
                    print(f"{icon} {mode_str}[{rel}] ({details.get('branch')} -> {strategy}): {status_msg}")
            elif is_interactive:
                config_obj = getattr(args, "_config", None)
                max_tokens = getattr(args, "max_tokens", None) or getattr(config_obj, "max_tokens", None)
                res = _prompt_resolution_choice(r, rel, details, timeout=args.timeout, max_tokens=max_tokens)
                res["repo_rel"] = rel
                res["details"] = details
                results.append(res)
                progress.advance(rel)
            else:
                # Non-interactive without strategy or diff: report details
                res = {
                    "repo": r,
                    "repo_rel": rel,
                    "strategy": "none",
                    "success": True,
                    "details": details,
                    "dry_run": dry_run,
                }
                results.append(res)
                progress.advance(rel)
                if not args.json:
                    mode_str = "[DRY-RUN] " if dry_run else ""
                    print(f"⚠️ {mode_str}[{rel}] ({details.get('branch')} ↔ {details.get('upstream')}): {details.get('ahead')} à frente, {details.get('behind')} atrás")

    if args.json:
        print(json.dumps(results, indent=2))

    has_failures = any(not r.get("success", True) for r in results)
    return 1 if has_failures else 0


def cmd_push(args: argparse.Namespace) -> int:
    root = args.root
    exclude = getattr(args, "exclude", None)
    remote = getattr(args, "remote", None)
    use_cache = not getattr(args, "no_cache", False)
    cache = BulkCache(enabled=use_cache)
    controller = AdaptiveConcurrencyController()
    auto_tuned = getattr(args, "auto_tune", False) or (args.workers == "auto")
    benchmark = TelemetryBenchmark(workers=args.workers, auto_tuned=auto_tuned)
    retries = getattr(args, "retries", DEFAULT_RETRIES)

    if getattr(args, "preflight", False):
        if not args.json:
            print("🩺 Executando verificação prévia de credenciais...")
        preflight_res = check_credentials_sanity(root, group=getattr(args, "group", None), timeout=args.timeout, test_remotes=True)
        if not preflight_res["ok"]:
            if args.json:
                print(json.dumps({"error": "Preflight credential sanity check failed", "preflight": preflight_res}, indent=2))
                return 1
            print("❌ Falha na verificação prévia de credenciais:")
            for err in preflight_res["errors"]:
                print(f"  • {err}")
            for sug in preflight_res["suggestions"]:
                print(f"  💡 {sug}")
            return 1
        elif not args.json:
            print("✅ Pre-flight: credenciais verificadas com sucesso.")

    t0 = time.perf_counter()
    all_repos = discover_repos(root, exclude=exclude)
    all_repos = _filter_by_group(
        all_repos,
        root,
        group=getattr(args, "group", None),
        groups=getattr(args, "groups", None),
    )
    t_disc = time.perf_counter() - t0
    benchmark.record_stage("discovery", t_disc)
    targets: List[str] = []

    if args.all:
        if not args.json:
            mode_prefix = "[DRY-RUN] " if args.dry_run else ""
            print(f"🔍 {mode_prefix}Identificando repositórios com commits pendentes de envio...")
        do_fetch = not getattr(args, "no_fetch", False)
        t1 = time.perf_counter()
        configured_w = None if auto_tuned else args.workers
        effective_workers = controller.determine_workers(
            len(all_repos),
            is_fetch=do_fetch,
            configured_workers=configured_w,
        )
        benchmark.active_workers = effective_workers
        benchmark.optimal_workers = effective_workers
        pending_repos = find_push_pending_repos(
            all_repos, root, do_fetch=do_fetch, workers=effective_workers, timeout=args.timeout, cache=cache, remote=remote
        )
        t_insp = time.perf_counter() - t1
        benchmark.record_stage("inspection", t_insp)
        targets = [r["path"] for r in pending_repos]
        if not targets:
            benchmark.stop()
            if args.json:
                res_obj: Dict[str, Any] = {"success": True, "message": "No repositories have pending commits to push.", "pushed": []}
                if getattr(args, "benchmark", False):
                    res_obj["telemetry"] = benchmark.to_dict()
                print(json.dumps(res_obj))
            else:
                print("✅ Nenhum repositório possui commits pendentes de envio.")
                if getattr(args, "benchmark", False):
                    _print_benchmark_output(benchmark, getattr(args, "json", False))
            return 0
    else:
        if not args.repos:
            if args.json:
                print(json.dumps({"error": "Specify repository name(s) or pass --all"}))
            else:
                print("Especifique um ou mais repositórios ou use --all")
            return 1
        for q in args.repos:
            matched = match_repo(q, all_repos, root)
            if matched:
                targets.append(matched)
            else:
                if not args.json:
                    print(f"❌ Repositório não encontrado para a busca: '{q}'")

    push_results: List[PushResult | Dict[str, Any]] = []
    preview_mode = getattr(args, "preview", False)
    confirm_mode = getattr(args, "confirm", False)
    force = getattr(args, "force", False)
    block_prot = getattr(args, "block_protected", False)
    allow_prot = getattr(args, "allow_protected", False)
    prot_branches = getattr(args, "effective_protected_branches", DEFAULT_PROTECTED_BRANCHES)

    if (preview_mode or confirm_mode) and targets:
        pending_map = {r["path"]: r for r in pending_repos} if args.all else {}
        preview_items: List[BulkActionItem] = []
        for r in targets:
            rel = os.path.relpath(r, root)
            chk_info: Dict[str, Any]
            if r in pending_map:
                chk_info = dict(pending_map[r])
            else:
                chk_info = dict(inspect_repo(r, root, do_fetch=False, timeout=args.timeout))
            branch = str(chk_info.get("branch") or args.branch or "")
            ahead = int(chk_info.get("ahead", 0))
            is_prot = is_branch_protected(branch, prot_branches) if branch else False
            warnings: List[str] = []
            if is_prot:
                warnings.append(f"Branch protegida: {branch}")
            if force:
                warnings.append("Push forçado (--force)")
            aux_ahead = chk_info.get("aux_branches_ahead") or {}
            if aux_ahead:
                aux_desc = ", ".join(f"{b} (+{cnt})" for b, cnt in aux_ahead.items())
                warnings.append(f"{len(aux_ahead)} branch(es) secundária(s) pendente(s) de envio ({aux_desc})")
            action_label = "PUSH (--force)" if force else "PUSH"
            preview_items.append(
                BulkActionItem(
                    repo=r,
                    repo_rel=rel,
                    action=action_label,
                    branch=branch,
                    ahead=ahead,
                    is_protected=is_prot,
                    warnings=warnings,
                    details=chk_info,
                )
            )

        if preview_mode:
            if args.json:
                print(json.dumps({"preview": True, "action": "push", "total": len(preview_items), "items": [i.to_dict() for i in preview_items]}, indent=2))
            else:
                print(format_bulk_action_preview(preview_items, title="PRÉVIA DE ENVIO EM LOTE (BULK PUSH PREVIEW)"))
            return 0

        if confirm_mode:
            if args.json:
                print(json.dumps({"error": "Interactive confirmation cannot be prompted in JSON mode. Pass --yes to confirm.", "preview": [i.to_dict() for i in preview_items]}, indent=2))
                return 1
            confirmed = prompt_bulk_confirmation(
                preview_items,
                title="PRÉVIA DE ENVIO EM LOTE (BULK PUSH PREVIEW)",
                prompt_text=f"Confirmar envio de commits em {len(preview_items)} repositório(s)? [y/N]: ",
            )
            if not confirmed:
                return 0

    t2 = time.perf_counter()
    show_progress = False if (args.json or getattr(args, "compact", False) or getattr(args, "summary", False)) else None
    msg_prog = "Simulando push..." if args.dry_run else "Enviando commits..."

    with ProgressIndicator(msg_prog, total=len(targets), enabled=show_progress) as progress:
        for r in targets:
            rel = os.path.relpath(r, root)
            res = push_repo(
                r,
                branch=args.branch,
                remote=remote,
                dry_run=args.dry_run,
                force=force,
                protected_branches=prot_branches,
                block_protected=block_prot,
                allow_protected=allow_prot,
                timeout=args.timeout,
                cache=cache,
                benchmark=benchmark,
                retries=retries,
                sync_branches=getattr(args, "sync_branches", False),
            )
            res["repo_rel"] = rel
            push_results.append(res)
            prog_label = f"{rel} [worktree]" if res.get("is_worktree") else rel
            progress.advance(prog_label)

            if not args.json:
                render_push_item(res, dry_run=args.dry_run)

    t_push = time.perf_counter() - t2
    benchmark.record_stage("push", t_push)
    benchmark.total_repos = len(targets)
    configured_w = None if auto_tuned else args.workers
    effective_workers = controller.determine_workers(
        len(all_repos),
        is_fetch=not getattr(args, "no_fetch", False),
        configured_workers=configured_w,
    )
    benchmark.active_workers = effective_workers
    c_stats = cache.stats
    benchmark.cache_hits = c_stats["hits"]
    benchmark.cache_misses = c_stats["misses"]
    benchmark.stop()
    benchmark.evaluate_thresholds(controller)

    if args.json:
        if getattr(args, "benchmark", False):
            print(json.dumps({"results": push_results, "telemetry": benchmark.to_dict()}, indent=2))
        else:
            print(json.dumps(push_results, indent=2))
    else:
        if getattr(args, "benchmark", False):
            _print_benchmark_output(benchmark, getattr(args, "json", False))

    has_failures = any(not r["success"] for r in push_results)
    return 1 if has_failures else 0


def cmd_dirty(args: argparse.Namespace) -> int:
    root = args.root
    exclude = getattr(args, "exclude", None)
    all_repos = discover_repos(root, exclude=exclude)
    all_repos = _filter_by_group(
        all_repos,
        root,
        group=getattr(args, "group", None),
        groups=getattr(args, "groups", None),
    )

    controller = AdaptiveConcurrencyController()
    auto_tuned = getattr(args, "auto_tune", False) or (args.workers == "auto")
    configured_w = None if auto_tuned else args.workers
    effective_workers = controller.determine_workers(
        len(all_repos),
        is_fetch=False,
        configured_workers=configured_w,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as executor:
        inspections = list(executor.map(lambda r: inspect_repo(r, root, do_fetch=False), all_repos))

    dirty = [r for r in inspections if r["is_dirty"]]
    if args.json:
        print(json.dumps([{"repo": r["rel_path"], "files": r["dirty_files"]} for r in dirty], indent=2))
        return 0

    print_dirty_report(dirty)
    return 0


def cmd_config(args: argparse.Namespace, config: GitSyncConfig) -> int:
    """
    Display active workspace profile and resolved runtime configuration,
    or initialize/edit configuration templates.
    # INVARIANT: --init creates templates without clobbering; --edit opens $EDITOR on TTY or prints path on non-TTY.
    """
    if getattr(args, "init", False):
        res = init_config_file(
            target_path=getattr(args, "config", None),
            global_config=getattr(args, "global_config", False),
            root=getattr(args, "root", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
            return 0 if res.get("ok") else 1
        if res.get("action") == "already_exists":
            print(f"ℹ️  {res.get('message')}")
        elif res.get("action") == "created":
            print(f"✨ {res.get('message')}")
        else:
            print(f"❌ {res.get('message')}", file=sys.stderr)
            return 1
        if not getattr(args, "edit", False):
            return 0

    if getattr(args, "edit", False):
        target_path_str = get_default_config_path(
            global_config=getattr(args, "global_config", False),
            custom_path=getattr(args, "config", None) or config.config_path,
        )
        target_path = Path(target_path_str)
        if not target_path.is_file():
            res = init_config_file(
                target_path=str(target_path),
                global_config=getattr(args, "global_config", False),
                root=getattr(args, "root", None),
            )
            if not res.get("ok"):
                if getattr(args, "json", False):
                    print(json.dumps(res, indent=2))
                else:
                    print(f"❌ {res.get('message')}", file=sys.stderr)
                return 1

        is_tty = sys.stdin.isatty() and sys.stdout.isatty()
        if not is_tty:
            if getattr(args, "json", False):
                print(json.dumps({"ok": True, "path": str(target_path), "is_tty": False}, indent=2))
            else:
                print(str(target_path))
            return 0

        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        try:
            p = subprocess.run([editor, str(target_path)])
            return p.returncode
        except FileNotFoundError:
            try:
                p = subprocess.run(["vi", str(target_path)])
                return p.returncode
            except FileNotFoundError:
                print(f"⚠️  Editor '{editor}' not found. Configuration file path: {target_path}", file=sys.stderr)
                return 1

    if getattr(args, "json", False):
        print(json.dumps(config.to_dict(), indent=2))
        return 0

    print("\n⚙️  Configuração ativa do git-sync:")
    print("=" * 60)
    print(f"  Arquivo de config:   {config.config_path or '(nenhum / padrões em uso)'}")
    print(f"  Fontes (sources):    {', '.join(config.sources) if config.sources else 'defaults'}")
    print(f"  Perfil ativo:        {config.active_profile or '(padrão / raiz)'}")
    print(f"  Perfis disponíveis:  {', '.join(config.available_profiles) if config.available_profiles else '(nenhum)'}")
    print(f"  Diretório raiz:      {args.root}")
    print(f"  Workers:             {args.workers}{' (auto-tuned)' if getattr(args, 'auto_tune', False) else ''}")
    print(f"  Auto-tune:           {getattr(args, 'auto_tune', config.auto_tune)}")
    print(f"  Timeout (s):         {args.timeout}")
    print(f"  Exclusões:           {', '.join(args.exclude) if args.exclude else '(nenhuma)'}")
    print(f"  Grupos preferidos:   {', '.join(args.groups) if args.groups else '(todos)'}")
    print(f"  Pull (ff-only):      {getattr(args, 'ff_only', config.pull_ff_only)}")
    print(f"  Pull (sync-branches):{getattr(args, 'sync_branches', config.pull_sync_branches)}")
    print(f"  Pull (strategy):     {getattr(args, 'strategy', config.pull_strategy) or '(nenhuma)'}")
    print(f"  Pull (autostash):    {getattr(args, 'autostash', config.pull_autostash)}")
    print(f"  Pull (dry-run):      {args.dry_run if getattr(args, 'dry_run', None) is not None else config.pull_dry_run}")
    print(f"  Push (dry-run):      {args.dry_run if getattr(args, 'dry_run', None) is not None else config.push_dry_run}")
    print(f"  Confirm (bulk):      {getattr(args, 'confirm', config.confirm)}")
    print(f"  Push (protected):    {', '.join(getattr(config, 'push_protected_branches', DEFAULT_PROTECTED_BRANCHES))}")
    print(f"  Push (block-prot):   {getattr(config, 'push_block_protected', False)}")
    print(f"  Push (sync-branches):{getattr(args, 'push_sync_branches', getattr(config, 'push_sync_branches', False))}")
    print(f"  Status (compact):    {getattr(args, 'compact', config.status_compact)}")
    print(f"  Benchmark:           {getattr(args, 'benchmark', config.benchmark)}")
    print(f"  Bulk Cache:          {not getattr(args, 'no_cache', not config.use_cache)}")
    print(f"  Remote:              {getattr(args, 'remote', config.remote) or '(automático / origin)'}")
    print(f"  Retries:             {getattr(args, 'retries', config.retries)}")
    print(f"  Pre-flight:          {getattr(args, 'preflight', config.preflight)}")
    print("=" * 60 + "\n")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """
    Install or uninstall global symlink to git-sync launcher in target directory.
    # INVARIANT: Preserves idempotency and provides actionable feedback on PATH integration.
    """
    target_dir = getattr(args, "target_dir", None)
    force = getattr(args, "force", False)
    uninstall = getattr(args, "uninstall", False)

    if uninstall:
        result = uninstall_symlink(target_dir=target_dir)
        skill_results = uninstall_agent_skills()
        result["skills"] = skill_results
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        if result["ok"]:
            print(f"🗑️  {result['message']}")
            for sk in skill_results:
                if sk.get("ok") and sk.get("action") == "uninstalled":
                    print(f"🗑️  {sk['message']}")
            return 0
        else:
            print(f"❌ {result['error']}", file=sys.stderr)
            return 1

    result = install_symlink(target_dir=target_dir, force=force)
    skill_results = install_agent_skills(force=force)
    result["skills"] = skill_results
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if not result["ok"]:
        print(f"❌ Erro na instalação: {result['error']}", file=sys.stderr)
        return 1

    if result["action"] == "already_installed":
        print(f"✅ {result['message']}")
    else:
        print(f"🚀 {result['message']}")

    for sk in skill_results:
        if sk.get("ok"):
            if sk.get("action") == "installed":
                print(f"🧠 {sk['message']}")
            elif sk.get("action") == "already_installed":
                print(f"🧠 {sk['message']}")
        else:
            print(f"⚠️  {sk.get('error')}", file=sys.stderr)

    if not result["in_path"]:
        install_dir = str(Path(result["path"]).parent)
        print("\n⚠️  ATENÇÃO: O diretório de instalação não está presente no seu PATH!")
        print("   Adicione a seguinte linha ao seu ~/.bashrc ou ~/.zshrc:")
        print(f'   export PATH="{install_dir}:$PATH"\n')

    return 0


def cmd_completion(args: argparse.Namespace) -> int:
    """
    Generate or install shell completion script for Bash or Zsh.
    # INVARIANT: Emits clean zero-dependency completion scripts compatible with standard shell engines.
    """
    shell = getattr(args, "shell", "bash")
    do_install = getattr(args, "install", False)
    target_dir = getattr(args, "target_dir", None)

    if do_install:
        result = install_completion(shell=shell, target_dir=target_dir)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        if result["ok"]:
            print(f"✅ {result['message']}")
            if result.get("instructions"):
                print(f"\n{result['instructions']}\n")
            return 0
        else:
            print(f"❌ {result['error']}", file=sys.stderr)
            return 1

    try:
        script = generate_completion_script(shell=shell)
    except ValueError as err:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(err)}, indent=2))
        else:
            print(f"❌ {err}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "shell": shell, "script": script}, indent=2))
    else:
        sys.stdout.write(script)
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    """
    Safely inspect and recover locked repositories (.git/*.lock).
    # INVARIANT: Preserves active locks (<300s) unless --force is specified to prevent data corruption.
    """
    root = args.root
    all_repos = discover_repos(root, exclude=args.exclude)
    filtered = _filter_by_group(
        all_repos,
        root,
        group=getattr(args, "group", None),
        groups=getattr(args, "groups", None),
    )

    targets: List[str] = []
    if getattr(args, "unlock_all", False):
        targets = filtered
    elif getattr(args, "repos", None):
        for q in args.repos:
            matched = match_repo(q, filtered, root)
            if matched:
                targets.append(matched)
            else:
                if not args.json:
                    print(f"⚠️  Repositório '{q}' não encontrado em {root}")
    else:
        targets = filtered

    locked_items = find_locked_repos(root, repos=targets, timeout=args.timeout)

    if not locked_items:
        if args.json:
            print(json.dumps({"root": root, "total_locked": 0, "unlocked": []}, indent=2))
        else:
            print(f"\n✅ Nenhum arquivo de lock ativo ou stale (.git/*.lock) encontrado em {root}.\n")
        return 0

    results = []
    for lk in locked_items:
        repo_path = lk["repo"]
        lock_path = Path(lk["lock_file"])
        age = lk["age_seconds"]
        is_stale = lk["stale"]
        force = getattr(args, "force", False)

        if not force and not is_stale:
            results.append({
                "repo": repo_path,
                "rel_path": lk["rel_path"],
                "lock_name": lk["lock_name"],
                "age_seconds": age,
                "status": "SKIPPED_ACTIVE",
                "message": f"Lock is active (age: {age:.1f}s < threshold 300s). Use --force to override.",
            })
            continue

        try:
            lock_path.unlink()
            results.append({
                "repo": repo_path,
                "rel_path": lk["rel_path"],
                "lock_name": lk["lock_name"],
                "age_seconds": age,
                "status": "UNLOCKED",
                "message": f"Successfully removed {'stale ' if is_stale else 'active '}lock '{lk['lock_name']}'",
            })
        except Exception as exc:
            results.append({
                "repo": repo_path,
                "rel_path": lk["rel_path"],
                "lock_name": lk["lock_name"],
                "age_seconds": age,
                "status": "ERROR",
                "message": f"Failed to remove lock: {exc}",
            })

    if args.json:
        print(json.dumps({
            "root": root,
            "total_locked": len(locked_items),
            "unlocked": results,
        }, indent=2))
        return 0

    print_unlock_report(results)
    return 0


def cmd_worktree(args: argparse.Namespace) -> int:
    """
    Inspect or prune linked worktrees across repository workspaces.
    """
    action = getattr(args, "worktree_action", None)
    if action == "prune":
        return cmd_worktree_prune(args)
    return cmd_worktree_list(args)


def cmd_worktree_prune(args: argparse.Namespace) -> int:
    """
    Clean missing worktree metadata and orphaned admin directories, and warn on dirty linked worktrees.
    # INVARIANT: Preserves active dirty linked worktrees and warns without deleting working files.
    """
    root = args.root
    all_repos = discover_repos(root, exclude=args.exclude)
    filtered = _filter_by_group(
        all_repos,
        root,
        group=getattr(args, "group", None),
        groups=getattr(args, "groups", None),
    )

    targets: List[str] = []
    if getattr(args, "prune_all", False):
        targets = filtered
    elif getattr(args, "repos", None):
        for q in args.repos:
            matched = match_repo(q, filtered, root)
            if matched:
                targets.append(matched)
            else:
                if not getattr(args, "json", False):
                    print(f"⚠️  Repositório '{q}' não encontrado em {root}")
    else:
        targets = filtered

    dry_run = getattr(args, "dry_run", False)
    expire = getattr(args, "expire", "now")
    cache = BulkCache(enabled=not getattr(args, "no_cache", False))
    preview_mode = getattr(args, "preview", False)
    confirm_mode = getattr(args, "confirm", False)

    if (preview_mode or confirm_mode) and targets:
        sim_pruned_repos: List[Dict[str, Any]] = []
        for repo_path in targets:
            res_sim = prune_repo_worktrees(
                repo_path,
                dry_run=True,
                expire=expire,
                timeout=args.timeout,
                cache=cache,
                root_dir=root,
            )
            if res_sim["pruned"]:
                sim_pruned_repos.append(res_sim)

        preview_items: List[BulkActionItem] = []
        for pr in sim_pruned_repos:
            for p_item in pr["pruned"]:
                preview_items.append(
                    BulkActionItem(
                        repo=pr["repo"],
                        repo_rel=pr["rel_path"],
                        action="PRUNE WORKTREE",
                        warnings=[f"{p_item.get('name', '')}: {p_item.get('reason', '')}"],
                        details=p_item,
                    )
                )

        if preview_mode:
            if getattr(args, "json", False):
                print(json.dumps({"preview": True, "action": "worktree_prune", "total": len(preview_items), "items": [i.to_dict() for i in preview_items]}, indent=2))
            else:
                print(format_bulk_action_preview(preview_items, title="PRÉVIA DE LIMPEZA DE WORKTREES"))
            return 0

        if confirm_mode:
            if getattr(args, "json", False):
                print(json.dumps({"error": "Interactive confirmation cannot be prompted in JSON mode. Pass --yes to confirm.", "preview": [i.to_dict() for i in preview_items]}, indent=2))
                return 1
            if preview_items:
                confirmed = prompt_bulk_confirmation(
                    preview_items,
                    title="PRÉVIA DE LIMPEZA DE WORKTREES",
                    prompt_text=f"Confirmar poda de {len(preview_items)} worktree(s)? [y/N]: ",
                )
                if not confirmed:
                    return 0

    pruned_repos: List[Dict[str, Any]] = []
    all_warnings: List[str] = []
    total_pruned = 0

    for repo_path in targets:
        res = prune_repo_worktrees(
            repo_path,
            dry_run=dry_run,
            expire=expire,
            timeout=args.timeout,
            cache=cache,
            root_dir=root,
        )
        if res["pruned"] or res["dirty_worktrees"] or getattr(args, "repos", None):
            pruned_repos.append(res)
            total_pruned += len(res["pruned"])
            all_warnings.extend(res["warnings"])

    if getattr(args, "json", False):
        print(json.dumps({
            "root": root,
            "dry_run": dry_run,
            "total_checked": len(targets),
            "total_pruned": total_pruned,
            "total_warnings": len(all_warnings),
            "pruned_repos": pruned_repos,
        }, indent=2))
        return 0

    print_worktree_prune_report(pruned_repos, total_pruned, all_warnings, root, dry_run=dry_run)
    return 0


def cmd_worktree_list(args: argparse.Namespace) -> int:
    """
    List linked worktrees across repositories.
    """
    root = args.root
    all_repos = discover_repos(root, exclude=args.exclude)
    filtered = _filter_by_group(
        all_repos,
        root,
        group=getattr(args, "group", None),
        groups=getattr(args, "groups", None),
    )

    targets: List[str] = []
    if getattr(args, "repos", None):
        for q in args.repos:
            matched = match_repo(q, filtered, root)
            if matched:
                targets.append(matched)
            else:
                if not getattr(args, "json", False):
                    print(f"⚠️  Repositório '{q}' não encontrado em {root}")
    else:
        targets = filtered

    cache = BulkCache(enabled=not getattr(args, "no_cache", False))
    entries: List[Dict[str, Any]] = []

    for repo_path in targets:
        wt_info = cache.get_worktree_info(repo_path, timeout=args.timeout) if cache else get_worktree_info(repo_path, timeout=args.timeout)
        worktrees = wt_info.get("worktrees", [])
        if len(worktrees) > 1 or any(w.get("is_prunable") for w in worktrees):
            rel = os.path.relpath(repo_path, root)
            entries.append({
                "repo": repo_path,
                "rel_path": rel,
                "is_worktree": wt_info.get("is_worktree", False),
                "main_worktree": wt_info.get("main_worktree"),
                "worktrees": worktrees,
                "stale_worktrees": wt_info.get("stale_worktrees", []),
            })

    if getattr(args, "json", False):
        print(json.dumps({
            "root": root,
            "total_repos_with_worktrees": len(entries),
            "repositories": entries,
        }, indent=2))
        return 0

    print_worktree_list_report(entries, root)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint with support for workspace profiles and custom configurations."""
    if argv is None:
        argv = sys.argv[1:]

    # Fast hook for shell completion dynamic repo lookup
    if "--complete-repos" in argv:
        pre_parser = argparse.ArgumentParser(add_help=False)
        pre_parser.add_argument("--config", help="Path to custom .git-sync.json configuration file")
        pre_parser.add_argument("--profile", help="Named workspace profile to activate")
        pre_args, _ = pre_parser.parse_known_args(argv)
        try:
            cfg = load_config(config_path=pre_args.config, profile_name=pre_args.profile)
        except Exception:
            cfg = GitSyncConfig()
        repos = discover_repos(cfg.root, exclude=cfg.exclude)
        for repo_path in repos:
            rel = os.path.relpath(repo_path, cfg.root)
            print(rel)
        return 0

    # 1. Pre-parse configuration path and workspace profile
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", help="Path to custom .git-sync.json configuration file")
    pre_parser.add_argument("--profile", help="Named workspace profile to activate")
    pre_args, _ = pre_parser.parse_known_args(argv)

    try:
        config = load_config(config_path=pre_args.config, profile_name=pre_args.profile)
    except FileNotFoundError as exc:
        if "config" in argv and ("--init" in argv or "--edit" in argv):
            config = GitSyncConfig()
            if pre_args.config:
                config.config_path = str(Path(pre_args.config).expanduser().resolve())
        else:
            print(f"❌ Configuration error: {exc}", file=sys.stderr)
            return 1
    except (ValueError, OSError) as exc:
        print(f"❌ Configuration error: {exc}", file=sys.stderr)
        return 1

    if "cli" not in config.sources:
        config.sources.append("cli")

    # 2. Setup common parser options with SUPPRESS to allow arguments before or after subcommands
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help="Path to custom .git-sync.json configuration file")
    common.add_argument("--profile", default=argparse.SUPPRESS, help="Named workspace profile to activate")
    common.add_argument("--root", default=argparse.SUPPRESS, help=f"Root directory (default: {config.root})")
    common.add_argument("--workers", type=_parse_workers_arg, default=argparse.SUPPRESS, help=f"Concurrent workers count or 'auto' (default: {config.workers})")
    common.add_argument("--auto-tune", "--adaptive", dest="auto_tune", action="store_true", default=argparse.SUPPRESS, help="Dynamically auto-tune worker concurrency based on host cores and repository count")
    common.add_argument("--timeout", type=int, default=argparse.SUPPRESS, help=f"Git timeout in seconds (default: {config.timeout})")
    common.add_argument("--retries", type=int, default=argparse.SUPPRESS, help=f"Git network retry attempts on transient errors (default: {config.retries})")
    common.add_argument("--preflight", action="store_true", default=argparse.SUPPRESS, help="Run pre-flight credential and remote sanity check before executing operations")
    common.add_argument("--exclude", nargs="*", default=argparse.SUPPRESS, help="Directories or patterns to exclude from repo discovery")
    common.add_argument("--compact", action="store_true", default=argparse.SUPPRESS, help="Output compact summary omitting repository items array (recommended for AI agents)")
    common.add_argument("--summary", dest="compact", action="store_true", default=argparse.SUPPRESS, help="Alias for --compact")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Output machine-readable JSON")
    common.add_argument("--benchmark", action="store_true", default=argparse.SUPPRESS, help="Measure and display execution performance telemetry")
    common.add_argument("--telemetry", dest="benchmark", action="store_true", default=argparse.SUPPRESS, help="Alias for --benchmark")
    common.add_argument("--no-cache", action="store_true", default=argparse.SUPPRESS, help="Disable in-memory bulk cache across worktree and remote audits")
    common.add_argument("--remote", default=argparse.SUPPRESS, help="Target specific Git remote (e.g. origin, upstream, fork)")
    common.add_argument("--dry-run", dest="dry_run", action="store_true", default=argparse.SUPPRESS, help="Simulate execution without modifying repositories")
    common.add_argument("--no-dry-run", dest="dry_run", action="store_false", default=argparse.SUPPRESS, help="Disable dry-run simulation mode")
    common.add_argument("--confirm", dest="confirm", action="store_true", default=argparse.SUPPRESS, help="Prompt for interactive confirmation with safety preview before mutating repositories")
    common.add_argument("--no-confirm", dest="confirm", action="store_false", default=argparse.SUPPRESS, help="Disable confirmation prompts")
    common.add_argument("-y", "--yes", dest="confirm", action="store_false", default=argparse.SUPPRESS, help="Automatic yes to confirmation prompts; run non-interactively without prompting")
    common.add_argument("--preview", dest="preview", action="store_true", default=argparse.SUPPRESS, help="Show formatted bulk action preview and safety summary without mutating repositories")
    common.add_argument("--dashboard", "--tui", dest="dashboard", action="store_true", default=argparse.SUPPRESS, help="Launch interactive terminal dashboard")
    common.add_argument("--max-tokens", type=int, default=argparse.SUPPRESS, help="Maximum token budget for diff inspection (~4 chars/token)")
    common.add_argument("--changelog", action="store_true", default=argparse.SUPPRESS, help="Summarize pending incoming and outgoing commit categories (Conventional Commits)")
    common.add_argument("--submodules", action="store_true", default=argparse.SUPPRESS, help="Inspect drift and recursively update git submodules")
    common.add_argument("--no-submodules", dest="submodules", action="store_false", default=argparse.SUPPRESS, help="Do not update or inspect submodules")
    common.add_argument("--unshallow", action="store_true", default=argparse.SUPPRESS, help="Fetch full history for shallow submodules and repositories")
    common.add_argument("--reapply-sparse", action="store_true", default=argparse.SUPPRESS, help="Reapply sparse-checkout cone patterns to prevent unbounded checkout conflicts")
    common.add_argument("--lfs", dest="lfs", action="store_true", default=argparse.SUPPRESS, help="Explicitly pull Git LFS objects for repositories utilizing Git LFS")
    common.add_argument("--no-lfs", dest="no_lfs", action="store_true", default=argparse.SUPPRESS, help="Skip downloading large Git LFS binary payloads (GIT_LFS_SKIP_SMUDGE=1) for bandwidth savings")

    parser = argparse.ArgumentParser(
        prog="git-sync",
        description="Fast concurrent multi-repo synchronization for GitHub workspaces.",
        parents=[common],
    )
    parser.add_argument("-v", "--version", action="version", version=f"git-sync {__version__}")
    # Global flags on root parser (when invoked without subcommand)
    parser.add_argument("--fetch", dest="fetch", action="store_true", default=None, help="Fetch remotes")
    parser.add_argument("--no-fetch", dest="fetch", action="store_false", default=None, help="Skip remote fetch (instant local check)")
    parser.add_argument("--pending", action="store_true", default=None, help="Only show repos with pending actions or dirty files")
    parser.add_argument("--group", default=None, help="Filter by subfolder/group")
    parser.add_argument("-i", "--interactive", dest="interactive", action="store_true", default=False, help="Launch interactive terminal dashboard")

    subparsers = parser.add_subparsers(dest="command")

    # doctor
    p_doctor = subparsers.add_parser("doctor", parents=[common], help="Run proactive credential and connectivity diagnostics")
    p_doctor.add_argument("--group", default=None, help="Filter target repositories by group")
    p_doctor.add_argument("--ssh", action="store_true", default=False, help="Audit SSH connection multiplexing (ControlMaster) and measure latency")
    p_doctor.add_argument("--prune-ssh", action="store_true", default=False, help="Auto-prune stale or defunct SSH ControlMaster multiplexing sockets")

    # status
    p_status = subparsers.add_parser("status", parents=[common], help="Show status of all repositories")
    p_status.add_argument("--group", default=None, help="Filter by subfolder/group (e.g. work, personal, oss)")
    p_status.add_argument("--fetch", dest="fetch", action="store_true", default=None, help="Fetch remotes")
    p_status.add_argument("--no-fetch", dest="fetch", action="store_false", default=None, help="Skip remote fetch (instant local check)")
    p_status.add_argument("--pending", action="store_true", default=None, help="Only show repos with pending actions or dirty files")
    p_status.add_argument("-i", "--interactive", dest="interactive", action="store_true", default=False, help="Launch interactive terminal dashboard")

    # dashboard / tui
    for dash_cmd in ("dashboard", "tui"):
        p_dash = subparsers.add_parser(dash_cmd, parents=[common], help="Launch interactive terminal dashboard")
        p_dash.add_argument("--group", default=None, help="Filter by subfolder/group")
        p_dash.add_argument("--fetch", dest="fetch", action="store_true", default=None, help="Fetch remotes")
        p_dash.add_argument("--no-fetch", dest="fetch", action="store_false", default=None, help="Skip remote fetch (instant local check)")
        p_dash.add_argument("--pending", action="store_true", default=None, help="Only show repos with pending actions or dirty files")

    # pull
    p_pull = subparsers.add_parser("pull", parents=[common], help="Pull changes for specified repositories or --all")
    p_pull.add_argument("repos", nargs="*", help="Repository name(s) or patterns to pull")
    p_pull.add_argument("--all", dest="pull_all", action="store_true", default=None, help="Pull all repositories with pending commits")
    p_pull.add_argument("--ff-only", dest="ff_only", action="store_true", default=None, help="Require fast-forward merge")
    p_pull.add_argument("--no-ff-only", dest="ff_only", action="store_false", default=None, help="Allow non fast-forward merge")
    p_pull.add_argument("--sync-branches", dest="sync_branches", action="store_true", default=None, help="Fast-forward auxiliary tracking branches")
    p_pull.add_argument("--no-sync-branches", dest="sync_branches", action="store_false", default=None, help="Skip fast-forward auxiliary tracking branches")
    p_pull.add_argument("--autostash", dest="autostash", action="store_true", default=None, help="Automatically stash dirty changes before pull and restore after")
    p_pull.add_argument("--no-autostash", dest="autostash", action="store_false", default=None, help="Do not autostash dirty changes")
    p_pull.add_argument("--strategy", choices=["abort", "rebase", "merge"], default=None, help="Fallback resolution strategy if fast-forward is refused")
    p_pull.add_argument("-i", "--interactive", dest="interactive", action="store_true", default=False, help="Prompt interactively if fast-forward fails due to divergence")
    p_pull.add_argument("--group", default=None, help="Filter target repositories by group")
    p_pull.add_argument("--recover-locks", action="store_true", default=False, help="Automatically recover stale Git index locks (>300s) before pulling")

    # resolve
    p_resolve = subparsers.add_parser("resolve", parents=[common], help="Inspect and resolve divergent branches")
    p_resolve.add_argument("repos", nargs="*", help="Repository name(s) or patterns to resolve")
    p_resolve.add_argument("--all", dest="resolve_all", action="store_true", default=None, help="Resolve all repositories with divergent branches")
    p_resolve.add_argument("--strategy", choices=["abort", "rebase", "merge", "stash"], default=None, help="Automated non-interactive resolution strategy")
    p_resolve.add_argument("--diff", action="store_true", default=False, help="Display divergence diff without applying changes")
    p_resolve.add_argument("-i", "--interactive", dest="interactive", action="store_true", default=False, help="Prompt interactively for resolution strategy")
    p_resolve.add_argument("--group", default=None, help="Filter target repositories by group")
    p_resolve.add_argument("--fetch", dest="fetch", action="store_true", default=None, help="Fetch remotes before checking divergence")
    p_resolve.add_argument("--no-fetch", dest="fetch", action="store_false", default=None, help="Skip remote fetch before checking divergence")

    # push
    p_push = subparsers.add_parser("push", parents=[common], help="Push committed changes to remote repository")
    p_push.add_argument("repos", nargs="*", help="Repository name(s) or patterns to push")
    p_push.add_argument("--all", dest="push_all", action="store_true", default=None, help="Push all repositories with pending commits (ahead > 0)")
    p_push.add_argument("--branch", default=None, help="Specific branch to push (defaults to current branch)")
    p_push.add_argument("--fetch", dest="fetch", action="store_true", default=None, help="Fetch remotes before push inspection")
    p_push.add_argument("--no-fetch", dest="fetch", action="store_false", default=None, help="Skip remote fetch before push inspection")
    p_push.add_argument("--group", default=None, help="Filter target repositories by group")
    p_push.add_argument("--force", action="store_true", default=None, help="Force push with lease (strictly prohibited on protected branches)")
    p_push.add_argument("--block-protected", dest="block_protected", action="store_true", default=None, help="Block direct push to protected branches")
    p_push.add_argument("--allow-protected", dest="allow_protected", action="store_true", default=None, help="Allow direct push to protected branches when blocked by policy")
    p_push.add_argument("--protected-branches", default=None, help="Comma-separated patterns of protected branches (default: main,master,release/*,production,prod)")
    p_push.add_argument("--sync-branches", "--all-branches", dest="sync_branches", action="store_true", default=None, help="Push auxiliary tracking branches ahead of upstream")
    p_push.add_argument("--no-sync-branches", dest="sync_branches", action="store_false", default=None, help="Skip pushing auxiliary tracking branches")

    # dirty
    p_dirty = subparsers.add_parser("dirty", parents=[common], help="Show repositories with uncommitted or untracked changes")
    p_dirty.add_argument("--group", default=None, help="Filter repositories by group")

    # config
    p_config = subparsers.add_parser("config", parents=[common], help="Display active workspace profile and configuration")
    p_config.add_argument("--init", action="store_true", default=False, help="Initialize default .git-sync.json configuration template if missing")
    p_config.add_argument("--edit", action="store_true", default=False, help="Open configuration file in $EDITOR or print path if non-TTY")
    p_config.add_argument("--global", dest="global_config", action="store_true", default=False, help="Target global user configuration (~/.git-sync.json or XDG) instead of workspace")

    # install
    p_install = subparsers.add_parser("install", parents=[common], help="Install or update global symlink in ~/.local/bin")
    p_install.add_argument("--target-dir", default=None, help="Directory to install executable (default: ~/.local/bin)")
    p_install.add_argument("--force", action="store_true", default=False, help="Overwrite existing symlink or file")
    p_install.add_argument("--uninstall", action="store_true", default=False, help="Uninstall global symlink")

    # completion
    p_completion = subparsers.add_parser("completion", parents=[common], help="Generate shell autocompletion script")
    p_completion.add_argument("shell", choices=["bash", "zsh"], help="Target shell (bash or zsh)")
    p_completion.add_argument("--install", dest="install", action="store_true", default=False, help="Install completion script to standard shell directory")
    p_completion.add_argument("--target-dir", default=None, help="Custom target directory for completion script")

    # unlock
    p_unlock = subparsers.add_parser("unlock", parents=[common], help="Safely inspect and recover locked repositories (.git/*.lock)")
    p_unlock.add_argument("repos", nargs="*", help="Repository name(s) or patterns to unlock")
    p_unlock.add_argument("--all", dest="unlock_all", action="store_true", default=None, help="Unlock all repositories with locked index or ref files")
    p_unlock.add_argument("--force", action="store_true", default=False, help="Force remove locks even if recently active (<300s)")
    p_unlock.add_argument("--group", default=None, help="Filter target repositories by group")

    # worktree
    p_worktree = subparsers.add_parser("worktree", parents=[common], help="Inspect or prune linked worktree metadata")
    wt_sub = p_worktree.add_subparsers(dest="worktree_action")

    # worktree prune
    p_wt_prune = wt_sub.add_parser("prune", parents=[common], help="Clean missing worktree metadata and orphaned admin directories")
    p_wt_prune.add_argument("repos", nargs="*", help="Repository name(s) or patterns to prune")
    p_wt_prune.add_argument("--all", dest="prune_all", action="store_true", default=None, help="Prune worktrees across all repositories")
    p_wt_prune.add_argument("--expire", default="now", help="Only prune worktree administrative entries older than <expire> (default: now)")
    p_wt_prune.add_argument("--group", default=None, help="Filter target repositories by group")

    # worktree list
    p_wt_list = wt_sub.add_parser("list", parents=[common], help="List linked worktrees across repositories")
    p_wt_list.add_argument("repos", nargs="*", help="Repository name(s) or patterns to list")
    p_wt_list.add_argument("--group", default=None, help="Filter target repositories by group")

    args = parser.parse_args(argv)
    args._config = config

    if not args.command:
        args.command = "status"
    elif args.command in ("dashboard", "tui"):
        args.command = "status"
        args.dashboard = True

    # Precedence resolution: Explicit CLI args > Selected Profile > Top-level Config > Defaults
    args.root = getattr(args, "root", None) or config.root
    raw_workers = getattr(args, "workers", None)
    auto_tune_flag = getattr(args, "auto_tune", None)

    if auto_tune_flag is not None:
        args.auto_tune = auto_tune_flag
    else:
        args.auto_tune = config.auto_tune

    if raw_workers is not None:
        if str(raw_workers).strip().lower() == "auto":
            args.workers = "auto"
            args.auto_tune = True
        else:
            args.workers = int(raw_workers)
    else:
        args.workers = config.workers
        if str(config.workers).strip().lower() == "auto":
            args.auto_tune = True
    args.timeout = getattr(args, "timeout", None) or config.timeout
    args.exclude = getattr(args, "exclude", None) if getattr(args, "exclude", None) is not None else list(config.exclude)
    args.groups = list(config.groups)
    args.json = getattr(args, "json", False)

    benchmark_flag = getattr(args, "benchmark", None)
    args.benchmark = benchmark_flag if benchmark_flag is not None else config.benchmark

    no_cache_flag = getattr(args, "no_cache", None)
    args.no_cache = no_cache_flag if no_cache_flag is not None else (not config.use_cache)

    retries_flag = getattr(args, "retries", None)
    args.retries = retries_flag if retries_flag is not None else config.retries

    preflight_flag = getattr(args, "preflight", None)
    args.preflight = preflight_flag if preflight_flag is not None else config.preflight

    remote_flag = getattr(args, "remote", None)
    args.remote = remote_flag if remote_flag is not None else config.remote

    fetch_flag = getattr(args, "fetch", None)
    if fetch_flag is not None:
        do_fetch = fetch_flag
    else:
        do_fetch = not config.status_no_fetch
    args.no_fetch = not do_fetch

    pending_flag = getattr(args, "pending", None)
    args.pending = pending_flag if pending_flag is not None else config.status_pending

    compact_flag = getattr(args, "compact", None)
    args.compact = compact_flag if compact_flag is not None else config.status_compact

    changelog_flag = getattr(args, "changelog", None)
    args.changelog = changelog_flag if changelog_flag is not None else config.status_changelog

    ff_only_flag = getattr(args, "ff_only", None)
    args.ff_only = ff_only_flag if ff_only_flag is not None else config.pull_ff_only

    sync_branches_flag = getattr(args, "sync_branches", None)
    if args.command == "push":
        args.sync_branches = sync_branches_flag if sync_branches_flag is not None else config.push_sync_branches
    elif args.command == "pull":
        args.sync_branches = sync_branches_flag if sync_branches_flag is not None else config.pull_sync_branches
    else:
        args.sync_branches = sync_branches_flag if sync_branches_flag is not None else True

    autostash_flag = getattr(args, "autostash", None)
    args.autostash = autostash_flag if autostash_flag is not None else config.pull_autostash

    pull_all_flag = getattr(args, "pull_all", None)
    if pull_all_flag is not None:
        args.all = pull_all_flag
    elif args.command == "pull":
        args.all = config.pull_all

    resolve_all_flag = getattr(args, "resolve_all", None)
    if resolve_all_flag is not None:
        args.all = resolve_all_flag

    strategy_flag = getattr(args, "strategy", None)
    args.strategy = strategy_flag if strategy_flag is not None else config.pull_strategy

    args.interactive = getattr(args, "interactive", False)
    args.dashboard = getattr(args, "dashboard", False) or (args.command == "status" and args.interactive)

    dry_run_flag = getattr(args, "dry_run", None)
    if args.command == "pull":
        args.dry_run = dry_run_flag if dry_run_flag is not None else config.pull_dry_run
    elif args.command == "push":
        args.dry_run = dry_run_flag if dry_run_flag is not None else config.push_dry_run
    elif args.command == "resolve":
        args.dry_run = dry_run_flag if dry_run_flag is not None else False
    elif args.command == "config":
        args.dry_run = dry_run_flag
    else:
        args.dry_run = dry_run_flag if dry_run_flag is not None else False

    confirm_flag = getattr(args, "confirm", None)
    if args.command == "pull":
        args.confirm = confirm_flag if confirm_flag is not None else config.pull_confirm
    elif args.command == "push":
        args.confirm = confirm_flag if confirm_flag is not None else config.push_confirm
    else:
        args.confirm = confirm_flag if confirm_flag is not None else config.confirm

    args.preview = getattr(args, "preview", False)

    push_all_flag = getattr(args, "push_all", None)
    if args.command == "push":
        # Smart disambiguation: passing git-sync push <repo> --all treats --all
        # as --all-branches (--sync-branches) for the specified repo rather than triggering global discovery.
        if push_all_flag and getattr(args, "repos", None):
            args.sync_branches = True
            args.all = False
        elif push_all_flag is not None:
            args.all = push_all_flag
        else:
            args.all = config.push_all

    branch_flag = getattr(args, "branch", None)
    args.branch = branch_flag if branch_flag is not None else config.push_branch

    force_flag = getattr(args, "force", None)
    args.force = force_flag if force_flag is not None else getattr(config, "push_force", False)

    block_prot_flag = getattr(args, "block_protected", None)
    args.block_protected = block_prot_flag if block_prot_flag is not None else getattr(config, "push_block_protected", False)

    allow_prot_flag = getattr(args, "allow_protected", None)
    args.allow_protected = allow_prot_flag if allow_prot_flag is not None else getattr(config, "push_allow_protected", False)

    prot_branches_flag = getattr(args, "protected_branches", None)
    if prot_branches_flag:
        args.effective_protected_branches = [b.strip() for b in prot_branches_flag.split(",") if b.strip()]
    else:
        args.effective_protected_branches = getattr(config, "push_protected_branches", DEFAULT_PROTECTED_BRANCHES)

    max_tokens_flag = getattr(args, "max_tokens", None)
    args.max_tokens = max_tokens_flag if max_tokens_flag is not None else getattr(config, "max_tokens", None)

    submodules_flag = getattr(args, "submodules", None)
    if args.command == "pull":
        args.submodules = submodules_flag if submodules_flag is not None else config.pull_submodules
    elif args.command == "status":
        args.submodules = submodules_flag if submodules_flag is not None else config.status_submodules
    else:
        args.submodules = submodules_flag if submodules_flag is not None else config.submodules

    unshallow_flag = getattr(args, "unshallow", None)
    if args.command == "pull":
        args.unshallow = unshallow_flag if unshallow_flag is not None else config.pull_unshallow
    else:
        args.unshallow = unshallow_flag if unshallow_flag is not None else config.unshallow

    if args.command == "doctor":
        return cmd_doctor(args)
    elif args.command == "status":
        return cmd_status(args)
    elif args.command == "pull":
        return cmd_pull(args)
    elif args.command == "resolve":
        return cmd_resolve(args)
    elif args.command == "push":
        return cmd_push(args)
    elif args.command == "dirty":
        return cmd_dirty(args)
    elif args.command == "config":
        return cmd_config(args, config)
    elif args.command == "install":
        return cmd_install(args)
    elif args.command == "completion":
        return cmd_completion(args)
    elif args.command == "unlock":
        return cmd_unlock(args)
    elif args.command == "worktree":
        return cmd_worktree(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
