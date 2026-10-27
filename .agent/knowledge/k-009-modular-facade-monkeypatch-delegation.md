---
id: "k-009"
title: "Preserve Unittest Mock Patch Compatibility in Modular Facade Extractions"
date: 2026-10-26
tags:
  - "python"
  - "testing"
  - "refactoring"
  - "unittest-mock"
severity: "high"
affected_components:
  - "src/git_sync/core.py"
  - "src/git_sync/engine/git.py"
symptoms:
  - "Unit tests patching facade symbols (e.g. @patch('git_sync.core.run_git')) fail to intercept internal calls within extracted submodules"
  - "Tests execute real subprocess calls or report mock call_count == 0"
---

# [k-009] Preserve Unittest Mock Patch Compatibility in Modular Facade Extractions

## Problem / Symptom
When deconstructing a monolithic module (`git_sync.core`) into modular submodules (`git_sync.engine.git`) while keeping the legacy module as a re-exporting facade, existing test suites that use `unittest.mock.patch("git_sync.core.run_git")` fail because intra-module calls inside the submodules call the submodule's local function reference directly instead of resolving through the monkeypatched facade attribute in `sys.modules["git_sync.core"]`.

```text
AssertionError: Expected 'run_git' to have been called once. Called 0 times.
```

---

## Approved Solution
In extracted domain submodules that call sibling functions frequently mocked in existing test harnesses (e.g. `run_git`, `discover_repos`, `get_repo_remotes`), route internal dispatches through a dynamic lookup helper that checks if the symbol has been patched on the legacy facade module:

```python
def _dispatch_fn(name: str, fallback: Any) -> Any:
    core = sys.modules.get("git_sync.core")
    if core is not None:
        target = getattr(core, name, None)
        if target is not None and target is not fallback:
            return target
    return fallback

def _run_git(repo_path: str, args: List[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    core = sys.modules.get("git_sync.core")
    if core is not None:
        target = getattr(core, "run_git", None)
        if target is not None and target is not run_git:
            return target(repo_path, args, timeout=timeout)
    return _real_run_git(repo_path, args, timeout=timeout)
```

**Root Cause:** `unittest.mock.patch("package.module.func")` updates the dictionary of `sys.modules["package.module"]`. If the implementation is moved to `package.submodule` and re-exported, the submodule's internal bytecode references its own global namespace dictionary, completely bypassing changes to the re-exporting module dictionary.

---

## What NEVER to Do
- ❌ **Anti-Pattern:** Break backward compatibility by altering all existing test files to patch the new submodule paths instead of supporting legacy facade patches.
  - *Why:* Breaks external client libraries, plugins, or third-party tests depending on established public contracts.
- ❌ **Anti-Pattern:** Create circular imports between the facade and submodules.
  - *Why:* Causes runtime `ImportError` or partial module initialization bugs.

---

## Verification
```bash
python3 -m unittest discover -s tests
```
All unit tests patching `git_sync.core.*` pass seamlessly.
