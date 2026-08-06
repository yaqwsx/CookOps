# CookOps Agent Workflow

Discuss work with the user in Czech. Write source code, tests, commit messages,
and specification text in English.

Treat `design/` and unrelated working-tree changes as user-owned. Do not stage,
rewrite, or delete them.

## Slice workflow

Keep each implementation slice independently testable and small enough for one
meaningful commit:

1. Read the relevant specification and trace the touched flow end to end.
2. Apply Ponytail `full`: stop at the first valid rung—YAGNI, existing code,
   standard library, native platform, installed dependency, one line, then minimum
   custom code. Do not simplify away security, trust-boundary validation,
   accessibility, data-loss protection, tests, or explicit requirements.
3. Implement one vertical behavior and its smallest useful unit, integration,
   property/fuzz, or Playwright check.
4. Ask an agent that did not implement the slice for a correctness, security,
   performance, and specification review.
5. Ask a separate review pass to apply Ponytail review only: `delete`, `stdlib`,
   `native`, `yagni`, and `shrink` findings.
6. Resolve findings, rerun the relevant checks, and commit only that slice.

Subagents do not commit unless the primary agent explicitly delegates the commit.
