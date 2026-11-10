"""
Terminal formatting and output presentation helpers for git-sync CLI subcommands.

# INVARIANT: Zero external dependencies; standard library only.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from git_sync.engine import (
    DEFAULT_COMPACT_DIFF_TOKENS,
    DEFAULT_TIMEOUT,
    DoctorSanityReport,
    PullResult,
    PushResult,
    RepoStatusRecord,
    ResolveResult,
    format_changelog_summary,
    get_divergence_diff,
    resolve_divergence,
)
from git_sync.telemetry import TelemetryBenchmark
from git_sync.ui.style import format_diff_stat


def print_benchmark_report(benchmark: TelemetryBenchmark, is_json: bool = False) -> None:
    """Print human-readable benchmark summary and latency/saturation alerts."""
    print(benchmark.summary_line())
    if benchmark.alerts and not is_json:
        for alert in benchmark.alerts:
            print(f"  ⚠️  Alerta [{alert['category']}]: {alert['message']}")
            if alert.get("recommendation"):
                print(f"     💡 {alert['recommendation']}")


def print_ssh_audit_report(ssh_audit: Dict[str, Any]) -> None:
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

    sock_info = ssh_audit.get("sockets")
    if sock_info:
        act = sock_info.get("active", [])
        pruned = sock_info.get("pruned", [])
        corrupted = sock_info.get("corrupted", [])
        if act:
            print(f"  Sockets Ativos:     🔌 {len(act)} conexão(ões) mestre ativa(s)")
            for s in act:
                print(f"                      • {s.get('path')}")
        if pruned:
            print(f"  Sockets Podados:    🧹 {len(pruned)} socket(s) obsoleto(s) removido(s)")
            for p in pruned:
                print(f"                      • {p}")
        if corrupted and not pruned:
            print(f"  Sockets Inválidos:  ⚠️  {len(corrupted)} arquivo(s) corrompido(s)")

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


def print_doctor_report(
    sanity: DoctorSanityReport | Dict[str, Any],
    ssh_audit: Optional[Dict[str, Any]] = None,
) -> int:
    """Render human-readable doctor sanity and credential health report."""
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
        pruned = mux_info.get("stale_sockets_pruned", [])
        if pruned:
            print(f"                      🧹 {len(pruned)} socket(s) stale podado(s)")
        active = mux_info.get("active_sockets", [])
        if active:
            print(f"                      🔌 {len(active)} conexão(ões) mestre ativa(s)")
    else:
        print("  SSH Multiplexing:   ℹ️  Inativo (use 'git-sync doctor --ssh' para auditar)")
        pruned = mux_info.get("stale_sockets_pruned", [])
        if pruned:
            print(f"  Sockets SSH:        🧹 {len(pruned)} socket(s) stale podado(s)")

    helpers = sanity["credential_helpers"]
    cred_diag = sanity.get("credential_helper_diagnostics") or {}
    helper_entries = cred_diag.get("helpers", [])
    if helpers:
        all_helpers_ok = all(h.get("ok", True) and h.get("status") == "HEALTHY" for h in helper_entries)
        any_helper_err = any(not h.get("ok", True) for h in helper_entries)
        hdr_icon = "✅" if all_helpers_ok else ("❌" if any_helper_err else "⚠️ ")
        print(f"  Credential Helper:  {hdr_icon} Configurado ({', '.join(helpers)})")
        for h_entry in helper_entries:
            h_type = h_entry.get("type", "custom")
            h_status = h_entry.get("status", "HEALTHY")
            h_icon = "✅" if h_status == "HEALTHY" else ("❌" if not h_entry.get("ok", True) else "⚠️ ")
            detail_parts = [f"status={h_status}"]
            if h_type == "cache":
                detail_parts.append(f"timeout={h_entry.get('timeout_seconds', 900)}s")
                detail_parts.append(f"socket={h_entry.get('socket_status', 'IDLE')}")
            elif h_type == "store":
                detail_parts.append(f"entries={h_entry.get('entries_count', 0)}")
                if h_entry.get("file_mode"):
                    detail_parts.append(f"mode={h_entry['file_mode']}")
            elif h_entry.get("binary_path"):
                detail_parts.append(f"bin={h_entry['binary_path']}")
            print(f"                      • {h_icon} [{h_type}] {', '.join(detail_parts)}")
    else:
        print("  Credential Helper:  ℹ️  Nenhum configurado (git config credential.helper)")

    has_token = sanity["has_token_env"]
    token_env_info = cred_diag.get("token_env") or {}
    if has_token:
        if token_env_info.get("valid_format", True):
            t_fmt = token_env_info.get("token_format")
            fmt_str = f" [{t_fmt}]" if t_fmt else ""
            print(f"  Token de Ambiente:  ✅ GITHUB_TOKEN ou GH_TOKEN presente{fmt_str}")
        else:
            print("  Token de Ambiente:  ⚠️  Token presente, mas com formato inválido ou placeholder")
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
        if gh_info.get("token_expired"):
            print("  GitHub CLI (gh):    ❌ Token expirado ou inválido (use 'gh auth refresh' ou 'gh auth login')")
        else:
            print("  GitHub CLI (gh):    ⚠️  Instalado, mas não autenticado (use 'gh auth login')")
    else:
        print("  GitHub CLI (gh):    ℹ️  Não instalado no PATH")

    lfs_cli = sanity.get("git_lfs", {})
    lfs_repos = sanity.get("lfs_repos", [])
    if lfs_cli.get("installed"):
        ver = lfs_cli.get("version") or "instalado"
        repo_note = f" ({len(lfs_repos)} repositório(s) com LFS)" if lfs_repos else ""
        print(f"  Git LFS:            ✅ {ver}{repo_note}")
    elif lfs_repos:
        print(f"  Git LFS:            ⚠️  Não instalado ({len(lfs_repos)} repositório(s) com LFS; execute 'git lfs install')")
    else:
        print("  Git LFS:            ℹ️  Não instalado no PATH")

    if ssh_audit:
        print_ssh_audit_report(ssh_audit)

    remotes_tested = sanity["remotes"]
    if remotes_tested:
        print("\n🌐 VERIFICAÇÃO DE REMOTES TESTADOS:")
        print("-" * 70)
        for r_url, r_data in remotes_tested.items():
            proto = r_data.get("protocol", "https").upper()
            host = r_data.get("host") or r_url
            repo_slug = r_url.replace("https://github.com/", "").replace("git@github.com:", "")
            display_name = f"{host} ({repo_slug})" if repo_slug != r_url else host
            if len(display_name) > 34:
                display_name = display_name[:31] + "..."

            latency = r_data.get("latency_seconds")
            grade = r_data.get("grade")
            if grade and latency is not None:
                grade_info = f"Grade {grade} ({latency:.3f}s)"
            elif grade:
                grade_info = f"Grade {grade}"
            else:
                grade_info = ""

            if r_data["ok"]:
                if grade_info:
                    print(f"  [{proto}] {display_name:<34} {grade_info:<16} ✅ Autenticado")
                else:
                    print(f"  [{proto}] {display_name:<45} ✅ Autenticado")
            else:
                err_label = r_data.get("error", "Falha na conexão")
                diag = r_data.get("diagnostic")
                diag_cat = f"[{diag['category']}] " if diag else ""
                if grade_info:
                    print(f"  [{proto}] {display_name:<34} {grade_info:<16} ❌ {diag_cat}{err_label}")
                else:
                    print(f"  [{proto}] {display_name:<45} ❌ {diag_cat}{err_label}")
                if diag and diag.get("suggestion"):
                    print(f"        💡 Sugestão: {diag['suggestion']}")

    multi_matrix = sanity.get("multi_remote_matrix") or {}
    matrix_entries = multi_matrix.get("matrix", [])
    if matrix_entries:
        multi_count = multi_matrix.get("multi_remote_repos", 0)
        total_endpoints = multi_matrix.get("total_endpoints_tested", 0)
        healthy_endpoints = multi_matrix.get("healthy_endpoints", 0)
        grades = multi_matrix.get("grade_distribution", {})
        grade_summary = f"A:{grades.get('A', 0)} B:{grades.get('B', 0)} C:{grades.get('C', 0)} F:{grades.get('F', 0)}"

        print("\n🌐 MATRIZ DE CONECTIVIDADE MULTI-REMOTE:")
        print("-" * 70)
        print(f"  Repos: {len(matrix_entries)} ({multi_count} multi-remote) | Endpoints: {healthy_endpoints}/{total_endpoints} ok | Latência: {grade_summary}")
        focus_entries = [m for m in matrix_entries if m.get("multi_remote") or m.get("has_unreachable")]
        if not focus_entries:
            focus_entries = matrix_entries[:10]
        for m in focus_entries:
            r_name = m["repo"]
            if len(r_name) > 28:
                r_name = r_name[:25] + "..."
            endpoints_parts = []
            for r_k, r_v in m.get("remotes", {}).items():
                lat = r_v.get("latency_seconds")
                lat_str = f" ({lat:.2f}s)" if lat is not None else ""
                ok_mark = "✅" if r_v.get("ok") else "❌"
                endpoints_parts.append(f"{r_k}:{ok_mark}{r_v.get('grade', 'F')}{lat_str}")
            endpoints_str = " | ".join(endpoints_parts) if endpoints_parts else "nenhum remote"
            status_tag = "❌ FALHA" if m.get("has_unreachable") else "✅ OK"
            print(f"  {r_name:<28} {status_tag:<9} {endpoints_str}")

    wt_health = sanity.get("worktree_health") or {}
    wt_items = wt_health.get("items", [])
    if wt_items:
        repos_with_wt = [w for w in wt_items if w.get("total_worktrees", 0) > 1 or w.get("is_secondary_worktree") or w.get("stale_worktrees_count", 0) > 0 or not w.get("is_healthy")]
        if repos_with_wt:
            print("\n🌿 AUDITORIA DE WORKTREES VINCULADOS:")
            print("-" * 70)
            print(f"  Repos com worktrees: {wt_health.get('repos_with_linked_worktrees', len(repos_with_wt))} | Sujos: {wt_health.get('total_dirty_worktrees', 0)} | Stale: {wt_health.get('total_stale_worktrees', 0)} | Detached: {wt_health.get('total_detached_worktrees', 0)}")
            for w in repos_with_wt:
                r_name = w["repo"]
                if len(r_name) > 28:
                    r_name = r_name[:25] + "..."
                wt_status = "✅ SAUDÁVEL" if w.get("is_healthy") else "⚠️  ATENÇÃO"
                details = []
                if w.get("dirty_worktrees_count", 0) > 0:
                    details.append(f"{w['dirty_worktrees_count']} sujo(s)")
                if w.get("stale_worktrees_count", 0) > 0:
                    details.append(f"{w['stale_worktrees_count']} stale")
                if w.get("detached_worktrees_count", 0) > 0:
                    details.append(f"{w['detached_worktrees_count']} detached")
                if not details:
                    details.append(f"{w.get('total_worktrees', 1)} worktree(s)")
                detail_str = ", ".join(details)
                print(f"  {r_name:<28} {wt_status:<14} {detail_str}")

    locked = sanity.get("locked_repos", [])
    if locked:
        print("\n🔒 REPOSITÓRIOS COM LOCK ATIVO/STALE (.git/*.lock):")
        print("-" * 70)
        for lk in locked:
            stale_str = "⚠️  STALE" if lk.get("stale") else "ℹ️  ATIVO"
            print(f"  {lk.get('rel_path', lk['repo']):<35} {lk['lock_name']:<18} {stale_str} ({lk['age_seconds']}s)")

    if lfs_repos:
        print(f"\n📦 REPOSITÓRIOS COM GIT LFS ({len(lfs_repos)}):")
        print("-" * 70)
        for repo_slug in lfs_repos:
            print(f"  • {repo_slug}")

    if sanity.get("warnings"):
        print("\n⚠️  AVISOS:")
        for w in sanity["warnings"]:
            print(f"  • {w}")

    if sanity.get("suggestions"):
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


def _extract_submodules_list(r: RepoStatusRecord | Dict[str, Any]) -> List[Dict[str, Any]]:
    subs = r.get("submodules")
    if isinstance(subs, list):
        return [s for s in subs if isinstance(s, dict)]
    return []


def build_status_summary(
    results: Sequence[RepoStatusRecord | Dict[str, Any]],
    root: str,
    changelog_enabled: bool = False,
    compact: bool = False,
    benchmark: Optional[TelemetryBenchmark] = None,
) -> Dict[str, Any]:
    """Construct structured JSON summary dictionary from multi-repository status results."""
    summary: Dict[str, Any] = {
        "root": root,
        "total": len(results),
        "pull_pending": [r["rel_path"] for r in results if "PULL" in r.get("status", "")],
        "push_pending": [r["rel_path"] for r in results if r.get("status") == "PUSH_PENDING"],
        "dirty": [r["rel_path"] for r in results if r.get("is_dirty")],
        "diverged": [r["rel_path"] for r in results if r.get("status") == "DIVERGED"],
        "worktrees": [r["rel_path"] for r in results if r.get("is_worktree")],
        "stale_worktrees": [r["rel_path"] for r in results if r.get("stale_worktrees")],
        "has_submodules": [r["rel_path"] for r in results if r.get("submodules")],
        "submodule_drift": [r["rel_path"] for r in results if any(s.get("status") != "UP_TO_DATE" or s.get("out_of_sync") for s in _extract_submodules_list(r))],
        "shallow_submodules": [r["rel_path"] for r in results if any(s.get("is_shallow") for s in _extract_submodules_list(r))],
        "has_lfs": [r["rel_path"] for r in results if r.get("has_lfs") or bool((r.get("lfs") or {}).get("has_lfs"))],
        "lfs_pointers": [r["rel_path"] for r in results if int((r.get("lfs") or {}).get("pointers_count", 0)) > 0],
        "lfs_warnings": [r["rel_path"] for r in results if bool((r.get("lfs") or {}).get("warning"))],
        "sparse_checkout": [r["rel_path"] for r in results if bool((r.get("sparse_checkout") or {}).get("enabled"))],
        "sparse_cone_warnings": [r["rel_path"] for r in results if bool((r.get("sparse_checkout") or {}).get("warning"))],
        "no_remote": [r["rel_path"] for r in results if r.get("status") == "NO_REMOTE"],
        "no_upstream": [r["rel_path"] for r in results if r.get("status") == "NO_UPSTREAM"],
        "offline": [r["rel_path"] for r in results if r.get("network_state") == "offline"],
        "errors": [r["rel_path"] for r in results if r.get("status") == "FETCH_ERROR"],
        "multi_remote": [r["rel_path"] for r in results if r.get("multi_remote") or len(r.get("remotes") or {}) > 1],
        "worktree_health": {
            "repos_with_worktrees": [r["rel_path"] for r in results if len(r.get("worktrees") or []) > 1 or r.get("is_worktree")],
            "stale_worktrees": [r["rel_path"] for r in results if r.get("stale_worktrees")],
            "dirty_worktrees": [r["rel_path"] for r in results if r.get("is_worktree") and r.get("is_dirty")],
        },
        "remediations": [
            {
                "repo": r["rel_path"],
                "status": r["status"],
                "network_state": r.get("network_state"),
                "action_required": r.get("action_required"),
                "remediation": r.get("remediation"),
            }
            for r in results
            if r.get("remediation")
        ],
    }
    existing_remed_pairs = {(rem["repo"], rem.get("action_required")) for rem in summary["remediations"]}
    for dr in results:
        sub_items = _extract_submodules_list(dr)
        if any(s.get("status") != "UP_TO_DATE" or s.get("out_of_sync") for s in sub_items):
            if (dr["rel_path"], "update_submodules") not in existing_remed_pairs:
                summary["remediations"].append({
                    "repo": dr["rel_path"],
                    "status": "SUBMODULE_DRIFT",
                    "network_state": dr.get("network_state", "healthy"),
                    "action_required": "update_submodules",
                    "remediation": f"git -C '{dr['path']}' submodule update --init --recursive",
                })
                existing_remed_pairs.add((dr["rel_path"], "update_submodules"))
        shallow_subs = [s for s in sub_items if s.get("is_shallow")]
        if shallow_subs:
            if (dr["rel_path"], "unshallow_submodules") not in existing_remed_pairs:
                summary["remediations"].append({
                    "repo": dr["rel_path"],
                    "status": "SHALLOW_SUBMODULE",
                    "network_state": dr.get("network_state", "healthy"),
                    "action_required": "unshallow_submodules",
                    "remediation": f"git-sync pull '{dr['rel_path']}' --submodules --unshallow",
                })
                existing_remed_pairs.add((dr["rel_path"], "unshallow_submodules"))
        if dr.get("stale_worktrees"):
            if (dr["rel_path"], "prune_worktrees") not in existing_remed_pairs:
                summary["remediations"].append({
                    "repo": dr["rel_path"],
                    "status": "STALE_WORKTREE",
                    "network_state": dr.get("network_state", "healthy"),
                    "action_required": "prune_worktrees",
                    "remediation": f"git-sync worktree prune '{dr['rel_path']}'",
                })
                existing_remed_pairs.add((dr["rel_path"], "prune_worktrees"))
        if dr.get("sparse_checkout", {}).get("warning"):
            if (dr["rel_path"], "reapply_sparse_checkout") not in existing_remed_pairs:
                summary["remediations"].append({
                    "repo": dr["rel_path"],
                    "status": "SPARSE_CHECKOUT_WARNING",
                    "network_state": dr.get("network_state", "healthy"),
                    "action_required": "reapply_sparse_checkout",
                    "remediation": f"git -C '{dr['path']}' sparse-checkout reapply --cone",
                })
                existing_remed_pairs.add((dr["rel_path"], "reapply_sparse_checkout"))
        if int((dr.get("lfs") or {}).get("missing_objects", 0)) > 0:
            if (dr["rel_path"], "pull_lfs") not in existing_remed_pairs:
                summary["remediations"].append({
                    "repo": dr["rel_path"],
                    "status": "LFS_OBJECTS_MISSING",
                    "network_state": dr.get("network_state", "healthy"),
                    "action_required": "pull_lfs",
                    "remediation": f"git -C '{dr['path']}' lfs pull",
                })
                existing_remed_pairs.add((dr["rel_path"], "pull_lfs"))
    if changelog_enabled:
        summary["changelogs"] = {
            r["rel_path"]: {
                "incoming_categories": (r.get("changelog") or {}).get("incoming_categories", {}),
                "outgoing_categories": (r.get("changelog") or {}).get("outgoing_categories", {}),
                "incoming_breaking": (r.get("changelog") or {}).get("incoming_breaking", 0),
                "outgoing_breaking": (r.get("changelog") or {}).get("outgoing_breaking", 0),
                "has_breaking": (r.get("changelog") or {}).get("has_breaking", False),
            }
            for r in results
            if r.get("changelog") and ((r.get("changelog") or {}).get("incoming") or (r.get("changelog") or {}).get("outgoing"))
        }
    if not compact:
        summary["items"] = results
    if benchmark is not None:
        summary["telemetry"] = benchmark.to_dict()
    return summary


def print_status_compact(
    results: Sequence[RepoStatusRecord | Dict[str, Any]],
    changelog_enabled: bool = False,
    benchmark: Optional[TelemetryBenchmark] = None,
) -> None:
    """Print one-line summary metrics and compact changelog breakdown."""
    pending_pull = sum(1 for r in results if "PULL" in r.get("status", ""))
    pending_push = sum(1 for r in results if "PUSH" in r.get("status", ""))
    dirty_count = sum(1 for r in results if r.get("is_dirty"))
    diverged_count = sum(1 for r in results if r.get("status") == "DIVERGED")
    print(f"Total: {len(results)} repositórios | Pendentes de Pull: {pending_pull} | Pendentes de Push: {pending_push} | Divergidos: {diverged_count} | Modificados (Dirty): {dirty_count}\n")
    if changelog_enabled:
        for r in results:
            cl = r.get("changelog")
            if cl and (cl.get("incoming") or cl.get("outgoing")):
                inc_s = format_changelog_summary(cl.get("incoming_categories", {})) or "-"
                out_s = format_changelog_summary(cl.get("outgoing_categories", {})) or "-"
                brk = " ⚠️ BREAKING" if cl.get("has_breaking") else ""
                print(f"  • {r['rel_path']}{brk}: incoming[{inc_s}] | outgoing[{out_s}]")
        print()
    if benchmark is not None:
        print_benchmark_report(benchmark, is_json=False)


def print_status_table(
    results: Sequence[RepoStatusRecord | Dict[str, Any]],
    changelog_enabled: bool = False,
    show_submodules: bool = False,
    audit_hygiene: bool = False,
    benchmark: Optional[TelemetryBenchmark] = None,
) -> None:
    """Render full human-readable multi-column status table with submodules, LFS, and changelog breakdown."""
    status_icons = {
        "UP_TO_DATE": "✅ OK",
        "PULL_PENDING": "⬇️ PULL",
        "PUSH_PENDING": "⬆️ PUSH",
        "DIVERGED": "⚠️ DIVERGIU",
        "AUX_PULL_PENDING": "⬇️ AUX_PULL",
        "AUX_PUSH_PENDING": "⬆️ AUX_PUSH",
        "NO_UPSTREAM": "⚠️ SEM UPSTREAM",
        "FETCH_ERROR": "❌ ERRO FETCH",
        "NO_REMOTE": "❌ SEM REMOTE",
    }

    print(f"\n{'STATUS':<15} {'REPOSITÓRIO':<40} {'BRANCH':<12} {'DETALHES'}")
    print("=" * 95)

    for r in results:
        raw_st = str(r.get("status", "UNKNOWN"))
        sym = status_icons.get(raw_st, raw_st)
        if raw_st == "FETCH_ERROR" and r.get("network_state") == "offline":
            sym = "🌐 OFFLINE"
        details: List[str] = []
        if r.get("is_worktree"):
            details.append("worktree")
        if r.get("worktrees"):
            linked_wts = [w for w in (r.get("worktrees") or []) if not w.get("is_main")]
            if linked_wts:
                details.append(f"{len(linked_wts)} worktree(s)")
        if r.get("stale_worktrees"):
            details.append(f"worktrees_stale({len(r['stale_worktrees'])})")
        subs = r.get("submodules") if isinstance(r.get("submodules"), list) else []
        if subs:
            dirty_subs = [s for s in subs if s.get("status") != "UP_TO_DATE"]
            shallow_subs = [s for s in subs if s.get("is_shallow")]
            sub_parts = []
            if dirty_subs:
                sub_parts.append(f"{len(dirty_subs)}/{len(subs)} pendentes")
            else:
                sub_parts.append(f"{len(subs)}")
            if shallow_subs:
                sub_parts.append(f"{len(shallow_subs)} shallow")
            details.append(f"submodules({', '.join(sub_parts)})")
        if r.get("sparse_checkout", {}).get("enabled"):
            sc = r["sparse_checkout"]
            cone_tag = "cone" if sc.get("cone_mode") else "full"
            warn_flag = "⚠️" if sc.get("warning") else ""
            details.append(f"{warn_flag}sparse({cone_tag}:{sc.get('pattern_count', 0)})")
        lfs_d = r.get("lfs") or {}
        if r.get("has_lfs") or lfs_d.get("has_lfs"):
            warn_flag = "⚠️" if lfs_d.get("warning") else ""
            if lfs_d.get("missing_objects", 0) > 0:
                details.append(f"{warn_flag}lfs({lfs_d['missing_objects']} unsmudged)")
            elif lfs_d.get("pointers_count", 0) > 0:
                details.append(f"{warn_flag}lfs({lfs_d['pointers_count']} pointers)")
            elif lfs_d.get("files_count", 0) > 0:
                details.append(f"lfs({lfs_d['files_count']} files)")
            elif lfs_d.get("pattern_count", 0) > 0:
                details.append(f"lfs({lfs_d['pattern_count']} patterns)")
            else:
                details.append("lfs")
        if r.get("multi_remote") or len(r.get("remotes", {})) > 1:
            remote_names = ", ".join(sorted(r.get("remotes", {}).keys()))
            details.append(f"remotes[{remote_names}]")
        behind = int(r.get("behind", 0))
        ahead = int(r.get("ahead", 0))
        if behind > 0:
            details.append(f"{behind} atrás")
        if ahead > 0:
            details.append(f"{ahead} à frente")
        cl = r.get("changelog")
        if changelog_enabled and cl:
            if cl.get("has_breaking"):
                details.append("⚠️ BREAKING")
            if cl.get("incoming_categories"):
                inc_s = format_changelog_summary(cl["incoming_categories"])
                details.append(f"incoming[{inc_s}]")
            if cl.get("outgoing_categories"):
                out_s = format_changelog_summary(cl["outgoing_categories"])
                details.append(f"outgoing[{out_s}]")
        if r.get("aux_branches_behind"):
            aux_desc = ", ".join(f"{k}:{v}" for k, v in r["aux_branches_behind"].items())
            details.append(f"aux_behind[{aux_desc}]")
        if r.get("aux_branches_ahead"):
            aux_ahead_desc = ", ".join(f"{k}:{v}" for k, v in r["aux_branches_ahead"].items())
            details.append(f"aux_ahead[{aux_ahead_desc}]")
        if r.get("is_dirty"):
            details.append(f"dirty({len(r.get('dirty_files', []))} arquivos)")
        if r.get("fetch_error"):
            diag = r.get("diagnostic")
            if diag:
                remed_str = f" (💡 {r['remediation']})" if r.get("remediation") else f" (💡 {diag['suggestion']})"
                details.append(f"erro[{diag['category']}]: {r['fetch_error']}{remed_str}")
            else:
                details.append(f"erro: {r['fetch_error']}")
        elif raw_st == "FETCH_ERROR":
            details.append("erro no fetch")
        if raw_st == "NO_REMOTE":
            remed_str = f" (💡 {r['remediation']})" if r.get("remediation") else ""
            details.append(f"sem remote configurado{remed_str}")
        elif raw_st == "NO_UPSTREAM":
            remed_str = f" (💡 {r['remediation']})" if r.get("remediation") else ""
            details.append(f"sem upstream configurado{remed_str}")
        elif not details:
            details.append("sincronizado")

        detail_str = "; ".join(details)
        rel_p = str(r.get("rel_path", ""))
        br = str(r.get("branch", ""))
        print(f"{sym:<15} {rel_p:<40} {br:<12} {detail_str}")
        if changelog_enabled and cl:
            if cl.get("incoming"):
                print(f"    ⬇️ incoming ({len(cl['incoming'])}):")
                for c in cl["incoming"]:
                    brk_tag = " [BREAKING]" if c.get("is_breaking") else ""
                    scope_tag = f"({c['scope']})" if c.get("scope") else ""
                    print(f"      • [{c['category']}]{brk_tag} {c['type']}{scope_tag}: {c['description']} ({c['hash']})")
            if cl.get("outgoing"):
                print(f"    ⬆️ outgoing ({len(cl['outgoing'])}):")
                for c in cl["outgoing"]:
                    brk_tag = " [BREAKING]" if c.get("is_breaking") else ""
                    scope_tag = f"({c['scope']})" if c.get("scope") else ""
                    print(f"      • [{c['category']}]{brk_tag} {c['type']}{scope_tag}: {c['description']} ({c['hash']})")
        if show_submodules and subs:
            print(f"    📦 submódulos ({len(subs)}):")
            for sub in subs:
                st_icon = "✅" if sub.get("status") == "UP_TO_DATE" and not sub.get("is_shallow") else "⚠️"
                sub_cmt = (sub.get("commit") or "")[:7]
                desc_str = f" ({sub.get('describe')})" if sub.get("describe") else ""
                detached_str = " [detached HEAD]" if sub.get("detached") else ""
                shallow_str = " [shallow]" if sub.get("is_shallow") else ""
                print(f"      • {st_icon} {sub.get('path')}: {sub.get('status')} [{sub_cmt}{desc_str}]{detached_str}{shallow_str}")
        if r.get("sparse_checkout", {}).get("enabled"):
            sc = r["sparse_checkout"]
            cone_tag = "cone" if sc.get("cone_mode") else "full pattern"
            pat_str = ", ".join(sc.get("patterns", [])) if sc.get("patterns") else "(root only)"
            warn_str = f" ⚠️ {sc['warning']}" if sc.get("warning") else ""
            print(f"    📐 sparse-checkout: {cone_tag} mode ({sc.get('pattern_count', 0)} pattern(s): {pat_str}){warn_str}")

    print("=" * 95)
    offline_count = sum(1 for r in results if r.get("network_state") == "offline")
    if offline_count:
        print(f"🌐 AVISO DE CONECTIVIDADE: {offline_count} repositório(s) inacessíveis por queda de rede/DNS.")
        print("   💡 Execute com '--no-fetch' para inspeção instantânea do estado local.\n")
    pending_pull = sum(1 for r in results if "PULL" in r.get("status", ""))
    pending_push = sum(1 for r in results if "PUSH" in r.get("status", ""))
    dirty_count = sum(1 for r in results if r.get("is_dirty"))
    print(f"Total: {len(results)} repositórios | Pendentes de Pull: {pending_pull} | Pendentes de Push: {pending_push} | Modificados (Dirty): {dirty_count}\n")
    if benchmark is not None:
        print_benchmark_report(benchmark, is_json=False)


def prompt_resolution_choice(
    repo_path: str,
    rel_path: str,
    details: Dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT,
    input_func: Any = input,
    max_tokens: Optional[int] = None,
) -> ResolveResult:
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
            diff_text = get_divergence_diff(
                repo_path,
                branch=details.get("branch"),
                timeout=timeout,
                max_tokens=max_tokens or DEFAULT_COMPACT_DIFF_TOKENS,
            )
            if diff_text:
                print("\n--- DIFF INÍCIO ---")
                print(format_diff_stat(diff_text))
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


def render_pull_item(res: PullResult | Dict[str, Any], root: str, dry_run: bool = False) -> None:
    """Format and print an individual repository pull operation output row and sub-actions."""
    mode_str = "[DRY-RUN] " if dry_run else ""
    icon = "ℹ️" if dry_run else ("✅" if res.get("success") else "❌")
    detail_raw = "OK" if res.get("success") else (res.get("stderr") or res.get("stdout") or "")
    detail_msg = str(detail_raw)
    if dry_run and res.get("success") and res.get("stdout"):
        detail_msg = str(res["stdout"])
    res_resolved = res.get("resolved")
    if res_resolved:
        strat_applied = res_resolved.get("strategy")
        detail_msg = f"{detail_msg} (resolvido via {strat_applied})"
    elif res.get("autostash", {}).get("would_stash"):
        detail_msg = f"{detail_msg} (autostash de {len(res['autostash']['dirty_files'])} arquivos seria realizado)"
    elif res.get("autostash", {}).get("stashed"):
        if res["autostash"].get("conflict"):
            detail_msg = f"{detail_msg} (conflito ao restaurar stash mantido em {res['autostash'].get('stash_ref')})"
        elif res["autostash"].get("restored"):
            detail_msg = f"{detail_msg} (autostash restaurado com sucesso)"
    res_diag = res.get("diagnostic")
    if not res.get("success") and res_diag:
        detail_msg += f" (💡 {res_diag.get('suggestion')})"
    remote_tag = f" [{res['remote']}]" if res.get("remote") else ""
    wt_tag = " [worktree]" if res.get("is_worktree") else ""
    rel = str(res.get("repo_rel") or os.path.relpath(str(res.get("repo", "")), root))
    print(f"{icon} {mode_str}[{rel}]{wt_tag}{remote_tag} ({res.get('branch')}): {detail_msg}")
    for aux_b, aux_ok in (res.get("aux_branches") or {}).items():
        aux_icon = "  ↳ ℹ️ [DRY-RUN]" if dry_run else ("  ↳ ✅" if aux_ok else "  ↳ ❌")
        aux_status = "fast-forward previsto" if dry_run else "fast-forward concluído"
        print(f"{aux_icon} Branch auxiliar '{aux_b}': {aux_status}")
    for iso_b, target_wt in (res.get("worktree_isolated") or {}).items():
        try:
            iso_rel = os.path.relpath(target_wt, root)
        except ValueError:
            iso_rel = target_wt
        iso_icon = "  ↳ ℹ️ [DRY-RUN]" if dry_run else "  ↳ ⚠️"
        iso_status = "isolada de fast-forward" if dry_run else "isolada (ativa em worktree alvo)"
        print(f"{iso_icon} Branch auxiliar '{iso_b}': {iso_status} [{iso_rel}]")
    sub_info = res.get("submodules")
    if sub_info and sub_info.get("has_submodules"):
        unshallowed_set = set(sub_info.get("unshallowed", []))
        for sub in sub_info.get("submodules", []):
            sub_path = sub.get("path")
            sub_st = sub.get("status")
            sub_commit = (sub.get("commit") or "")[:7]
            sub_desc = f" ({sub.get('describe')})" if sub.get("describe") else ""
            if sub_path in unshallowed_set:
                sub_shallow = " [unshallowed]"
            elif sub.get("is_shallow"):
                sub_shallow = " [shallow]"
            else:
                sub_shallow = ""
            sub_icon = "  ↳ ℹ️ [DRY-RUN]" if dry_run else ("  ↳ ✅" if sub_info.get("success") else "  ↳ ❌")
            sub_action = "atualização recursiva prevista" if dry_run else ("submódulo atualizado" if sub_st == "UP_TO_DATE" else f"submódulo {sub_st}")
            print(f"{sub_icon} Submódulo '{sub_path}': {sub_action} [{sub_commit}{sub_desc}]{sub_shallow}")
    sp_res = res.get("sparse_reapply")
    if sp_res:
        sp_icon = "  ↳ ✅" if sp_res.get("success") else "  ↳ ❌"
        sp_action = "filtros cone de sparse-checkout reaplicados" if sp_res.get("success") else f"falha ao reaplicar sparse-checkout: {sp_res.get('stderr')}"
        print(f"{sp_icon} Sparse-checkout: {sp_action}")
    if res.get("lfs"):
        lfs_r = res["lfs"]
        if lfs_r.get("skipped"):
            lfs_icon = "  ↳ ℹ️ [DRY-RUN]" if dry_run else "  ↳ 📦"
            print(f"{lfs_icon} Git LFS: download de payloads ignorado (--no-lfs / GIT_LFS_SKIP_SMUDGE=1)")
        elif lfs_r.get("fetched"):
            lfs_pull_ok = lfs_r.get("pull_result", {}).get("success", True) if not dry_run else True
            lfs_icon = "  ↳ ℹ️ [DRY-RUN]" if dry_run else ("  ↳ ✅" if lfs_pull_ok else "  ↳ ❌")
            lfs_st = "sincronização de objetos prevista ('git lfs pull')" if dry_run else ("objetos sincronizados via 'git lfs pull'" if lfs_pull_ok else "falha ao sincronizar objetos LFS")
            print(f"{lfs_icon} Git LFS: {lfs_st}")


def render_push_item(res: PushResult | Dict[str, Any], dry_run: bool = False) -> None:
    """Format and print an individual repository push operation result."""
    mode_str = "[DRY-RUN] " if res.get("dry_run") else ""
    res_diag = res.get("diagnostic")
    diag_cat = res_diag.get("category") if res_diag else ""
    if res.get("success"):
        icon = "✅"
    elif diag_cat in ("PROTECTED_BRANCH_FORCE_PUSH", "PROTECTED_BRANCH_DIRECT_PUSH"):
        icon = "🛡️"
    else:
        icon = "❌"
    status_raw = "OK" if res.get("success") else (res.get("stderr") or res.get("stdout") or "")
    status_detail = str(status_raw)
    if not res.get("success") and res_diag:
        status_detail += f" (💡 {res_diag.get('suggestion')})"
    ahead_info = f" ({res['ahead']} commit(s) à frente)" if int(res.get("ahead", 0)) > 0 else ""
    remote_tag = f" -> {res['remote']}" if res.get("remote") else ""
    wt_tag = " [worktree]" if res.get("is_worktree") else ""
    rel = res.get("repo_rel", os.path.basename(str(res.get("repo", ""))))
    print(f"{icon} {mode_str}[{rel}]{wt_tag} ({res.get('branch')}{ahead_info}{remote_tag}): {status_detail}")


def print_dirty_report(dirty: Sequence[RepoStatusRecord | Dict[str, Any]]) -> None:
    """Print uncommitted dirty and untracked files report."""
    if not dirty:
        print("✅ Nenhum repositório com arquivos modificados ou não rastreados.")
        return
    print(f"\nEncontrados {len(dirty)} repositório(s) com alterações locais:")
    for r in dirty:
        print(f"\n📂 {r['rel_path']}:")
        for f in r.get("dirty_files", []):
            print(f"   {f}")
    print()


def print_unlock_report(results: List[Dict[str, Any]]) -> None:
    """Render repository lock recovery report."""
    print("=" * 70)
    print("🔓 RECUPERAÇÃO DE REPOSITÓRIOS COM LOCK (.git/*.lock)")
    print("=" * 70)
    for res in results:
        status_icon = "✅" if res["status"] == "UNLOCKED" else ("⚠️" if res["status"] == "SKIPPED_ACTIVE" else "❌")
        print(f"  {status_icon} {res['rel_path']:<35} {res['lock_name']:<18} {res['message']}")
    print("=" * 70)
    unlocked_count = sum(1 for r in results if r["status"] == "UNLOCKED")
    print(f"Concluído: {unlocked_count} lock(s) removido(s).\n")


def print_worktree_prune_report(
    pruned_repos: List[Dict[str, Any]],
    total_pruned: int,
    all_warnings: List[str],
    root: str,
    dry_run: bool = False,
) -> None:
    """Render worktree prune and stale cleanup report."""
    mode_tag = "[DRY-RUN] " if dry_run else ""
    print("=" * 80)
    print(f"🧹 {mode_tag}LIMPEZA E PRUNING DE WORKTREES STALE / ÓRFÃOS")
    print("=" * 80)

    if not pruned_repos:
        print(f"\n✅ Nenhum worktree stale ou órfão encontrado para prune em {root}.\n")
        return

    for pr in pruned_repos:
        print(f"\n📦 {pr['rel_path']}:")
        for p_item in pr.get("pruned", []):
            act = "Seria prunado" if dry_run else "Prunado"
            print(f"  🧹 {act} worktree '{p_item['name']}': {p_item['reason']}")
        for w in pr.get("warnings", []):
            print(f"  ⚠️  {w}")

    print("=" * 80)
    act_label = "seria(m) prunado(s)" if dry_run else "prunado(s)"
    print(f"Concluído: {total_pruned} worktree(s) {act_label}. {len(all_warnings)} aviso(s).\n")


def print_worktree_list_report(entries: List[Dict[str, Any]], root: str) -> None:
    """Render worktrees inventory table."""
    if not entries:
        print(f"\n✅ Nenhum repositório com múltiplos worktrees encontrado em {root}.\n")
        return

    print(f"\n{'REPOSITÓRIO':<35} {'WORKTREE PATH':<40} {'BRANCH':<15} {'STATUS'}")
    print("=" * 105)
    for entry in entries:
        for wt in entry.get("worktrees", []):
            st = "main" if wt.get("is_main") else "linked"
            if wt.get("is_prunable"):
                st = f"prunável ({wt.get('prunable_reason', '')})"
            elif wt.get("is_locked"):
                st = f"locked ({wt.get('lock_reason', '')})"
            print(f"{entry['rel_path']:<35} {wt['path']:<40} {wt.get('branch', 'detached'):<15} {st}")
    print("=" * 105)
    print()


__all__ = [
    "build_status_summary",
    "print_benchmark_report",
    "print_dirty_report",
    "print_doctor_report",
    "print_ssh_audit_report",
    "print_status_compact",
    "print_status_table",
    "print_unlock_report",
    "print_worktree_list_report",
    "print_worktree_prune_report",
    "prompt_resolution_choice",
    "render_pull_item",
    "render_push_item",
]
