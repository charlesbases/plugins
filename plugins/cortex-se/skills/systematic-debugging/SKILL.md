---
name: systematic-debugging
description: Diagnose observed failures and prove their root causes before proposing fixes. Use for bugs, test or build failures, regressions, runtime errors, flaky behavior, or unexpected output.
---

# Systematic Debugging

## Diagnosis Gate

- Treat diagnosis as read-only for the subject files. Do not edit them, generate implementation patches, add diagnostic instrumentation, create tests, or apply fixes. A controller may maintain its own [session snapshot](../using-cortex/references/task-context.md); delegated agents return state updates instead. An explicit user prohibition on writes still takes precedence.
- Limit executable commands, service interactions and validation to user instructions, the request-scoped plan or explicitly authorized diagnostic scope.
- Before any command outside that scope, state its purpose, expected evidence, and side effects, then wait for user confirmation.
- When required validation fails within an explicit implementation request, treat necessary read-only repository analysis and local diagnostic commands within its changed and validation scope as authorized diagnosis. Commands need not be individually listed when that scope covers them. This does not authorize external-service interaction, destructive actions, writes to subject files during diagnosis, or expanded implementation or validation scope.
- Do not accept a root cause from correlation, stack location, a recent change, or a plausible explanation alone.
- Do not propose a fix or stack speculative changes before the root cause is confirmed.

## Investigation

1. Record observed behavior, expected behavior, environment, impact, reproduction steps, and available failure evidence.
2. Recover existing requirements and evidence, then separate confirmed facts, inferences, and unknowns under [Evidence And Clarification](../using-cortex/SKILL.md#evidence-and-clarification). Trace source-resolvable questions; request necessary external or runtime information in parallel when source cannot establish it, stating the verified context, precise gap, and diagnostic impact.
3. Identify the candidate boundary where expected and actual behavior diverge.
4. Use `cortex-se:code-tracing` to prove or reject every repository-owned path needed for the root-cause conclusion.
5. Compare failing and proven working paths when a comparable repository example exists.
6. Identify the first source that violates the required invariant, contract, or external-input boundary.
7. State external or runtime boundaries that source cannot prove and the evidence needed to continue.

## Hypothesis Discipline

- State one specific hypothesis at a time with the evidence it predicts.
- Keep hypotheses separate from confirmed problem statements and fixes. An untested candidate is a direction for investigation, not proof of a defect or the necessity of a modification.
- Gather the smallest observation that proves or rejects that hypothesis.
- Record rejected hypotheses and continue investigation; do not infer a different cause from a failed speculative fix.
- Do not treat a missing-value failure as a request to add a guard. Trace how the value became missing and why the calling path allowed it.
- Add defensive validation only when a proven external-input boundary and confirmed contract require invalid-input handling.
- Correct an internal initialization or ownership invariant at its source instead of masking the symptom.

## Diagnosis Completion

- Before calling a cause confirmed, connect the observed symptom to the relevant entry, preconditions, state transition, and failure boundary. If runtime identity, configuration, or timing is not established, preserve that uncertainty even when source permits the scenario.
- Distinguish a proven local method effect from its runtime invocation and from the cause of the observed failure. A cleanup method or lifecycle declaration alone does not establish that the failing path reaches it; an unproven link remains an evidence gap throughout the fix handoff.
- A proposed correction must be reachable on the observed failure path and remove the proven cause. Correct code that runs only after an unproven transition is not a demonstrated fix for a failure before that transition.
- Check the diagnosis under the [batch evidence contract](../code-tracing/references/evidence-contract.md). Do not promote a plausible cause, a successful compilation, or a later apology into proof of the original diagnosis.

- Investigate every repository-owned path needed to prove or reject the failure.
- Reconcile symptom, reproduction, source evidence, state, configuration, and collected runtime evidence before output.
- Classify each candidate as a confirmed root cause, inferred cause, or unresolved cause.
- Return every independent confirmed root cause and unresolved boundary in one response.
- Add a later root-cause conclusion only when evidence or requirements changed, and identify that new evidence.
- Do not propose a fix for inferred or unresolved causes. Read available claim-relevant source before requesting more information; identify the specific missing path or unavailable external evidence required to continue.

## Fix Handoff

- Return the proven diagnosis with caller role, originating workflow, current plan and user-authorized scope, execution mode, affected validation and handoff reason. Diagnosis remains read-only and creates no unplanned recovery artifacts.
- A missing implementation request does not block read-only diagnosis. An SDD implementer returns the result and new decisions to its controller.
- For any validation failure, resume the selected execution workflow when the proven correction preserves the requested target and file scope, key chain and branches, state and output meanings, external contracts and validation. Do not add confirmation solely because a command failed.
- Reread the affected context and chain before recommending the correction; diagnosis must prove it reaches and removes the failure. After correction, rerun the failed and affected checks within the authorized validation scope.
- When no implementation request exists, deliver the diagnosis and proposed correction without implementing it. For a requested fix that changes established decisions, use brainstorming's Revision Evidence and writing-plans to update the complete plan. Resolve a material unanswered user choice before the dependent edit.
- Follow [Proposal Revision](../using-cortex/SKILL.md#proposal-revision), without separate design, plan or execution-mode confirmations.
- Use verification-before-completion before claiming the issue fixed or passing.

## Output

- Start with the classification of every investigated candidate cause.
- State observed failure, expected behavior, reproduction status, and impact scope.
- Separate confirmed evidence, rejected hypotheses, inferences, and unknowns.
- Identify key source references and call-chain nodes without pasting large logs, source files, or command output.

## Scope

Diagnose failures and prove root causes. Keep source traversal in `cortex-se:code-tracing`, fix design in `cortex-se:brainstorming`, planning in `cortex-se:writing-plans`, execution workflows in `cortex-se:subagent-driven-development` or `cortex-se:executing-plans`, and completion evidence in `cortex-se:verification-before-completion`.
