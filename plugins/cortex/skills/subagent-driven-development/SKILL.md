---
name: subagent-driven-development
description: Coordinate confirmed implementation plans with two or more independent file-writing tasks using isolated implementer and reviewer subagents. Use after the user selects subagent-driven execution.
---

# Subagent-Driven Development

## Hard Gate

- Start only after the user confirms the complete current proposal specifying user-requested SDD execution. Follow the [single proposal workflow](../using-cortex/SKILL.md#workflow-state); do not request independent design, plan, or mode approval.
- Use only when the plan has at least two tasks whose writes, mutable state, and required decisions do not overlap. A later task may depend only on an interface or fact established by an earlier completed task.
- Stop when tasks become tightly coupled or require overlapping writes or decisions. Present revised boundaries or a changed execution mode through the complete-proposal revision workflow before dispatching further work; do not switch to direct execution silently.
- Keep the user-approved workspace and do not run Git mutation commands unless explicitly requested.
- Do not allow a subagent to make a new design, change the confirmed plan, or expand validation scope. Return to `cortex:brainstorming` or `cortex:writing-plans` through the controller when required.

## Entry And Return

- Recover the approved complete proposal, its approving instruction, SDD mode, scoped task authority, and handoff reason. Preserve approval for unchanged scope; use the single proposal gate when complete content or approval is absent.
- Missing recovery artifact paths or their creation tasks require a revised complete proposal before creating them.
- Before dispatching a newly proposed or revised behavior-changing task, require its approved code-level chain segment and separate file-by-file pseudocode. Do not reopen an unchanged older approved proposal solely for format.
- Changed design or task decisions follow using-cortex's Proposal Revision and brainstorming's Revision Evidence. A controller brief authorizes only its approved task scope; implementers return decisions rather than requesting user approval.

## Persistent Task Record

1. After the complete proposal specifying SDD is confirmed, the controller creates the planned `.temp/cortex/sdd/<plan-id>/` records. Use one stable `<plan-id>` throughout the work; record creation must already be covered by the confirmed plan.
2. The controller creates `progress.md` containing:
   - the current phase, execution mode, handoff reason, and any pending proposal revision;
   - the current complete proposal revision, its approved requirements, context, design, chain, changes, validation, and scope, and the actual approving user instruction;
   - the exact requirements, file boundaries, constraints, dependencies, and approved validation for every remaining task, plus task order and status;
   - review status, unresolved findings, validation evidence, and any active testing handoff's stage, approved-scheme revision, and readable baseline reference.
   Keep recovery content inline or in readable local snapshots whose writes the plan authorizes. A reference to unavailable conversation content, a revision label, or an `approved` flag alone is insufficient.
3. For every task, the controller is the sole owner of creating `task-<n>-brief.md`, `task-<n>-report.md`, and `task-<n>-review.md` in that directory, and creates all three before dispatch. A reviewer may neither create nor write these controller-owned artifacts.
4. After context compaction, read `progress.md`, the referenced task artifacts and approval snapshots, and repository evidence before resuming. Reconstruct the current approved scope, mode, remaining tasks, and testing baseline from those records. Do not rely on conversation memory or redispatch a task marked complete.
5. If required approved content or its confirmation cannot be recovered, ask only for the missing facts or confirmation before further writes or tests. Do not infer authority from existing edits or overwrite an old approved snapshot with a new proposal.
6. Update progress at handoffs. Preserve approved content and its testing baseline while a revised complete proposal awaits its single confirmation; never mark a pending revision executable.

## Task Dispatch

1. Run one task at a time. Do not run implementation subagents concurrently in the same workspace.
2. Dispatch a fresh implementer subagent with only:
   - the task brief path as the source of exact requirements;
   - the task's project role and directly relevant interfaces or completed-task facts;
   - binding constraints from the confirmed plan, including the plan ID and approved revision, the implementer role, and the controller's authorization for this task's exact writes and validation;
   - the report path and return contract.
3. Keep exact values, file lists, acceptance criteria, and validation requirements in every brief. For unit tests, include the changed-method-to-case mapping, exact selectors, or skip reasons from [Unit Test Selection](../writing-plans/SKILL.md#unit-test-selection). For a behavior-changing task with approved call chain and pseudocode, include only its relevant segments and any applicable outcome-to-behavior or value mapping. Do not require these materials for tasks without behavior changes. Do not provide the controller's history, prior-task summaries, full plan, full diff, or unbounded repository context.
4. Authorize the implementer to execute only its task brief under this skill's confirmed design, plan, and validation scope. Require `cortex:code-tracing` when source evidence is missing and `cortex:code-standards` before code generation. Do not invoke `cortex:executing-plans` or `cortex:verification-before-completion` from an implementer task.
5. For changed Go files, select exactly one formatter with `command -v gofumpt`: when found, run `gofumpt -w <changed-go-files>`; only when not found, silently run `gofmt -w <changed-go-files>`. Do not run both in sequence or switch tools when the selected formatter fails; report that failure. For changed non-Go files, run a formatter only when the confirmed plan or repository configuration defines its command; otherwise silently skip formatting.
6. Use `cortex:code-standards` for the targeted after-generation review after the formatting step. After code changes, recheck the final diff and candidate test assertions under Unit Test Selection before running only the approved matching cases; skip absent or non-isolatable cases without adding unplanned tests.
7. Require a full report in `task-<n>-report.md`: changed files, implementation evidence, source assumptions, validation commands and results, self-review, and concerns.
8. Require a short return message containing only `DONE`, `DONE_WITH_CONCERNS`, `NEEDS_CONTEXT`, or `BLOCKED`; changed-file list; one-line validation result; concerns; and the report path.

## Status Handling

- For `DONE`, inspect the report and cited repository evidence, then start task review.
- For `DONE_WITH_CONCERNS`, resolve correctness or scope concerns before task review; record non-blocking concerns in `progress.md`.
- For `NEEDS_CONTEXT`, provide only the missing task-local facts and dispatch again.
- For `BLOCKED`, determine whether the missing input is task context, task size, source evidence, or a plan defect. Provide facts, split the task, trace the code, or return to design or planning. Do not retry unchanged.

## Task Review

1. Dispatch a fresh review subagent for every completed task. The review subagent is read-only and follows `cortex:requesting-code-review`.
2. Give it the task brief, implementer report, task-scoped changed-file or diff evidence, and binding constraints. Never give it controller history.
3. Require the review subagent to return the complete review result and a short verdict in its response. A reviewer may neither create nor write controller-owned artifacts, and must not return an artifact path.
4. Recheck each cited source, call-chain, and diff fact before accepting the review conclusion. Treat unproven concerns as the open questions or residual risks defined by `cortex:requesting-code-review`.
5. For a behavior-changing task with approved call chain and pseudocode, compare the implementation with its task brief and applicable outcome mapping. Return material deviations to the controller's complete-proposal revision workflow.
6. After those checks, the controller writes the complete review result to the already controller-owned `task-<n>-review.md` and records the review verdict and every unresolved item in `progress.md`.

## Fix Loop

- For a proven finding that must be corrected, dispatch one implementer with the complete finding set for that task. The controller does not edit code in this workflow.
- Re-run only confirmed validation covering the fix, append evidence to the task report, and perform a fresh scoped re-review.
- When `cortex:systematic-debugging` returns a proven correction for a failed test point that preserves the confirmed design, plan, and test-point semantics, treat it as a proven finding for the affected task. After its scoped re-review, return to `cortex:testing` to rerun the failed and affected test points before final review.
- Resume the original implementer for rounds 1 through 3 when supported; otherwise use a fresh implementer with the brief, report, and findings. Use a fresh implementer for rounds 4 and 5.
- If a finding conflicts with the approved proposal, return the decision to the controller for evidence recheck and a revised complete proposal before dispatching a fix.
- After five unsuccessful fix rounds, stop and report every unresolved finding, evidence, and prior attempt to the user. Do not silently defer or discard a load-bearing finding.

## Manual Testing Handoff

- Do not invoke testing automatically.
- When the approved proposal includes a user-initiated test scheme, record its stage, readable approved baseline, and proposal revision in progress.md.
- After its report and baseline tasks and their reviews complete, return control to testing to reread, compare, and execute the already approved mappings without another scheme confirmation. Do not dispatch testing-controller actions as implementation tasks.
- Keep the handoff active through completion verification; complete initial testing before final review, and rerun affected points after scoped corrections.
- A changed test point, expected result, command mapping, or testing scope follows the complete-proposal revision workflow. Execution results alone do not change approval.

## Final Review And Handoff

1. After all tasks have clean task reviews and every manual testing handoff has completed its initial test execution, dispatch one fresh whole-change reviewer through `cortex:requesting-code-review` with the completed briefs, reports, review artifacts, confirmed requirements, applicable approved call chains and pseudocode, and whole-change diff evidence. Check that the combined implementation preserves applicable outcome mappings.
2. Send all final-review fixes as one scoped implementation task, then perform one fresh scoped re-review.
3. When a scope-preserving final-review correction changes source during an active manual testing handoff, return to `cortex:testing` to reread the report and rerun affected test points after that re-review.
4. After that revalidation completes, continue to completion verification without another whole-change review unless the revalidation fails or changes confirmed scope.
5. Use the complete-proposal revision workflow when final-review feedback changes approved decisions; include its required context reread and chain recheck.
6. Use `cortex:verification-before-completion` only after the final review has no unresolved blocking finding and all confirmed validation evidence is recorded.

## Scope

Coordinate isolated task execution and review for independent confirmed plan tasks. Keep task-scoped source evidence in `cortex:code-tracing`, code quality in `cortex:code-standards`, review standards in `cortex:requesting-code-review`, and completion evidence in `cortex:verification-before-completion`.
