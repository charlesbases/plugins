---
name: executing-plans
description: Directly execute a clear implementation request using its complete plan and authorized file and validation scope, without a separate plan approval.
---

# Executing Plans

## Hard Gate

- Start when an explicit implementation request and complete current plan establish the target, file and validation scope. Follow the [request-scoped workflow](../using-cortex/SKILL.md#workflow-state); do not request separate design, plan or mode approval.
- Review the current plan against the current repository state before editing files.
- Before editing under a newly presented or revised behavior-changing plan, including runtime output or log changes, verify its planned call-chain form and separate file-by-file pseudocode. Do not reopen an unchanged older current plan solely because it predates this output format; return to planning or design when an actual implementation decision remains unapproved.
- Keep changes within the confirmed design, plan, and ownership boundaries; preserve existing user changes and edit only authorized files.
- Do not introduce unconfirmed behavior, data flow, contracts, abstractions, asynchronous work, caches, dependencies, tests, or validation.
- Do not run Git mutation commands unless explicitly requested.

## Entry And Resume

- Read [Current Task Context](../using-cortex/references/task-context.md) and the controller's saved requirements before starting or resuming implementation, including after compaction. Reconcile them with current files and actual authorization. Save only clarified user requirement or constraint changes in context. Keep progress, pending cleanup and questions in the conversation or existing authorized records. Complete authorized removals of superseded artifacts without discarding shared or independently owned work.

- Recover the complete plan revision, actual implementation request, resolved material decisions, file and validation scope, execution mode and remaining work before the first write or resumed validation.
- Without a complete plan, use brainstorming and writing-plans to produce one. Without an implementation request, deliver the requested analysis or plan without executing it. With user-requested SDD mode, return to that workflow without silently switching mode.
- Use context for user requirements and constraints; recover the proposal and its actual user authority from the conversation or existing authorized records, and check progress against actual files. Existing edits or an approval flag do not establish authority; ask only for genuinely missing information. Do not create unplanned recovery files.
- Preserve applicable user authority for unchanged scope. Changed decisions follow [Proposal Revision](../using-cortex/SKILL.md#proposal-revision), including the required evidence recheck.
- A delegated SDD implementer returns to its controller rather than obtaining independent write authority here.

## Execution Workflow

Mid-turn user steering must pass the [Current Task Context](../using-cortex/references/task-context.md) reconciliation procedure before the next affected action. Clarify conflicting requirements in the conversation before updating affected context content. Verify that the latest clarified requirements and constraints are saved before controllable compaction; do not save progress or next steps there. After recovery, load those requirements and separately recover actual user authority before continuing. Material revisions follow the existing request and material-decision boundary.

For each current plan step:

1. Use `cortex-se:code-tracing` when behavior, call chains, state, configuration, or ownership are not proven.
2. Use `cortex-se:code-standards` before generating or editing code.
3. Implement only the current plan step.
4. Do not investigate, modify, or report unrelated findings. Surface one `Out-of-scope risk` only when evidence shows a security risk, data integrity or data loss risk, or a current validation blocker; state its impact and take no scope-expanding action.

5. Run a formatter only when the current plan or repository configuration defines its command; otherwise silently skip formatting.
6. Use `cortex-se:code-standards` for the targeted after-generation review.
7. Re-read changed context and the relevant call chain after behavior-affecting changes.
8. For a behavior-changing step, compare the implemented end-to-end path with every planned chain sketch and pseudocode available in the plan; newly presented or revised plans require both. For a changed observable outcome, value, or state meaning, compare the actual behavior or expression and lifecycle point with the planned mapping. Local code details may differ, but material changes to planned file operations, caller-to-callee links, key branches, state changes, outputs, or persistence and external boundaries require the Stop And Replan handoff.
9. After code changes, recheck the actual changed methods and candidate test assertions under [Unit Test Selection](../writing-plans/SKILL.md#unit-test-selection). Run only the mapped independently selectable cases within confirmed scope, or report the skip/insufficient-evidence reason; do not broaden to a file or package or add unplanned tests. Run other validation only within its confirmed scope.

## Stop And Replan

- Use systematic-debugging when required validation fails and its root cause is unproven. A proven correction that preserves established key decisions, file scope, validation, and testing semantics resumes this workflow without another confirmation.
- Before an unplanned change to target or files, key calls, branches, state, output meaning, contracts or external boundaries, validation, or mode, pause affected work and apply using-cortex's Proposal Revision rule.
- Use brainstorming to reread the affected context and recheck the necessary chain, feasibility and semantic mapping, then writing-plans to present one revised complete plan. Resolve only material unanswered user choices before the dependent edit; do not add a revised-plan approval.
- Do not work around blockers or validation failures by guessing or silently changing established decisions.

## Completion

- Use `cortex-se:verification-before-completion` after all scoped steps and validations complete, including revalidation after corrections.
- Use `cortex-se:requesting-code-review` when review is required.

## Scope

Execute current plans. Keep source analysis in `cortex-se:code-tracing`, code-quality rules in `cortex-se:code-standards`, and completion evidence in `cortex-se:verification-before-completion`.
