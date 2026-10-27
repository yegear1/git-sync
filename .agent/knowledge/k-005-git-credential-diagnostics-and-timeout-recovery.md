---
id: "k-005"
title: "Automated Git Credential Sanity Diagnostics and Transient Network Timeout Recovery"
date: 2026-10-21
tags:
  - "git"
  - "credentials"
  - "ssh"
  - "http-auth"
  - "network-timeout"
  - "exponential-backoff"
severity: "medium"
affected_components:
  - "src/git_sync/core.py"
  - "src/git_sync/cli.py"
  - "src/git_sync/config.py"
symptoms:
  - "Permission denied (publickey) hanging or failing bulk status/pull operations"
  - "terminal prompts disabled when HTTP credentials or tokens are missing"
  - "Transient connection timeouts and DNS glitches causing false-positive fetch errors"
  - "Unclear git errors preventing automated agents from determining actionable resolution steps"
---

# [k-005] Automated Git Credential Sanity Diagnostics and Transient Network Timeout Recovery

## Problem / Symptom

1. **Non-Interactive Background Process Hanging:** When Git operations require credentials or SSH passphrases in headless agent environments, Git defaults to interactive terminal prompts, locking background processes indefinitely if not strictly guarded.
2. **Obscure Authentication Failures:** When `GIT_TERMINAL_PROMPT=0` is enforced, Git fails immediately with cryptic messages like `fatal: could not read Username: terminal prompts disabled` or `Permission denied (publickey)`, leaving users and AI agents without actionable guidance on whether an SSH agent is stopped, keys are missing, or HTTP tokens are expired.
3. **Transient Network Flakiness:** In large workspaces with dozens of repositories, transient network errors (packet loss, momentary DNS hiccups, curl 56/28 errors, server rate-limiting delays) cause individual repository fetches to fail, leading to false-positive audit errors.
4. **Retry Cascades on Permanent Failures:** Retrying authentication failures or non-existent repository errors wastes execution time, risks remote provider rate-limiting, and delays reporting.

---

## Approved Solution

1. **Deterministic Error Classification (`diagnose_git_error`):**
   - Parse stderr from failed Git commands into structured diagnostic categories (`SSH_PERMISSION_DENIED`, `SSH_NO_AGENT`, `SSH_HOST_UNVERIFIED`, `HTTP_AUTH_REQUIRED`, `HTTP_AUTH_FAILED`, `NETWORK_DNS_FAILURE`, `NETWORK_TIMEOUT`, `REMOTE_NOT_FOUND`).
   - Associate each category with a human-actionable suggestion (e.g. running `ssh-add ~/.ssh/id_ed25519` or configuring `git config --global credential.helper store`).
   - Attach diagnostic payloads to repo inspection items and command results for both terminal display and agent JSON consumption.
2. **Selective Transient Network Retries (`run_git_with_retry`):**
   - Differentiate transient network glitches (`subprocess.TimeoutExpired`, connection timeouts, connection resets, EOF, curl 56) from permanent errors (authentication rejections, repository not found).
   - Apply bounded exponential backoff with configurable retries (default 2), base delay (0.5s), and maximum cap (4.0s).
   - Fast-fail permanent failures immediately without entering retry loops.
3. **Proactive Pre-flight Credential Sanity Probing (`check_credentials_sanity` / `git-sync doctor`):**
   - Probe local SSH agent status via non-blocking `ssh-add -l` (detecting `ACTIVE`, `NO_IDENTITIES`, or `NO_AGENT`).
   - Audit available private key files in `~/.ssh/` and configured Git credential helpers.
   - Probe unique remote hosts across repositories using `git ls-remote --heads` with `GIT_TERMINAL_PROMPT=0` to verify remote access without mutating repo state or hanging.
4. **CLI Integration (`git-sync doctor` & `--preflight`):**
   - Expose `git-sync doctor` subcommand to print a comprehensive diagnostic badge summary or machine-readable JSON.
   - Support `--preflight` on `status`, `pull`, and `push` to catch credential or network issues early before kicking off concurrent operations.

---

## What NEVER to Do

- **NEVER** retry permanent authentication failures (`Permission denied`, `Authentication failed`, `terminal prompts disabled`). Doing so pointlessly delays command execution and may trigger IP blocks or account lockout.
- **NEVER** probe remotes without `GIT_TERMINAL_PROMPT=0` and command timeouts. Any SSH prompt or credential dialog in a headless subprocess will block indefinitely.
- **NEVER** discard the underlying Git stderr when surfacing diagnostics; always retain the raw error alongside categorized advice for debugging edge cases.
- **NEVER** probe duplicate remote URLs in bulk workspaces; always deduplicate unique remotes or group by host to minimize network overhead during pre-flight checks.
