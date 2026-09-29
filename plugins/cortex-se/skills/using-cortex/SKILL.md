---
name: using-cortex
description: Route software development work through Cortex SE while preserving source accuracy and avoiding repeated confirmation gates.
---

# Using Cortex SE

## Route The Request

- For an existing-behavior question or a change that depends on current behavior, use `cortex-se:code-tracing`.
- For a request with an unresolved behavior, contract, scope, or ownership choice, use `cortex-se:brainstorming` to obtain that decision. A clear implementation request authorizes work within its stated scope; it does not require a separate design, plan, or execution-mode confirmation.
- For a behavior-changing implementation, use `cortex-se:writing-plans` to establish how the change will work, then `cortex-se:executing-plans`. For a small change with no behavior path to design, keep the implementation approach proportionate.
- Apply `cortex-se:code-standards` to generated or edited code. Use `cortex-se:systematic-debugging` for an observed failure with an unproven cause.
- Use `cortex-se:requesting-code-review` when review is requested or needed by the selected workflow. Use `cortex-se:verification-before-completion` before claiming completion, correctness, or a passing check.
- Use `cortex-se:subagent-driven-development` only when the user requests delegated implementation. Direct execution is the default; do not ask the user to select an execution mode.
- If the user requests analysis, design, or a plan only, deliver that result without implementing it.

## Source And Scope

- Read applicable project instructions, relevant code, configuration, existing tests, and current changes before making a source-dependent decision. For behavior-changing work, trace the complete relevant repository-owned path from entry and dispatch through key branches and state changes to the observable or error boundary. Include additional entry paths when the requested outcome depends on them. Do not treat a search hit or available API as proof of runtime reachability.
- Use the [evidence contract](../code-tracing/references/evidence-contract.md) for repository conclusions and change bases. Evidence checkpoints establish structural source coverage, not semantic correctness or user authorization. Continue across bounded read batches until the relevant path is established; a read budget is not a reason to truncate the conclusion.
- Keep implementation and validation within the user's requested scope and applicable project policy. If a material choice changes the requested outcome, contract, ownership, or scope, present the decision once and wait for the answer. After the answer, continue without a second plan approval. Scope-preserving implementation or root-cause corrections do not reopen confirmation.
- When the user objects, supplies a new direction, or a design narrows or changes meaning, pause affected work and use brainstorming's [Revision Evidence](../brainstorming/SKILL.md#revision-evidence) before evaluating or answering the challenged decision, revising a plan, or continuing implementation. This also applies when the answer recommends keeping the existing design. Rechecking evidence does not introduce a generic approval gate; retain this skill's existing clear-request and material-decision rules.
- Do not infer permission for unrelated changes, irreversible operations, or external effects from a development request.

## Completion

- Select effective, targeted validation under `cortex-se:verification-before-completion`; do not add tests merely to meet a count or coverage target.
- Report what changed, what was checked, the result, and any remaining evidence gap. Never claim a check passed when it was not run or did not pass.
