---
name: executing-plans
description: Implement a scoped software change using a source-grounded how-to plan and targeted validation, without repeated approval gates.
---

# Executing Plans

## Entry And Scope

- An explicit user implementation request authorizes its stated scope. Begin after establishing relevant context, a complete relevant call chain for behavior changes, and the how-to plan required by `cortex-se:writing-plans`. Do not require separate design, plan, or execution-mode approvals for a clear request.
- Resolve a material unanswered outcome, contract, ownership, or scope decision through `cortex-se:brainstorming` before the affected implementation. Once answered, continue without another plan confirmation.
- For a revised direction, verify the plan includes brainstorming's revision evidence and current outcome-to-value/state/expression mapping before affected writes. Return missing proof to that workflow without adding a generic approval stage.
- Recheck current project state and preserve existing user changes. Edit only files required by the requested outcome or a proven prerequisite. Do not run Git mutation commands unless the user requests them.
- Before the first behavior-changing write, verify that the current plan contains both the required call-chain representation and separate file-by-file pseudocode. If either is absent, complete it through `cortex-se:writing-plans` before editing; do not ask for a new plan approval.

## Implementation

1. Use `cortex-se:code-tracing` to close any relevant source-evidence gap. For a repository-based proposal or write, use the [evidence contract](../code-tracing/references/evidence-contract.md).
2. Apply `cortex-se:code-standards` while editing. Implement the file-level logic and call-chain changes described by the plan.
3. Use formatters and static checks defined by project configuration or established repository practice. Do not impose a language-specific command that the project does not use.
4. Re-read changed code and the affected end-to-end path. Compare entry, dispatch, key branches, state changes, output and errors with the plan and requested outcome; recheck actual value/state definitions, expressions and lifecycle points against the current semantic mapping.
5. Recheck changed methods and candidate assertions under [Unit Test Selection](../writing-plans/SKILL.md#unit-test-selection), run only mapped selectable cases or record the skip reason, then assess all scoped validation through verification-before-completion. Do not add unplanned tests or broaden the unit scope.

Scope-preserving implementation details and corrections may update the plan and proceed. If a finding changes the requested observable behavior, contract, ownership, or scope, present that decision once before continuing. Use `cortex-se:systematic-debugging` when validation fails and its cause is unknown. Do not hide failures by weakening expected results or adding unrelated tests.

## Completion

Use `cortex-se:verification-before-completion` after implementation and validation. Use `cortex-se:requesting-code-review` when the user requests a review or the selected workflow requires it.
