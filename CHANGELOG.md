# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-11

### Added
- Concurrent multi-repository status auditor and synchronization engine with adaptive concurrency.
- Agent-first CLI defaults emitting token-dense compact JSON summaries (`--json --compact`) by default, with `--human` table formatting and `--full` item payloads.
- Fast-forward safety invariants (`--ff-only`) and worktree-family serialized synchronization.
- Worktree lifecycle management including stale branch pruning, detection, and isolation.
- Git submodule drift detection with optional recursive updates and shallow fetch deepening.
- Workspace health diagnostics (`git-sync doctor`) auditing SSH multiplexing, credentials, and remotes.
- Interactive terminal dashboard TUI with keyboard navigation, live diff inspection, and profile switching.
- Positional repository target filtering across `status`, `dirty`, `pull`, and `push` subcommands.
- Native Git subcommand integration (`git sync`) with complete Bash and Zsh auto-completion.
