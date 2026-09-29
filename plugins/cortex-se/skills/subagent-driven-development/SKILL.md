---
name: subagent-driven-development
description: Coordinate user-requested delegated implementation of independent software tasks with scoped briefs and integrated review.
---

# Subagent-Driven Development

## Entry

- Use this skill only when the user requests delegated implementation and the work has at least two tasks with separable writes and decisions. Otherwise use `cortex-se:executing-plans` without asking the user to choose a mode.
- Establish the requested outcome, applicable project context, complete relevant call chain, and the how-to plan from `cortex-se:writing-plans` before dispatch. A clear implementation request needs no separate design or plan confirmation.
- Before dispatch, verify that the plan includes both the required call-chain representation and separate file-by-file pseudocode. Complete missing plan content without another user confirmation, and include the relevant segments in each task brief.
- Do not delegate a material unresolved behavior, contract, ownership, or scope decision. Ask for it once through `cortex-se:brainstorming`, then continue with the answer.
- Confirm that subagent tools can create workers in the current runtime. If dispatch fails for a stable runtime limitation, report it and do not claim delegated execution or repeatedly retry the same mechanism. Direct execution is a fallback only when compatible with the user's instruction.

## Delegate

1. Give each implementer a bounded task brief: required outcome, files and interfaces it owns, relevant call-chain segment, separate file-level pseudocode, outcome/value/state/lifecycle mapping, dependencies and constraints. For unit tests, include the method-to-case mapping and exact selectors or skip reasons from Unit Test Selection.
2. Keep overlapping writes and shared decisions with the controller. Run one implementation worker at a time in a shared workspace, including tasks with different files: concurrent writes can invalidate a shared evidence checkpoint. Parallel implementation requires isolated workspaces and independent evidence state. Preserve existing user changes.
3. Require the implementer to use `cortex-se:code-tracing` for missing evidence and `cortex-se:code-standards` for edited code. Use the worker's own hook context when supplied, not a fabricated or reused controller session ID. Refresh source receipts invalidated by the previous worker before another scoped write. The implementer reports changed files, source assumptions, validation commands and results, and unresolved concerns.
4. The controller checks each result against its brief, actual diff, call chain and semantic mapping. A changed or challenged direction requires brainstorming's Revision Evidence before a new brief; the controller owns any genuinely unresolved material decision.
   Resolve material deviations or failures before accepting the result. Use a scoped read-only `cortex-se:requesting-code-review` review when risk or an unresolved concern warrants it.
5. After integration, review the whole changed path and run targeted validation through `cortex-se:verification-before-completion`. A passing isolated task does not establish the integrated behavior.

Use a persistent progress record only when the task is long enough to need recovery, and keep it outside project source unless requested. Do not create per-task reports merely to satisfy a workflow count. No delegated worker may expand the user's requested scope or make a new design decision on the controller's behalf.
