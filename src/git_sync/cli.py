"""
Command Line Interface (CLI) entrypoint for git-sync.
Handles argument parsing, command dispatch, workspace profiles, and output formatting.
"""

import argparse
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from git_sync import __version__
from git_sync.config import (
    CONFIG_FILENAME,
    GitSyncConfig,
    load_config,
)
from git_sync.engine import (
    DEFAULT_RETRIES,
    DEFAULT_ROOT,
    DEFAULT_TIMEOUT,
    DEFAULT_WORKERS,
    AdaptiveConcurrencyController,
    audit_ssh_multiplexing,
    benchmark_ssh_multiplexing,
    calculate_optimal_workers,
    check_credentials_sanity,
    diagnose_git_error,
    discover_repos,
    find_diverged_repos,
    find_locked_repos,
    find_push_pending_repos,
    get_divergence_details,
    get_divergence_diff,
    inspect_github_cli,
    inspect_repo,
    inspect_ssh_config,
    match_repo,
    pull_repo,
    push_repo,
    recover_index_lock,
    recover_repo_locks,
    resolve_divergence,
    run_git_with_retry,
)
from git_sync.telemetry import BulkCache, TelemetryBenchmark
from git_sync.ui import (
    AnsiStyler,
    InteractiveDashboard,
    ProgressIndicator,
    format_diff_stat,
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


def _print_ssh_audit_output(ssh_audit: Dict[str, Any]) -> None:
    """Print human-readable SSH multiplexing configuration audit and latency benchmark."""
    print("\n" + "=" * 70)
    print("🔌 AUDITORIA DE MULTIPLEXAÇÃO SSH (ControlMaster)")
    print("=" * 70)
    cfg = ssh_audit.get("config", {})
    cfg_path = cfg.get("config_path", "~/.ssh/config")
    exists = cfg.get("exists", False)
    exists_str = "Presente" if exists else "Não encontrado"
    print(f"  Arquivo de Config:  {cfg_path} ({exists_str})")
    print(f"  Host Alvo:          {cfg.get('host', 'github.com')}")

    cm = cfg.get("control_master")
    if cm and cm.lower() in ("auto", "yes", "autoask"):
        print(f"  ControlMaster:      ✅ {cm}")
    else:
        print(f"  ControlMaster:      ⚠️  {cm or 'Não configurado'}")

    cp = cfg.get("control_path")
    if cp:
        dir_ok = "✅ Diretório existe" if cfg.get("socket_dir_exists") else "❌ Diretório NÃO existe"
        print(f"  ControlPath:        {cp} ({dir_ok})")
    else:
        print("  ControlPath:        ℹ️  Não configurado")

    persist = cfg.get("control_persist")
    if persist:
        print(f"  ControlPersist:     ✅ {persist}")
    else:
        print("  ControlPersist:     ℹ️  Não configurado")

    bench = ssh_audit.get("benchmark")
    if bench:
        print("\n⚡ BENCHMARK DE LATÊNCIA SSH:")
        print("-" * 70)
        if bench.get("supported"):
            t_cold = bench.get("without_multiplexing_seconds")
            t_init = bench.get("with_multiplexing_initial_seconds")
            t_warm = bench.get("with_multiplexing_reused_seconds")
            speedup = bench.get("speedup_factor")
            savings = bench.get("savings_pct")
            if t_cold is not None:
                print(f"  Sem multiplexação (cold):        {t_cold:.4f}s")
            if t_init is not None:
                print(f"  Com multiplexação (1ª conexão):  {t_init:.4f}s")
            if t_warm is not None:
                print(f"  Com multiplexação (reutilizada): {t_warm:.4f}s")
            if speedup and savings:
                print(f"  Aceleração de handshake:        🚀 {speedup:.1f}x mais rápido (-{savings:.1f}%)")
        else:
            reason = bench.get("error") or bench.get("reason") or "Não suportado"
            diag = bench.get("diagnostic")
            print(f"  Status:             ⚠️  Fallback gracioso ({reason})")
            if diag and diag.get("suggestion"):
                print(f"  💡 Sugestão:        {diag['suggestion']}")

    rec = cfg.get("recommendation")
    if rec and not cfg.get("is_enabled"):
        print("\n💡 RECOMENDAÇÃO DE CONFIGURAÇÃO SSH:")
        for line in rec.splitlines():
            print(f"  {line}")


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

    sanity = check_credentials_sanity(
        root_dir=root,
        group=group,
        timeout=timeout,
        test_remotes=True,
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

    print("=" * 70)
    print("🔑 DIAGNÓSTICO DE CREDENCIAIS & AMBIENTE GIT")
    print("=" * 70)

    agent_info = sanity["ssh_agent"]
    agent_status = agent_info["status"]
    if agent_status == "ACTIVE":
        print(f"  SSH Agent:          ✅ Ativo ({agent_info['identities_count']} identidade(s) carregada(s))")
        for ident in agent_info["identities"]:
            print(f"                      • {ident}")
    elif agent_status == "NO_IDENTITIES":
        print("  SSH Agent:          ⚠️  Em execução, mas NENHUMA identidade carregada")
    else:
        print("  SSH Agent:          ❌ Inativo ou inacessível (SSH_AUTH_SOCK não configurado)")

    keys_found = sanity["ssh_keys"]
    if keys_found:
        print(f"  Chaves SSH locais:  ✅ Encontradas em ~/.ssh ({', '.join(keys_found)})")
    else:
        print("  Chaves SSH locais:  ⚠️  Nenhuma chave padrão encontrada em ~/.ssh")

    mux_info = sanity.get("ssh_multiplexing", {})
    if mux_info.get("is_enabled"):
        cm_val = mux_info.get("control_master", "auto")
        print(f"  SSH Multiplexing:   ✅ Ativo (ControlMaster {cm_val})")
    else:
        print("  SSH Multiplexing:   ℹ️  Inativo (use 'git-sync doctor --ssh' para auditar)")

    helpers = sanity["credential_helpers"]
    if helpers:
        print(f"  Credential Helper:  ✅ Configurado ({', '.join(helpers)})")
    else:
        print("  Credential Helper:  ℹ️  Nenhum configurado (git config credential.helper)")

    has_token = sanity["has_token_env"]
    if has_token:
        print("  Token de Ambiente:  ✅ GITHUB_TOKEN ou GH_TOKEN presente")
    else:
        print("  Token de Ambiente:  ℹ️  Nenhum token configurado no ambiente")

    gh_info = sanity.get("github_cli", {})
    if gh_info.get("logged_in"):
        user = gh_info.get("active_account") or "ativo"
        proto = gh_info.get("protocol") or "https"
        scopes = gh_info.get("token_scopes", [])
        scopes_str = f" [{', '.join(scopes)}]" if scopes else ""
        print(f"  GitHub CLI (gh):    ✅ Autenticado como {user} (protocolo: {proto}){scopes_str}")
    elif gh_info.get("installed"):
        print("  GitHub CLI (gh):    ⚠️  Instalado, mas não autenticado (use 'gh auth login')")
    else:
        print("  GitHub CLI (gh):    ℹ️  Não instalado no PATH")

    if ssh_audit:
        _print_ssh_audit_output(ssh_audit)

    remotes_tested = sanity["remotes"]
    if remotes_tested:
        print("\n🌐 VERIFICAÇÃO DE REMOTES TESTADOS:")
        print("-" * 70)
        for r_url, r_data in remotes_tested.items():
            proto = r_data["protocol"].upper()
            host = r_data["host"] or r_url
            repo_slug = r_url.replace("https://github.com/", "").replace("git@github.com:", "")
            display_name = f"{host} ({repo_slug})" if repo_slug != r_url else host
            if len(display_name) > 42:
                display_name = display_name[:39] + "..."
            if r_data["ok"]:
                print(f"  [{proto}] {display_name:<45} ✅ Autenticado")
            else:
                err_label = r_data["error"]
                diag = r_data.get("diagnostic")
                diag_cat = f"[{diag['category']}] " if diag else ""
                print(f"  [{proto}] {display_name:<45} ❌ {diag_cat}{err_label}")
                if diag and diag.get("suggestion"):
                    print(f"        💡 Sugestão: {diag['suggestion']}")

    locked = sanity.get("locked_repos", [])
    if locked:
        print("\n🔒 REPOSITÓRIOS COM LOCK ATIVO/STALE (.git/*.lock):")
        print("-" * 70)
        for lk in locked:
            stale_str = "⚠️  STALE" if lk.get("stale") else "ℹ️  ATIVO"
            print(f"  {lk.get('rel_path', lk['repo']):<35} {lk['lock_name']:<18} {stale_str} ({lk['age_seconds']}s)")

    if sanity["warnings"]:
        print("\n⚠️  AVISOS:")
        for w in sanity["warnings"]:
            print(f"  • {w}")

    if sanity["suggestions"]:
        print("\n💡 RECOMENDAÇÕES PARA RESOLUÇÃO:")
        for s in sanity["suggestions"]:
            print(f"  • {s}")

    print("=" * 70)
    if sanity["ok"]:
        print("✅ Diagnóstico concluído: credenciais e conexões estão operacionais.\n")
        return 0
    else:
        print("❌ Diagnóstico identificou problemas que requerem atenção.\n")
        return 1


def _print_benchmark_output(benchmark: TelemetryBenchmark, is_json: bool = False) -> None:
    """Print human-readable benchmark summary and latency/saturation alerts."""
    print(benchmark.summary_line())
    if benchmark.alerts and not is_json:
        for alert in benchmark.alerts:
            print(f"  ⚠️  Alerta [{alert['category']}]: {alert['message']}")
            if alert.get("recommendation"):
                print(f"     💡 {alert['recommendation']}")


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
    results: List[Dict[str, Any]] = []
    show_progress = False if (args.json or getattr(args, "compact", False) or getattr(args, "summary", False)) else None
    retries = getattr(args, "retries", DEFAULT_RETRIES)
    is_fetch = not args.no_fetch
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
            executor.submit(inspect_repo, r, root, not args.no_fetch, args.timeout, cache, remote, benchmark, retries): r
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
        summary: Dict[str, Any] = {
            "root": root,
            "total": len(results),
            "pull_pending": [r["rel_path"] for r in results if "PULL" in r["status"]],
            "push_pending": [r["rel_path"] for r in results if r["status"] == "PUSH_PENDING"],
            "dirty": [r["rel_path"] for r in results if r["is_dirty"]],
            "diverged": [r["rel_path"] for r in results if r["status"] == "DIVERGED"],
            "worktrees": [r["rel_path"] for r in results if r.get("is_worktree")],
            "has_submodules": [r["rel_path"] for r in results if r.get("submodules")],
            "no_remote": [r["rel_path"] for r in results if r["status"] == "NO_REMOTE"],
            "no_upstream": [r["rel_path"] for r in results if r["status"] == "NO_UPSTREAM"],
            "offline": [r["rel_path"] for r in results if r.get("network_state") == "offline"],
            "errors": [r["rel_path"] for r in results if r["status"] == "FETCH_ERROR"],
            "remediations": [
                {
                    "repo": r["rel_path"],
                    "status": r["status"],
                    "network_state": r.get("network_state"),
                    "action_required": r["action_required"],
                    "remediation": r.get("remediation"),
                }
                for r in results
                if r.get("remediation")
            ],
        }
        if not getattr(args, "compact", False):
            summary["items"] = results
        if getattr(args, "benchmark", False):
            summary["telemetry"] = benchmark.to_dict()
        print(json.dumps(summary, indent=2))
        return 0

    if getattr(args, "compact", False):
        pending_pull = sum(1 for r in results if "PULL" in r["status"])
        pending_push = sum(1 for r in results if "PUSH" in r["status"])
        dirty_count = sum(1 for r in results if r["is_dirty"])
        diverged_count = sum(1 for r in results if r["status"] == "DIVERGED")
        print(f"Total: {len(results)} repositórios | Pendentes de Pull: {pending_pull} | Pendentes de Push: {pending_push} | Divergidos: {diverged_count} | Modificados (Dirty): {dirty_count}\n")
        if getattr(args, "benchmark", False):
            _print_benchmark_output(benchmark, getattr(args, "json", False))
        return 0

    if getattr(args, "dashboard", False) or getattr(args, "tui", False):
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
        )
        return dashboard.run()

    status_icons = {
        "UP_TO_DATE": "✅ OK",
        "PULL_PENDING": "⬇️ PULL",
        "PUSH_PENDING": "⬆️ PUSH",
        "DIVERGED": "⚠️ DIVERGIU",
        "AUX_PULL_PENDING": "⬇️ AUX_PULL",
        "AUX_PUSH_PENDING": "⬆️ AUX_PUSH",
        "NO_UPSTREAM": "⚠️ SEM UPSTREAM",
        "FETCH_ERROR": "❌ ERRO FETCH",
        "NO_REMOTE": "❌ SEM REMOTE"
    }

    print(f"\n{'STATUS':<15} {'REPOSITÓRIO':<40} {'BRANCH':<12} {'DETALHES'}")
    print("=" * 95)

    for r in results:
        sym = status_icons.get(r["status"], r["status"])
        if r["status"] == "FETCH_ERROR" and r.get("network_state") == "offline":
            sym = "🌐 OFFLINE"
        details = []
        if r.get("is_worktree"):
            details.append("worktree")
        if r.get("worktrees"):
            linked_wts = [w for w in r["worktrees"] if not w.get("is_main")]
            if linked_wts:
                details.append(f"{len(linked_wts)} worktree(s)")
        if r.get("submodules"):
            dirty_subs = [s for s in r["submodules"] if s.get("status") != "UP_TO_DATE"]
            if dirty_subs:
                details.append(f"submodules({len(dirty_subs)}/{len(r['submodules'])} pendentes)")
            else:
                details.append(f"submodules({len(r['submodules'])})")
        if r.get("multi_remote") or len(r.get("remotes", {})) > 1:
            remote_names = ", ".join(sorted(r.get("remotes", {}).keys()))
            details.append(f"remotes[{remote_names}]")
        if r["behind"] > 0:
            details.append(f"{r['behind']} atrás")
        if r["ahead"] > 0:
            details.append(f"{r['ahead']} à frente")
        if r["aux_branches_behind"]:
            aux_desc = ", ".join(f"{k}:{v}" for k, v in r["aux_branches_behind"].items())
            details.append(f"aux_behind[{aux_desc}]")
        if r.get("aux_branches_ahead"):
            aux_ahead_desc = ", ".join(f"{k}:{v}" for k, v in r["aux_branches_ahead"].items())
            details.append(f"aux_ahead[{aux_ahead_desc}]")
        if r["is_dirty"]:
            details.append(f"dirty({len(r['dirty_files'])} arquivos)")
        if r["fetch_error"]:
            if r.get("diagnostic"):
                diag = r["diagnostic"]
                remed_str = f" (💡 {r['remediation']})" if r.get("remediation") else f" (💡 {diag['suggestion']})"
                details.append(f"erro[{diag['category']}]: {r['fetch_error']}{remed_str}")
            else:
                details.append(f"erro: {r['fetch_error']}")
        elif r["status"] == "FETCH_ERROR":
            details.append("erro no fetch")
        if r["status"] == "NO_REMOTE":
            remed_str = f" (💡 {r['remediation']})" if r.get("remediation") else ""
            details.append(f"sem remote configurado{remed_str}")
        elif r["status"] == "NO_UPSTREAM":
            remed_str = f" (💡 {r['remediation']})" if r.get("remediation") else ""
            details.append(f"sem upstream configurado{remed_str}")
        elif not details:
            details.append("sincronizado")

        detail_str = "; ".join(details)
        print(f"{sym:<15} {r['rel_path']:<40} {r['branch']:<12} {detail_str}")

    print("=" * 95)
    offline_count = sum(1 for r in results if r.get("network_state") == "offline")
    if offline_count:
        print(f"🌐 AVISO DE CONECTIVIDADE: {offline_count} repositório(s) inacessíveis por queda de rede/DNS.")
        print("   💡 Execute com '--no-fetch' para inspeção instantânea do estado local.\n")
    pending_pull = sum(1 for r in results if "PULL" in r["status"])
    pending_push = sum(1 for r in results if "PUSH" in r["status"])
    dirty_count = sum(1 for r in results if r["is_dirty"])
    print(f"Total: {len(results)} repositórios | Pendentes de Pull: {pending_pull} | Pendentes de Push: {pending_push} | Modificados (Dirty): {dirty_count}\n")
    if getattr(args, "benchmark", False):
        _print_benchmark_output(benchmark, getattr(args, "json", False))
    return 0


def _prompt_resolution_choice(
    repo_path: str,
    rel_path: str,
    details: Dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT,
    input_func: Any = input,
) -> Dict[str, Any]:
    """
    Prompt user interactively to choose a conflict or divergence resolution strategy.
    # INVARIANT: Abort is the safe default when user cancels, enters empty choice, or inputs EOF/SIGINT.
    """
    print("\n" + "=" * 60)
    print(f"📂 Repositório divergente: {rel_path}")
    print(f"Branch: {details.get('branch')} ↔ Upstream: {details.get('upstream') or '(sem upstream)'}")
    print(f"Divergência: {details.get('ahead', 0)} commit(s) à frente (local), {details.get('behind', 0)} atrás (remoto)")

    local_commits = details.get("local_commits", [])
    if local_commits:
        print("Commits locais pendentes de envio:")
        for c in local_commits[:5]:
            print(f"  * {c.get('hash')} {c.get('subject')}")
        if len(local_commits) > 5:
            print(f"  ... (+{len(local_commits) - 5} commits)")

    remote_commits = details.get("remote_commits", [])
    if remote_commits:
        print("Commits remotos pendentes de pull:")
        for c in remote_commits[:5]:
            print(f"  * {c.get('hash')} {c.get('subject')}")
        if len(remote_commits) > 5:
            print(f"  ... (+{len(remote_commits) - 5} commits)")

    if details.get("is_dirty"):
        dirty_files = details.get("dirty_files", [])
        print(f"⚠️  Árvore de trabalho modificada: {len(dirty_files)} arquivo(s) não commitados")

    while True:
        print("\nEstratégias de resolução:")
        print("  [1] Ver diff (diff)")
        print("  [2] Rebase no upstream (rebase)")
        print("  [3] Merge upstream (merge)")
        print("  [4] Stash alterações locais (stash)")
        print("  [5] Abortar / Pular (abort) [padrão]")
        try:
            choice = input_func("Escolha [1-5 / abort]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nOperação abortada pelo usuário.")
            return resolve_divergence(repo_path, strategy="abort", branch=details.get("branch"), timeout=timeout)

        if choice in ("1", "diff"):
            diff_text = get_divergence_diff(repo_path, branch=details.get("branch"), timeout=timeout)
            if diff_text:
                print("\n--- DIFF INÍCIO ---")
                print(format_diff_stat(diff_text[:3000]))
                if len(diff_text) > 3000:
                    print(f"\n... (diff truncado em 3000 caracteres, total: {len(diff_text)})")
                print("--- DIFF FIM ---\n")
            else:
                print("\n(Nenhuma diferença textual encontrada ou sem upstream)")
            continue
        elif choice in ("2", "rebase"):
            res = resolve_divergence(repo_path, strategy="rebase", branch=details.get("branch"), timeout=timeout)
            if res["success"]:
                print(f"✅ Rebase concluído com sucesso em {rel_path}.")
            else:
                print(f"❌ Rebase falhou: {res.get('stderr') or res.get('message')}")
            return res
        elif choice in ("3", "merge"):
            res = resolve_divergence(repo_path, strategy="merge", branch=details.get("branch"), timeout=timeout)
            if res["success"]:
                print(f"✅ Merge concluído com sucesso em {rel_path}.")
            else:
                print(f"❌ Merge falhou: {res.get('stderr') or res.get('message')}")
            return res
        elif choice in ("4", "stash"):
            res = resolve_divergence(repo_path, strategy="stash", branch=details.get("branch"), timeout=timeout)
            if res["success"]:
                print(f"✅ Alterações locais stashed com sucesso em {rel_path}.")
            else:
                print(f"❌ Falha no stash: {res.get('stderr') or res.get('message')}")
            continue
        elif choice in ("5", "abort", "skip", ""):
            print(f"Operação abortada para {rel_path}.")
            return resolve_divergence(repo_path, strategy="abort", branch=details.get("branch"), timeout=timeout)
        else:
            print("Opção inválida. Digite 1, 2, 3, 4, 5 ou abort.")


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
        inspections = []
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
        targets = [r["path"] for r in inspections if "PULL" in r["status"] or r["aux_branches_behind"]]
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
                mode_str = "[DRY-RUN] " if dry_run else ""
                icon = "ℹ️" if dry_run else ("✅" if res["success"] else "❌")
                detail_msg = "OK" if res["success"] else (res["stderr"] or res["stdout"])
                if dry_run and res["success"] and res.get("stdout"):
                    detail_msg = res["stdout"]
                if res.get("resolved"):
                    strat_applied = res["resolved"].get("strategy")
                    detail_msg = f"{detail_msg} (resolvido via {strat_applied})"
                elif res.get("autostash", {}).get("would_stash"):
                    detail_msg = f"{detail_msg} (autostash de {len(res['autostash']['dirty_files'])} arquivos seria realizado)"
                elif res.get("autostash", {}).get("stashed"):
                    if res["autostash"].get("conflict"):
                        detail_msg = f"{detail_msg} (conflito ao restaurar stash mantido em {res['autostash'].get('stash_ref')})"
                    elif res["autostash"].get("restored"):
                        detail_msg = f"{detail_msg} (autostash restaurado com sucesso)"
                if not res["success"] and res.get("diagnostic"):
                    detail_msg += f" (💡 {res['diagnostic']['suggestion']})"
                remote_tag = f" [{res['remote']}]" if res.get("remote") else ""
                wt_tag = " [worktree]" if res.get("is_worktree") else ""
                print(f"{icon} {mode_str}[{rel}]{wt_tag}{remote_tag} ({res['branch']}): {detail_msg}")
                for aux_b, aux_ok in res.get("aux_branches", {}).items():
                    aux_icon = "  ↳ ℹ️ [DRY-RUN]" if dry_run else ("  ↳ ✅" if aux_ok else "  ↳ ❌")
                    aux_status = "fast-forward previsto" if dry_run else "fast-forward concluído"
                    print(f"{aux_icon} Branch auxiliar '{aux_b}': {aux_status}")
                for iso_b, target_wt in res.get("worktree_isolated", {}).items():
                    try:
                        iso_rel = os.path.relpath(target_wt, root)
                    except ValueError:
                        iso_rel = target_wt
                    iso_icon = "  ↳ ℹ️ [DRY-RUN]" if dry_run else "  ↳ ⚠️"
                    iso_status = "isolada de fast-forward" if dry_run else "isolada (ativa em worktree alvo)"
                    print(f"{iso_icon} Branch auxiliar '{iso_b}': {iso_status} [{iso_rel}]")

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

    results: List[Dict[str, Any]] = []
    strategy = getattr(args, "strategy", None)
    show_diff_only = getattr(args, "diff", False)
    is_interactive = (not dry_run) and (getattr(args, "interactive", False) or (
        not args.json and not strategy and not show_diff_only and sys.stdin.isatty()
    ))

    msg_prog = "Simulando resolução..." if dry_run else "Resolvendo divergências..."
    with ProgressIndicator(msg_prog, total=len(targets), enabled=show_progress) as progress:
        for r in targets:
            rel = os.path.relpath(r, root)
            details = get_divergence_details(r, timeout=args.timeout)

            if show_diff_only:
                diff_text = get_divergence_diff(r, branch=details.get("branch"), timeout=args.timeout)
                if args.json:
                    results.append({"repo": r, "repo_rel": rel, "details": details, "diff": diff_text, "dry_run": dry_run})
                else:
                    print(f"\n📂 Diff de divergência para {rel} ({details.get('branch')} ↔ {details.get('upstream')}):")
                    print("=" * 60)
                    print(format_diff_stat(diff_text) if diff_text else "(Nenhuma diferença ou sem upstream)")
                progress.advance(rel)
                continue

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
                res = _prompt_resolution_choice(r, rel, details, timeout=args.timeout)
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

    push_results = []
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
                timeout=args.timeout,
                cache=cache,
                benchmark=benchmark,
                retries=retries,
            )
            res["repo_rel"] = rel
            push_results.append(res)
            prog_label = f"{rel} [worktree]" if res.get("is_worktree") else rel
            progress.advance(prog_label)

            if not args.json:
                mode_str = "[DRY-RUN] " if res["dry_run"] else ""
                icon = "✅" if res["success"] else "❌"
                status_detail = "OK" if res["success"] else (res["stderr"] or res["stdout"])
                if not res["success"] and res.get("diagnostic"):
                    status_detail += f" (💡 {res['diagnostic']['suggestion']})"
                ahead_info = f" ({res['ahead']} commit(s) à frente)" if res.get("ahead", 0) > 0 else ""
                remote_tag = f" -> {res['remote']}" if res.get("remote") else ""
                wt_tag = " [worktree]" if res.get("is_worktree") else ""
                print(f"{icon} {mode_str}[{rel}]{wt_tag} ({res['branch']}{ahead_info}{remote_tag}): {status_detail}")

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

    if not dirty:
        print("✅ Nenhum repositório com arquivos modificados ou não rastreados.")
        return 0

    print(f"\nEncontrados {len(dirty)} repositório(s) com alterações locais:")
    for r in dirty:
        print(f"\n📂 {r['rel_path']}:")
        for f in r["dirty_files"]:
            print(f"   {f}")
    print()
    return 0


def cmd_config(args: argparse.Namespace, config: GitSyncConfig) -> int:
    """Display active workspace profile and resolved runtime configuration."""
    if getattr(args, "json", False):
        print(json.dumps(config.to_dict(), indent=2))
        return 0

    print("\n⚙️  Configuração ativa do git-sync:")
    print("=" * 60)
    print(f"  Arquivo de config:   {config.config_path or '(nenhum / padrões em uso)'}")
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

    print("=" * 70)
    print("🔓 RECUPERAÇÃO DE REPOSITÓRIOS COM LOCK (.git/*.lock)")
    print("=" * 70)
    for res in results:
        status_icon = "✅" if res["status"] == "UNLOCKED" else ("⚠️" if res["status"] == "SKIPPED_ACTIVE" else "❌")
        print(f"  {status_icon} {res['rel_path']:<35} {res['lock_name']:<18} {res['message']}")
    print("=" * 70)
    unlocked_count = sum(1 for r in results if r["status"] == "UNLOCKED")
    print(f"Concluído: {unlocked_count} lock(s) removido(s).\n")
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
            from git_sync.config import GitSyncConfig
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
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"❌ Configuration error: {exc}", file=sys.stderr)
        return 1

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
    common.add_argument("--dashboard", "--tui", dest="dashboard", action="store_true", default=argparse.SUPPRESS, help="Launch interactive terminal dashboard")

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

    # dirty
    p_dirty = subparsers.add_parser("dirty", parents=[common], help="Show repositories with uncommitted or untracked changes")
    p_dirty.add_argument("--group", default=None, help="Filter repositories by group")

    # config
    p_config = subparsers.add_parser("config", parents=[common], help="Display active workspace profile and configuration")

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

    args = parser.parse_args(argv)

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

    ff_only_flag = getattr(args, "ff_only", None)
    args.ff_only = ff_only_flag if ff_only_flag is not None else config.pull_ff_only

    sync_branches_flag = getattr(args, "sync_branches", None)
    args.sync_branches = sync_branches_flag if sync_branches_flag is not None else config.pull_sync_branches

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

    push_all_flag = getattr(args, "push_all", None)
    if push_all_flag is not None:
        args.all = push_all_flag
    elif args.command == "push":
        args.all = config.push_all

    branch_flag = getattr(args, "branch", None)
    args.branch = branch_flag if branch_flag is not None else config.push_branch

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
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
