# INVARIANTS.md — System Invariants & Interface Verification Guardrails (git-sync)

> 🧱 **Chesterton's Fence Principle:**
> *"Never remove a rule, filter, or unusual piece of code until you understand why it was put there."*

---

## 3. Multi-Agent & Worktree Isolation Invariants

- **Disjoint Micro-Contracts (`[PARALLEL: Wx]`):** Subagents operating in parallel waves (`[PARALLEL: Wx]`) must operate on mutually disjoint mutable paths (`Paths:`).
- **Sequential Sub-Task Batching (`[BATCH: Bx]`):** Sequential atomic subtasks (`ZZ`) within the same feature (`XX.YY`) may be grouped into batches of **2 to 4 subtasks** ONLY when they belong to the same architectural layer (`Type:`). Mixing Backend (`Type: code`) with Frontend/UI (`Type: browser-qa` / `mobile-qa` / `preview-mockup`) in a single batch is prohibited. Shared feature contracts live in untracked `.agent/scratch/specs/XX-YY.md` and are copied into isolated worktrees by `bootstrap-worktree.sh`.
- **Fast-Link vs. Isolated-Install:** Worktree provisioning must use Fast-Link (symlink `node_modules/` or `.venv/`) when manifest files are unaltered, and Isolated-Install when dependencies change.

---

## 7. Adaptive Interface Verification & UI Protocol

- **Invariant 7.1 (Runtime-Aware Interface Verification):**
  - **Inside T3 Code:** When undertaking frontend, preview, or UI component tasks in a T3 Code environment, agents must verify visual and interactive behaviors using preview and device MCP tools (`preview_status`, `preview_open`, `preview_snapshot`, `preview_act`, `device_open`). Agents must check `preview_status` before opening tabs and prefer accessibility/snapshot locators over coordinates.
  - **Outside T3 Code (Headless CLI / CI):** In headless terminal runtimes or CI pipelines, agents must verify interfaces through non-blocking build, component, and E2E test suites (`npm run build`, `vitest`, `playwright --headless`), without hanging on graphical display servers.
- **Invariant 7.2 (Prohibition of Blind Preview Launches):**
  - Agents must NEVER invoke `preview_open` or launch mobile emulators to retrieve static web documentation or perform general search queries. Static web research belongs exclusively to headless HTTP tools (`read_url_content`, web search).
- **Invariant 7.3 (Mandatory Preview Lifecycle Cleanup):**
  - Any preview browser tab or device emulator session opened by an agent during execution MUST be explicitly closed (`preview_close`, `device_close`) before handing back control to the human or completing the task.
- **Invariant 7.4 (Non-Blocking Server Containment):**
  - Local development or preview servers started for interface testing must be run as manageable background tasks on ephemeral ports, and terminated immediately once verification concludes.
