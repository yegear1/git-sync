---
id: "k-000"
title: "[Short Imperative Title of Gotcha or Bug Fix]"
date: YYYY-MM-DD
tags:
  - "category"
  - "library-or-tool"
severity: "medium" # low | medium | high | critical
affected_components:
  - "path/to/affected/file-or-service"
symptoms:
  - "Exact error message snippet or exit code"
  - "Observable behavioral symptom"
---

# [k-000] [Short Imperative Title of Gotcha or Bug Fix]

## Problem / Symptom
[Describe the precise symptom, context, reproduction conditions, and paste exact error logs or stack traces.]

```text
[Paste error log / stack trace here]
```

---

## Approved Solution
[Explain the verified fix. Provide the exact code modification, configuration delta, or command.]

```bash
# Example fix command or configuration snippet
```

**Root Cause:** [1-3 sentences explaining why this failure occurred at the system or library level.]

---

## What NEVER to Do
- ❌ **Anti-Pattern:** [Describe naive workaround, e.g., disabling security checks, adding unconstrained retries, or arbitrary sleeps.]
  - *Why:* [Explanation of why this leads to regressions, security risks, or instability.]
- ❌ **Anti-Pattern:** [Another bad pattern to avoid.]
  - *Why:* [Consequence.]

---

## Verification
```bash
# Command to verify the fix works deterministically
```

---

## References
- [Issue / PR / Upstream documentation link]
