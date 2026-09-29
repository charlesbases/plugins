---
name: systematic-debugging
description: Diagnose observed failures and prove their root causes before proposing fixes. Use for bugs, test or build failures, regressions, runtime errors, flaky behavior, or unexpected output.
---

# Systematic Debugging

## Diagnosis Gate

- Treat diagnosis as read-only. Do not edit files, generate patches, add diagnostic instrumentation, create tests, or apply fixes.
- Limit executable commands, service interactions, and validation to user instructions, an approved complete proposal, or separately approved diagnostic scope.
- Before any command outside that scope, state its purpose, expected evidence, and side effects, then wait for user confirmation.
- When required validation fails under an approved complete proposal, including a testing handoff, treat necessary read-only repository analysis and local diagnostic commands within its changed and validation scope as approved diagnostic authority. Commands need not be individually listed when that scope covers them. This does not authorize external-service interaction, destructive actions, file writes during diagnosis, or expanded implementation or validation scope.
- Do not accept a root cause from correlation, stack location, a recent change, or a plausible explanation alone.
- Do not propose a fix or stack speculative changes before the root cause is confirmed.

## Investigation

1. Record observed behavior, expected behavior, environment, impact, reproduction steps, and available failure evidence.
2. Separate confirmed facts, inferences, and unknowns; ask for missing symptom, expectation, or boundary information when required.
3. Identify the candidate boundary where expected and actual behavior diverge.
4. Use `cortex:code-tracing` to prove or reject every repository-owned path needed for the root-cause conclusion.
5. Compare failing and proven working paths when a comparable repository example exists.
6. Identify the first source that violates the required invariant, contract, or external-input boundary.
7. State external or runtime boundaries that source cannot prove and the evidence needed to continue.

## Hypothesis Discipline

- State one specific hypothesis at a time with the evidence it predicts.
- Gather the smallest observation that proves or rejects that hypothesis.
- Record rejected hypotheses and continue investigation; do not infer a different cause from a failed speculative fix.
- Do not treat a nil-pointer panic as a request for a nil check. Trace why the value became nil and why the calling path allowed it.
- Add defensive validation only when a proven external-input boundary and confirmed contract require invalid-input handling.
- Correct an internal initialization or ownership invariant at its source instead of masking the symptom.

## Diagnosis Completion

- Before calling a cause confirmed, connect the observed symptom to the relevant entry, preconditions, state transition, and failure boundary. If runtime identity, configuration, or timing is not established, preserve that uncertainty even when source permits the scenario.
- A proposed correction must be reachable on the observed failure path and remove the proven cause. Correct code that runs only after an unproven transition is not a demonstrated fix for a failure before that transition.
- Check the diagnosis under the [batch evidence contract](../code-tracing/references/evidence-contract.md). Do not promote a plausible cause, a successful compilation, or a later apology into proof of the original diagnosis.

- Investigate every repository-owned path needed to prove or reject the failure.
- Reconcile symptom, reproduction, source evidence, state, configuration, and collected runtime evidence before output.
- Classify each candidate as a confirmed root cause, inferred cause, or unresolved cause.
- Return every independent confirmed root cause and unresolved boundary in one response.
- Add a later root-cause conclusion only when evidence or requirements changed, and identify that new evidence.
- Do not propose a fix for inferred or unresolved causes; state the evidence required to continue.


## Fix Handoff

- Return the proven diagnosis with caller role, originating workflow, complete proposal revision and approval context, execution mode, active testing baseline, affected validation, and handoff reason. Diagnosis remains read-only and creates no unplanned recovery artifacts.
- Missing implementation approval does not block read-only diagnosis. An SDD implementer returns the result and new decisions to its controller.
- For any validation failure, including ordinary validation outside testing, resume the approved execution workflow when the proven correction preserves its target and file scope, key chain and branches, state and output meanings, external contracts, validation, and test-point semantics. Do not reopen approval solely because a command failed.
- Reread the affected context and chain before recommending the correction; diagnosis must prove it reaches and removes the failure. For an active testing handoff, retain the approved baseline and return to testing to reread and rerun failed and affected points after correction.
- When no applicable implementation approval exists, or correction changes approved decisions, use brainstorming's Revision Evidence and writing-plans to present one complete fix proposal. A previous approval does not authorize a changed proposal.
- Follow the [single proposal revision rule](../using-cortex/SKILL.md#proposal-revision), never serial design and plan confirmations.
- Use verification-before-completion before claiming the issue fixed or passing.

## Output

- Start with the classification of every investigated candidate cause.
- State observed failure, expected behavior, reproduction status, and impact scope.
- Separate confirmed evidence, rejected hypotheses, inferences, and unknowns.
- Identify key source references and call-chain nodes without pasting large logs, source files, or command output.

## Scope

Diagnose failures and prove root causes. Keep source traversal in `cortex:code-tracing`, fix design in `cortex:brainstorming`, planning in `cortex:writing-plans`, execution workflows in `cortex:subagent-driven-development` or `cortex:executing-plans`, and completion evidence in `cortex:verification-before-completion`.
