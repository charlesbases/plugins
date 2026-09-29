---
name: executing-plans
description: Directly execute a user-approved complete engineering proposal within its file and validation scope. Use after proposal confirmation when its execution mode is direct.
---

# Executing Plans

## Hard Gate

- Start only after the user confirms the complete current proposal with direct execution specified. Follow the [single proposal workflow](../using-cortex/SKILL.md#workflow-state); do not request another design, plan, or mode approval.
- Review the confirmed plan against the current repository state before editing files.
- Before editing under a newly presented or revised behavior-changing plan, including runtime output or log changes, verify its approved call-chain form and separate file-by-file pseudocode. Do not reopen an unchanged older approved plan solely because it predates this output format; return to planning or design when an actual implementation decision remains unapproved.
- Keep changes within the confirmed design, plan, and ownership boundaries; preserve existing user changes and edit only approved files.
- Do not introduce unconfirmed behavior, data flow, contracts, abstractions, asynchronous work, caches, dependencies, tests, or validation.
- Do not run Git mutation commands unless explicitly requested.
- When the approved proposal includes a user-initiated testing scheme, complete its report and baseline tasks, then return to testing to reread and compare the approved content and execute its mapped tests without another confirmation. Resume remaining work after testing. Do not invoke testing without that explicit handoff.
- When a scope-preserving correction changes source after an active manual testing handoff has run, return to `cortex:testing` to reread the report and rerun affected test points before completion verification.

## Entry And Resume

- Recover the complete proposal revision, approving user instruction, file and validation scope, execution mode, remaining work, and active testing baseline before the first write or resumed validation.
- Without a complete proposal, use brainstorming and writing-plans to produce one; with an unapproved proposal, wait for its single confirmation. With approved SDD mode, return to that workflow without switching mode.
- Use task context or already authorized recovery records. Existing edits or an approval flag do not establish authority; ask only for genuinely missing context. Do not create unplanned recovery files.
- Preserve approval for unchanged scope. Changed decisions follow [Proposal Revision](../using-cortex/SKILL.md#proposal-revision), including the required evidence recheck.
- A delegated SDD implementer returns to its controller rather than obtaining independent write authority here.

## Execution Workflow

For each confirmed plan step:

1. Use `cortex:code-tracing` when behavior, call chains, state, configuration, or ownership are not proven.
2. Use `cortex:code-standards` before generating or editing code.
3. Implement only the current plan step.
4. Do not investigate, modify, or report unrelated findings. Surface one `Out-of-scope risk` only when evidence shows a security risk, data integrity or data loss risk, or a current validation blocker; state its impact and take no scope-expanding action.
5. For changed Go files, select exactly one formatter with `command -v gofumpt`: when found, run `gofumpt -w <changed-go-files>`; only when not found, silently run `gofmt -w <changed-go-files>`. Do not run both in sequence or switch tools when the selected formatter fails; report that failure.
6. For changed non-Go files, run a formatter only when the confirmed plan or repository configuration defines its command; otherwise silently skip formatting.
7. Use `cortex:code-standards` for the targeted after-generation review.
8. Re-read changed context and the relevant call chain after behavior-affecting changes.
9. For a behavior-changing step, compare the implemented end-to-end path with every approved chain sketch and pseudocode available in the plan; newly presented or revised plans require both. For a changed observable outcome, value, or state meaning, compare the actual behavior or expression and lifecycle point with the approved mapping. Local code details may differ, but material changes to planned file operations, caller-to-callee links, key branches, state changes, outputs, or persistence and external boundaries require the Stop And Replan handoff.
10. After code changes, recheck the actual changed methods and candidate test assertions under [Unit Test Selection](../writing-plans/SKILL.md#unit-test-selection). Run only the mapped independently selectable cases within confirmed scope, or report the skip/insufficient-evidence reason; do not broaden to a file or package or add unplanned tests. Run other validation only within its confirmed scope.

## Stop And Replan

- Use systematic-debugging when required validation fails and its root cause is unproven. A proven correction that preserves approved key decisions, file scope, validation, and testing semantics resumes this workflow without another confirmation.
- Before an unplanned change to target or files, key calls, branches, state, output meaning, contracts or external boundaries, validation, or mode, pause affected work and apply using-cortex's Proposal Revision rule.
- Use brainstorming to reread affected context and recheck the necessary chain, feasibility, and semantic mapping, then writing-plans to present one revised complete proposal for approval.
- Do not work around blockers or validation failures by guessing or silently changing approved decisions.

## Completion

- Use `cortex:verification-before-completion` after all confirmed steps and validations complete, including required revalidation of any active manual testing handoff.
- Use `cortex:requesting-code-review` when review is required.

## Scope

Execute confirmed plans. Keep source analysis in `cortex:code-tracing`, code-quality rules in `cortex:code-standards`, and completion evidence in `cortex:verification-before-completion`.
