"""
Installer and Shell Completion module for git-sync.
Provides safe, idempotent global symlinking to ~/.local/bin/git-sync,
path inspection, and shell autocompletion generation for Bash and Zsh.
"""

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_default_install_dir() -> Path:
    """Resolve the default global bin directory for CLI installation."""
    xdg_bin = os.environ.get("XDG_BIN_HOME")
    if xdg_bin:
        return Path(xdg_bin).expanduser().resolve()

    candidates: List[Path] = [Path.home() / ".local" / "bin"]

    # Check host user home if running inside an isolated profile
    try:
        import pwd
        pw_home = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "bin"
        if pw_home not in candidates:
            candidates.append(pw_home)
    except Exception:
        pass

    user = os.environ.get("USER")
    if user:
        u_home = Path(f"/home/{user}/.local/bin")
        if u_home not in candidates:
            candidates.append(u_home)

    # Prefer candidate that is already present in PATH
    for candidate in candidates:
        if is_in_path(candidate):
            return candidate

    return candidates[0]


def get_launcher_path() -> Path:
    """
    Resolve the canonical absolute path of the git-sync launcher script.
    # INVARIANT: Preserves link integrity by resolving to realpath of source repository launcher.
    """
    repo_launcher = Path(__file__).resolve().parent.parent.parent / "git-sync"
    if repo_launcher.is_file():
        return repo_launcher
    if sys.argv and sys.argv[0]:
        argv_launcher = Path(sys.argv[0]).resolve()
        if argv_launcher.is_file() and argv_launcher.name == "git-sync":
            return argv_launcher
    return repo_launcher


def is_in_path(directory: Path) -> bool:
    """Check whether the given directory is present in the PATH environment variable."""
    try:
        resolved_target = str(directory.resolve())
    except (OSError, ValueError):
        resolved_target = str(directory)

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    for entry in path_entries:
        if not entry:
            continue
        try:
            if str(Path(entry).expanduser().resolve()) == resolved_target:
                return True
        except (OSError, ValueError):
            continue
    return False


def install_symlink(
    target_dir: Optional[str] = None,
    launcher_path: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Safely and idempotently install a symlink to git-sync launcher in target_dir.
    # INVARIANT: Uses atomic symlink replacement (via tempfile and os.replace)
    # to guarantee zero-hang and prevent broken symlinks if interrupted.
    """
    target_dir_path = Path(target_dir).expanduser().resolve() if target_dir else get_default_install_dir()
    src_file = Path(launcher_path).expanduser().resolve() if launcher_path else get_launcher_path()

    if not src_file.is_file():
        return {
            "ok": False,
            "error": f"Launcher executable not found at '{src_file}'",
            "path": None,
            "target": str(src_file),
            "in_path": False,
        }

    # Ensure source file is executable
    try:
        current_mode = src_file.stat().st_mode
        if not (current_mode & 0o111):
            os.chmod(src_file, current_mode | 0o755)
    except OSError as err:
        return {
            "ok": False,
            "error": f"Failed to set executable permissions on '{src_file}': {err}",
            "path": None,
            "target": str(src_file),
            "in_path": False,
        }

    # Ensure target directory exists
    try:
        target_dir_path.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        return {
            "ok": False,
            "error": f"Failed to create target directory '{target_dir_path}': {err}",
            "path": None,
            "target": str(src_file),
            "in_path": False,
        }

    dest_link = target_dir_path / "git-sync"
    in_path = is_in_path(target_dir_path)

    # Check if destination already exists
    if dest_link.is_symlink():
        try:
            raw_target = os.readlink(dest_link)
            resolved_link = dest_link.resolve()
            if resolved_link == src_file or raw_target == str(src_file):
                return {
                    "ok": True,
                    "action": "already_installed",
                    "path": str(dest_link),
                    "target": str(src_file),
                    "in_path": in_path,
                    "message": f"git-sync is already installed and up to date at {dest_link} -> {src_file}",
                }
        except OSError:
            pass

        if not force:
            raw_target = os.readlink(dest_link) if dest_link.is_symlink() else "unknown"
            return {
                "ok": False,
                "error": f"Symlink already exists at '{dest_link}' pointing to '{raw_target}'. Pass --force to replace.",
                "path": str(dest_link),
                "target": str(src_file),
                "in_path": in_path,
            }
    elif dest_link.exists():
        if not force:
            return {
                "ok": False,
                "error": f"Regular file or directory already exists at '{dest_link}'. Pass --force to replace.",
                "path": str(dest_link),
                "target": str(src_file),
                "in_path": in_path,
            }

    # Create atomic symlink via temporary file and os.replace
    tmp_link = target_dir_path / f".git-sync-tmp-{os.getpid()}-{int(time.time() * 1000)}"
    try:
        os.symlink(src_file, tmp_link)
        os.replace(tmp_link, dest_link)
    except OSError as err:
        if tmp_link.is_symlink() or tmp_link.exists():
            try:
                tmp_link.unlink()
            except OSError:
                pass
        return {
            "ok": False,
            "error": f"Failed to create symlink at '{dest_link}': {err}",
            "path": str(dest_link),
            "target": str(src_file),
            "in_path": in_path,
        }

    return {
        "ok": True,
        "action": "installed",
        "path": str(dest_link),
        "target": str(src_file),
        "in_path": in_path,
        "message": f"Successfully installed git-sync to {dest_link} -> {src_file}",
    }


def uninstall_symlink(
    target_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Safely uninstall the git-sync global symlink."""
    target_dir_path = Path(target_dir).expanduser().resolve() if target_dir else get_default_install_dir()
    dest_link = target_dir_path / "git-sync"

    if not dest_link.is_symlink() and not dest_link.exists():
        return {
            "ok": True,
            "action": "not_installed",
            "path": str(dest_link),
            "message": f"git-sync is not installed at {dest_link}",
        }

    try:
        dest_link.unlink()
        return {
            "ok": True,
            "action": "uninstalled",
            "path": str(dest_link),
            "message": f"Successfully uninstalled git-sync from {dest_link}",
        }
    except OSError as err:
        return {
            "ok": False,
            "error": f"Failed to remove '{dest_link}': {err}",
            "path": str(dest_link),
        }


def get_canonical_skill_dir() -> Path:
    """
    Resolve the canonical path to the repository-hosted agent skill.
    # INVARIANT: Self-contained skill directory lives at repo root skills/git-sync.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / "skills" / "git-sync"


def get_default_skill_dirs() -> List[Path]:
    """
    Resolve candidate directories across Antigravity profiles and Cursor for skill discovery.
    # INVARIANT: Scans current user home and host profile homes without raising on missing paths.
    """
    candidates: List[Path] = []
    seen: set[str] = set()

    def add_candidate(p: Path) -> None:
        try:
            resolved = str(p.expanduser().resolve())
            if resolved not in seen:
                seen.add(resolved)
                candidates.append(Path(resolved))
        except (OSError, ValueError):
            pass

    # 1. Current user home
    home = Path.home()
    add_candidate(home / ".gemini" / "config" / "skills")
    add_candidate(home / ".cursor" / "skills")

    # 2. Antigravity isolated profiles
    profiles_root = home / "AntigravityProfiles"
    if profiles_root.is_dir():
        for prof_dir in profiles_root.iterdir():
            if prof_dir.is_dir():
                add_candidate(prof_dir / ".gemini" / "config" / "skills")
                add_candidate(prof_dir / ".cursor" / "skills")

    # 3. Host user home if running inside an isolated profile
    try:
        import pwd
        pw_dir = Path(pwd.getpwuid(os.getuid()).pw_dir)
        add_candidate(pw_dir / ".gemini" / "config" / "skills")
        add_candidate(pw_dir / ".cursor" / "skills")
    except Exception:
        pass

    user = os.environ.get("USER")
    if user:
        add_candidate(Path(f"/home/{user}/.gemini/config/skills"))
        add_candidate(Path(f"/home/{user}/.cursor/skills"))

    # Return only candidates whose parent config directory exists or that already exist
    valid_dirs: List[Path] = []
    for c in candidates:
        if c.is_dir() or c.parent.is_dir():
            valid_dirs.append(c)

    return valid_dirs


def install_agent_skills(
    target_dirs: Optional[List[str]] = None,
    force: bool = False,
    skill_src: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Safely deploy symlinks to the canonical git-sync skill into host agent skill directories.
    # INVARIANT: Uses atomic symlink replacement via temporary symlinks to prevent broken links.
    """
    src_path = Path(skill_src).expanduser().resolve() if skill_src else get_canonical_skill_dir()
    if not (src_path / "SKILL.md").is_file():
        return [{
            "ok": False,
            "error": f"Canonical SKILL.md not found in '{src_path}'",
            "path": None,
            "target": str(src_path),
        }]

    if target_dirs:
        dirs_to_check = [Path(d).expanduser().resolve() for d in target_dirs]
    else:
        dirs_to_check = get_default_skill_dirs()

    results: List[Dict[str, Any]] = []

    for s_dir in dirs_to_check:
        try:
            s_dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            results.append({
                "ok": False,
                "error": f"Failed to create skills directory '{s_dir}': {err}",
                "path": str(s_dir / "git-sync"),
                "target": str(src_path),
            })
            continue

        dest_link = s_dir / "git-sync"

        # Check existing destination
        if dest_link.is_symlink():
            try:
                raw_target = os.readlink(dest_link)
                resolved_link = dest_link.resolve()
                if resolved_link == src_path or raw_target == str(src_path):
                    results.append({
                        "ok": True,
                        "action": "already_installed",
                        "path": str(dest_link),
                        "target": str(src_path),
                        "message": f"Agent skill already up to date at {dest_link} -> {src_path}",
                    })
                    continue
            except OSError:
                pass

            if not force:
                raw_target = os.readlink(dest_link) if dest_link.is_symlink() else "unknown"
                results.append({
                    "ok": False,
                    "error": f"Skill symlink already exists at '{dest_link}' pointing to '{raw_target}'. Pass --force to replace.",
                    "path": str(dest_link),
                    "target": str(src_path),
                })
                continue
        elif dest_link.exists():
            if not force:
                results.append({
                    "ok": False,
                    "error": f"Regular file or directory exists at '{dest_link}'. Pass --force to replace.",
                    "path": str(dest_link),
                    "target": str(src_path),
                })
                continue

        # Atomic symlink creation
        tmp_link = s_dir / f".git-sync-skill-tmp-{os.getpid()}-{int(time.time() * 1000)}"
        try:
            os.symlink(src_path, tmp_link)
            os.replace(tmp_link, dest_link)
            results.append({
                "ok": True,
                "action": "installed",
                "path": str(dest_link),
                "target": str(src_path),
                "message": f"Successfully linked agent skill to {dest_link} -> {src_path}",
            })
        except OSError as err:
            if tmp_link.is_symlink() or tmp_link.exists():
                try:
                    tmp_link.unlink()
                except OSError:
                    pass
            results.append({
                "ok": False,
                "error": f"Failed to create skill symlink at '{dest_link}': {err}",
                "path": str(dest_link),
                "target": str(src_path),
            })

    return results


def uninstall_agent_skills(
    target_dirs: Optional[List[str]] = None,
    skill_src: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Safely remove git-sync agent skill symlinks from host skill directories."""
    src_path = Path(skill_src).expanduser().resolve() if skill_src else get_canonical_skill_dir()
    if target_dirs:
        dirs_to_check = [Path(d).expanduser().resolve() for d in target_dirs]
    else:
        dirs_to_check = get_default_skill_dirs()

    results: List[Dict[str, Any]] = []
    for s_dir in dirs_to_check:
        dest_link = s_dir / "git-sync"
        if not dest_link.is_symlink() and not dest_link.exists():
            results.append({
                "ok": True,
                "action": "not_installed",
                "path": str(dest_link),
                "message": f"Agent skill is not installed at {dest_link}",
            })
            continue

        try:
            dest_link.unlink()
            results.append({
                "ok": True,
                "action": "uninstalled",
                "path": str(dest_link),
                "message": f"Successfully uninstalled agent skill from {dest_link}",
            })
        except OSError as err:
            results.append({
                "ok": False,
                "error": f"Failed to remove skill at '{dest_link}': {err}",
                "path": str(dest_link),
            })

    return results


def get_default_completion_dir(shell: str) -> Path:
    """Resolve the default user completion directory for the specified shell."""
    if shell == "bash":
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data).expanduser().resolve() / "bash-completion" / "completions"
        return Path.home() / ".local" / "share" / "bash-completion" / "completions"
    elif shell == "zsh":
        return Path.home() / ".zfunc"
    else:
        raise ValueError(f"Unsupported shell: '{shell}'")


def generate_completion_script(shell: str) -> str:
    """
    Generate shell autocompletion script for Bash or Zsh.
    # INVARIANT: Scripts are completely hermetic, zero-dependency, and handle dynamic repo listing.
    """
    if shell == "bash":
        return _generate_bash_completion()
    elif shell == "zsh":
        return _generate_zsh_completion()
    else:
        raise ValueError(f"Unsupported shell '{shell}'. Supported: 'bash', 'zsh'")


def install_completion(
    shell: str,
    target_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Install autocompletion script to user's shell completion directory."""
    try:
        script = generate_completion_script(shell)
    except ValueError as err:
        return {
            "ok": False,
            "error": str(err),
            "shell": shell,
            "path": None,
        }

    try:
        target_dir_path = Path(target_dir).expanduser().resolve() if target_dir else get_default_completion_dir(shell)
        target_dir_path.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        return {
            "ok": False,
            "error": f"Failed to create completion directory '{target_dir}': {err}",
            "shell": shell,
            "path": None,
        }

    filename = "git-sync" if shell == "bash" else "_git-sync"
    dest_file = target_dir_path / filename

    try:
        dest_file.write_text(script, encoding="utf-8")
    except OSError as err:
        return {
            "ok": False,
            "error": f"Failed to write completion script to '{dest_file}': {err}",
            "path": str(dest_file),
            "shell": shell,
        }

    instructions = ""
    if shell == "bash":
        instructions = f"Source the script or reload your shell:\\n  source {dest_file}"
    elif shell == "zsh":
        instructions = (
            f"Add to your ~/.zshrc if not already present:\\n"
            f"  fpath=({target_dir_path} $fpath)\\n"
            f"  autoload -Uz compinit && compinit"
        )

    return {
        "ok": True,
        "shell": shell,
        "path": str(dest_file),
        "instructions": instructions,
        "message": f"Successfully installed {shell} completion script to {dest_file}",
    }


def _generate_bash_completion() -> str:
    """Generate self-contained Bash completion script."""
    return """# bash completion for git-sync -*- shell-script -*-
# INVARIANT: Zero-dependency completion script compatible with standard bash-completion
# and Git's native subcommand dispatcher (_git_sync for `git sync <TAB>`).

_git_sync_completions() {
    local cur prev words cword
    if [ -n "${COMP_LINE:-}" ] && declare -F _init_completion >/dev/null 2>&1; then
        _init_completion -n = || return
    else
        COMPREPLY=()
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
        words=("${COMP_WORDS[@]}")
        cword=$COMP_CWORD
    fi

    local commands="doctor status dashboard tui pull push resolve dirty unlock config install completion worktree"
    local common_opts="--config --profile --root --workers --auto-tune --adaptive --timeout --retries --preflight --exclude --compact --summary --changelog --submodules --no-submodules --unshallow --max-tokens --json --benchmark --telemetry --no-cache --remote --dry-run --no-dry-run --help -h"

    # Identify active subcommand (ignoring leading `git` and `sync` tokens when invoked as `git sync`)
    local cmd=""
    local i=1
    while [ $i -lt $cword ]; do
        case "${words[i]}" in
            doctor|status|dashboard|tui|pull|push|resolve|dirty|unlock|config|install|completion|worktree)
                cmd="${words[i]}"
                break
                ;;
            *)
                ;;
        esac
        ((i++))
    done

    # Top-level completion (no subcommand chosen yet)
    if [ -z "$cmd" ]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "$common_opts --fetch --no-fetch --pending --dashboard --tui --group" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$commands" -- "$cur"))
        fi
        return 0
    fi

    # Subcommand-specific completion
    case "$cmd" in
        status|dashboard|tui)
            COMPREPLY=($(compgen -W "$common_opts --fetch --no-fetch --pending --changelog --dashboard --tui -i --interactive --group" -- "$cur"))
            return 0
            ;;
        doctor)
            COMPREPLY=($(compgen -W "$common_opts --ssh --group" -- "$cur"))
            return 0
            ;;
        pull)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$common_opts --all --ff-only --no-ff-only --sync-branches --no-sync-branches --autostash --no-autostash --recover-locks --strategy -i --interactive --group" -- "$cur"))
            elif [ "$prev" = "--strategy" ]; then
                COMPREPLY=($(compgen -W "abort rebase merge" -- "$cur"))
            else
                local repos
                repos=$(git-sync --complete-repos 2>/dev/null)
                COMPREPLY=($(compgen -W "$repos" -- "$cur"))
            fi
            return 0
            ;;
        push)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$common_opts --all --branch --fetch --no-fetch --group --force --block-protected --allow-protected --protected-branches" -- "$cur"))
            else
                local repos
                repos=$(git-sync --complete-repos 2>/dev/null)
                COMPREPLY=($(compgen -W "$repos" -- "$cur"))
            fi
            return 0
            ;;
        resolve)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$common_opts --all --strategy --diff --max-tokens -i --interactive --group --fetch --no-fetch" -- "$cur"))
            elif [ "$prev" = "--strategy" ]; then
                COMPREPLY=($(compgen -W "abort rebase merge stash" -- "$cur"))
            else
                local repos
                repos=$(git-sync --complete-repos 2>/dev/null)
                COMPREPLY=($(compgen -W "$repos" -- "$cur"))
            fi
            return 0
            ;;
        unlock)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$common_opts --all --force --group" -- "$cur"))
            else
                local repos
                repos=$(git-sync --complete-repos 2>/dev/null)
                COMPREPLY=($(compgen -W "$repos" -- "$cur"))
            fi
            return 0
            ;;
        dirty)
            COMPREPLY=($(compgen -W "$common_opts --group" -- "$cur"))
            return 0
            ;;
        config)
            COMPREPLY=($(compgen -W "$common_opts --init --edit --global" -- "$cur"))
            return 0
            ;;
        install)
            COMPREPLY=($(compgen -W "$common_opts --target-dir --force --uninstall" -- "$cur"))
            return 0
            ;;
        completion)
            if [ "$prev" = "completion" ]; then
                COMPREPLY=($(compgen -W "bash zsh" -- "$cur"))
            else
                COMPREPLY=($(compgen -W "$common_opts --install --target-dir" -- "$cur"))
            fi
            return 0
            ;;
        worktree)
            if [ "$prev" = "worktree" ]; then
                COMPREPLY=($(compgen -W "prune list" -- "$cur"))
            elif [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "$common_opts --all --expire --group" -- "$cur"))
            else
                local repos
                repos=$(git-sync --complete-repos 2>/dev/null)
                COMPREPLY=($(compgen -W "$repos" -- "$cur"))
            fi
            return 0
            ;;
        *)
            COMPREPLY=($(compgen -W "$common_opts" -- "$cur"))
            return 0
            ;;
    esac
}

# Native `git sync <TAB>` hook invoked by git-completion.bash
_git_sync() {
    _git_sync_completions "$@"
}

complete -F _git_sync_completions git-sync
"""


def _generate_zsh_completion() -> str:
    """Generate self-contained Zsh completion script."""
    return """#compdef git-sync git-sync=git
# INVARIANT: Zero-dependency completion script for Zsh supporting both `git-sync` and `git sync`.

_git_sync() {
    local curcontext="$curcontext" state line
    typeset -A opt_args

    # Normalize word array when invoked via `git sync <TAB>` with unshifted `git` prefix
    if [[ "${words[1]}" == "git" && "${words[2]}" == "sync" ]]; then
        shift words
        (( CURRENT-- ))
    fi

    local -a common_args=(
        '--config[Path to custom .git-sync.json configuration file]:file:_files'
        '--profile[Named workspace profile to activate]:profile:'
        '--root[Root directory for repositories]:directory:_files -/'
        '--workers[Concurrent worker thread count or "auto"]:workers:'
        '--auto-tune[Dynamically auto-tune worker concurrency based on host cores]'
        '--adaptive[Alias for --auto-tune]'
        '--timeout[Git command timeout in seconds]:seconds:'
        '--retries[Git network retry attempts on transient errors]:retries:'
        '--preflight[Run pre-flight credential and remote sanity check]'
        '--compact[Output compact summary omitting repository items array]'
        '--summary[Alias for --compact]'
        '--changelog[Classify pending commits into Keep-a-Changelog categories]'
        '--submodules[Inspect drift and recursively update git submodules]'
        '--no-submodules[Do not update or inspect submodules]'
        '--unshallow[Fetch full history for shallow submodules and repositories]'
        '--max-tokens[Maximum token budget for diff output]:tokens:'
        '--json[Output machine-readable JSON]'
        '--benchmark[Measure and display execution performance telemetry]'
        '--telemetry[Alias for --benchmark]'
        '--no-cache[Disable in-memory bulk cache across worktree and remote audits]'
        '--remote[Target specific Git remote]:remote:'
        '--dry-run[Simulate execution without modifying repositories]'
        '--no-dry-run[Disable dry-run simulation mode]'
        '(-h --help)'{-h,--help}'[Display help message]'
    )

    _arguments -C \\
        $common_args \\
        '1: :->command' \\
        '*:: :->args'

    case $state in
        command)
            local -a commands=(
                'doctor:Run proactive credential and connectivity diagnostics'
                'status:Show status of all repositories'
                'dashboard:Launch interactive compact terminal dashboard'
                'tui:Alias for interactive terminal dashboard'
                'pull:Pull changes for specified repositories or --all'
                'push:Push committed changes to remote repository'
                'resolve:Inspect and resolve divergent branches'
                'dirty:Show repositories with uncommitted or untracked changes'
                'unlock:Inspect and recover stale Git lock files'
                'config:Display or bootstrap workspace profile and configuration'
                'install:Install or update global symlink in ~/.local/bin'
                'completion:Generate shell autocompletion script (bash or zsh)'
                'worktree:Inspect or prune linked worktree metadata and orphaned directories'
            )
            _describe -t commands 'git-sync command' commands
            ;;
        args)
            case $line[1] in
                doctor)
                    _arguments \\
                        $common_args \\
                        '--ssh[Benchmark SSH ControlMaster multiplexing]' \\
                        '--group[Filter target repositories by group]:group:'
                    ;;
                status|dashboard|tui)
                    _arguments \\
                        $common_args \\
                        '--fetch[Fetch remotes]' \\
                        '--no-fetch[Skip remote fetch]' \\
                        '--pending[Only show repos with pending actions or dirty files]' \\
                        '--changelog[Classify pending commits into Keep-a-Changelog categories]' \\
                        '--dashboard[Launch interactive terminal dashboard]' \\
                        '--tui[Alias for --dashboard]' \\
                        '(-i --interactive)'{-i,--interactive}'[Interactive triage loop]' \\
                        '--group[Filter by subfolder/group]:group:'
                    ;;
                pull)
                    _arguments \\
                        $common_args \\
                        '--all[Pull all repositories with pending commits]' \\
                        '--ff-only[Require fast-forward merge]' \\
                        '--no-ff-only[Allow non fast-forward merge]' \\
                        '--sync-branches[Fast-forward auxiliary tracking branches]' \\
                        '--no-sync-branches[Skip fast-forward auxiliary tracking branches]' \\
                        '--autostash[Automatically stash dirty changes before pull and restore after]' \\
                        '--no-autostash[Do not autostash dirty changes]' \\
                        '--recover-locks[Automatically clean up stale .git/*.lock files]' \\
                        '--strategy[Fallback resolution strategy]:strategy:(abort rebase merge)' \\
                        '(-i --interactive)'{-i,--interactive}'[Prompt interactively if fast-forward fails]' \\
                        '--group[Filter target repositories by group]:group:' \\
                        '*:repository:->repos'
                    ;;
                push)
                    _arguments \\
                        $common_args \\
                        '--all[Push all repositories with pending commits]' \\
                        '--branch[Specific branch to push]:branch:' \\
                        '--fetch[Fetch remotes before push inspection]' \\
                        '--no-fetch[Skip remote fetch before push inspection]' \\
                        '--group[Filter target repositories by group]:group:' \\
                        '--force[Force push with lease (strictly prohibited on protected branches)]' \\
                        '--block-protected[Block direct push to protected branches]' \\
                        '--allow-protected[Allow direct push to protected branches when blocked by policy]' \\
                        '--protected-branches[Comma-separated patterns of protected branches]:branches:' \\
                        '*:repository:->repos'
                    ;;
                resolve)
                    _arguments \\
                        $common_args \\
                        '--all[Resolve all repositories with divergent branches]' \\
                        '--strategy[Automated resolution strategy]:strategy:(abort rebase merge stash)' \\
                        '--diff[Display divergence diff without applying changes]' \\
                        '--max-tokens[Maximum token budget for diff output]:tokens:' \\
                        '(-i --interactive)'{-i,--interactive}'[Prompt interactively for resolution strategy]' \\
                        '--group[Filter target repositories by group]:group:' \\
                        '--fetch[Fetch remotes before checking divergence]' \\
                        '--no-fetch[Skip remote fetch before checking divergence]' \\
                        '*:repository:->repos'
                    ;;
                unlock)
                    _arguments \\
                        $common_args \\
                        '--all[Inspect and unlock all repositories]' \\
                        '--force[Force remove active locks younger than threshold]' \\
                        '--group[Filter target repositories by group]:group:' \\
                        '*:repository:->repos'
                    ;;
                dirty)
                    _arguments \\
                        $common_args \\
                        '--group[Filter repositories by group]:group:'
                    ;;
                config)
                    _arguments \\
                        $common_args \\
                        '--init[Bootstrap default configuration file]' \\
                        '--edit[Open configuration file in editor]' \\
                        '--global[Target global XDG configuration file]'
                    ;;
                install)
                    _arguments \\
                        $common_args \\
                        '--target-dir[Installation directory for symlink]:directory:_files -/' \\
                        '--force[Overwrite existing file or symlink]' \\
                        '--uninstall[Remove installed symlink]'
                    ;;
                completion)
                    _arguments \\
                        $common_args \\
                        '--install[Install completion script to shell completions directory]' \\
                        '--target-dir[Target directory for completion script]:directory:_files -/' \\
                        '1:shell:(bash zsh)'
                    ;;
                worktree)
                    _arguments \\
                        $common_args \\
                        '--all[Prune worktrees across all repositories]' \\
                        '--expire[Expire worktrees older than time (default: now)]:time:' \\
                        '--group[Filter target repositories by group]:group:' \\
                        '1:action:(prune list)' \\
                        '*:repository:->repos'
                    ;;
            esac

            if [[ "$state" == "repos" ]]; then
                local -a repo_list
                repo_list=(${(f)"$(git-sync --complete-repos 2>/dev/null)"})
                _describe -t repositories 'repository' repo_list
            fi
            ;;
    esac
}

_git-sync() {
    _git_sync "$@"
}

_git_sync "$@"
"""
