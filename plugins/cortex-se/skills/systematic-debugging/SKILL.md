---
name: systematic-debugging
description: Diagnose observed software failures from reproduction and complete relevant source paths, then correct proven causes within scope.
---

# Systematic Debugging

## Diagnose

1. Establish expected and observed behavior, environment, reproduction status, impact, and available logs or results.
2. State a specific hypothesis and the observation that would prove or reject it. Use the smallest relevant local diagnostic check; do not infer a cause from correlation, stack location, or a recent change alone.
3. Use `cortex-se:code-tracing` to follow the complete relevant entry, preconditions, state and configuration changes, and failure boundary. Check source claims with its [evidence contract](../code-tracing/references/evidence-contract.md).
4. Compare a known working path when it helps isolate the first divergence. Distinguish a confirmed root cause from an inferred or unresolved one.

Read-only local diagnosis within the requested task needs no separate confirmation. Temporary instrumentation may be used when necessary for an authorized fix, but remove it before completion unless it is part of the requested result. External interactions and irreversible diagnostics require their own authority.

## Fix And Recheck

- Correct the proven cause at the responsible boundary; do not merely mask its downstream symptom. A proposed correction must be reachable on the observed failure path.
- When the correction preserves the requested outcome and scope, create or update its how-to plan through `cortex-se:writing-plans`, then continue through `cortex-se:executing-plans` or the user-requested delegated workflow without renewed confirmation.
- If fixing the cause requires a new observable behavior, contract, ownership, or scope decision, present that decision once through `cortex-se:brainstorming` before the affected edit.
- Reproduce or rerun the targeted failing check after the final change when feasible. Report unproven runtime identity, timing, external state, and blocked validation explicitly. Use `cortex-se:verification-before-completion` before claiming the issue is fixed.
