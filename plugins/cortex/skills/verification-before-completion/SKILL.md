---
name: verification-before-completion
description: Verify fresh evidence before claiming work is complete, fixed, validated, reviewed, ready, or passing. Use after implementation, debugging, review, documentation updates, or other final conclusions.
---

# Verification Before Completion

## Verification Gate

- Read [Current Task Context](../using-cortex/references/task-context.md) and reconcile actual results with all effective user requirements and constraints. Check requirements against their sources when meaning is disputed, rather than using the latest design or its tests as the definition of success. Report progress, cleanup, results and unresolved items in the response, not context. Reading/updating the requirements file is not evidence that its contents were obeyed; withdraw unsupported conclusions instead of replacing them with unverified explanations.

- For repository conclusions, finish the [batch evidence checkpoint](../code-tracing/references/evidence-contract.md) using evidence current after the relevant changes. Reuse unchanged receipts; reread only invalidated or missing ranges.
- Report structural checkpoint status separately from semantic correctness and task completion. An analysis checkpoint may legitimately contain inferred or unresolved claims; do not present those as established facts or use them as a change basis.
- For implementation governed by an approved call chain and separate pseudocode, compare the final path with both. For a changed observable outcome, value, or state meaning, compare the final behavior or expression and lifecycle point with the confirmed mapping. Structural checkpoints, format checks, and tests alone do not prove semantic equivalence.
- If a checkpoint fails, correct its specific evidence gap or narrow the claim. Do not add unrelated reads, a new review agent, or repeated automatic review rounds merely to make the gate pass.

- Identify the evidence required for every completion, fix, implementation, validation, review, readiness, or passing claim.
- Use evidence produced after the final relevant change.
- Derive scope only from user instructions, confirmed plans, and repository policy.
- Apply [Validation Environment](../using-cortex/SKILL.md#validation-environment) before executing checks that produce artifacts or change state. Verify the selected isolated or restorable-local route, actual state and output paths, child-process inheritance and effective permissions. For local validation, establish the affected resources' original state and how changes will be restored before execution. Report checks as blocked when neither route is feasible or required execution capabilities remain unavailable.
- Do not add tests, expand test scope, run full suites, contact external services, or run validation outside that scope.
- Read complete command results, including exit status, failures, and claim-specific evidence.
- Do not infer build success from lint, requirement satisfaction from tests, or bug resolution from a code change alone.
- Do not claim success when evidence is stale, incomplete, failed, or environment-blocked.
- Use `cortex:code-tracing` before claiming behavior, reachability, impact, state, configuration, or compatibility is correct when source evidence does not prove it.
- For code generated or edited by Codex, confirm the targeted after-generation `cortex:code-standards` review completed. For changed Go files, confirm the Go formatting step required by the selected execution workflow completed.
- Verify language-specific checks and formatters only when Cortex, the confirmed plan, or repository configuration defines them. Do not treat an undefined language-specific step as incomplete verification.

## Implementation Context

- For an implementation completion claim, recover the complete proposal revision, actual approving instruction, file and validation scope, execution mode and active testing baseline from the conversation or existing authorized records. Check remaining work against actual files. Context supplies only the user's requirements and constraints; this skill does not create write or test authority or another approval stage.
- Do not require an implementation plan or its approvals merely to verify a read-only analysis, review, or code-only response. Verify the evidence appropriate to that requested result.
- If implementation state is incomplete, identify the missing fact and the appropriate upstream stage before further writes or tests. A previous `approved` flag or existing source change does not establish the missing authority.

## Verification Workflow

1. Re-read the request, established constraints, and applicable approved complete proposal.
2. Identify every final claim.
3. Map each claim to the smallest sufficient evidence: source review, diff review, targeted command, manual verification, or approved external interaction.
4. Inspect final artifacts and relevant context; inspect status and diffs when relevant.
5. Run or review every validation in scope.
   Confirm validation-owned processes have stopped and temporary artifacts are removed. For isolation, verify protected daily resources were unchanged; for local validation, compare affected resources with their original state and verify all validation-induced changes were restored. Preserve approved implementation edits and unrelated user work. Report restoration failures or residue even if functional checks passed.
6. Reconcile requirements, implementation, validation results, and unresolved items before reporting.

When the controller is delivering an approved implementation, perform the following checks before its first delivery, including after resuming a task. Use the latest user instructions and the current requirements from the context hook, reading `context.md` directly if the injection is absent, incomplete or stale. Recover the approved proposal and approving instruction separately. If a new requirement conflicts with an approved decision, route the affected work through Proposal Revision. Use evidence from direct execution or the existing SDD final review that still applies to the final change.

- **Requirements.** Map each effective requirement to the delivered result and its evidence. Identify it as satisfied, unmet or insufficiently evidenced.
- **Complexity.** Compare actual branches, state, dependencies and relevant hot-path work with the design assessment. Check material changes in scaling, copies or allocations. Distinguish source-based estimates from measurements and use comparable workloads and output semantics for measured claims.
- **Existing business behavior.** Compare the final change attributable to this task with the actual state before implementation, including pre-existing user edits. Trace affected consumers through relevant branches, exits, failures and side effects. Check that the implemented replacements, retained consumers and historical-data handling match the selected strategy. Report intended changes, unintended regressions and gaps, including an unavailable baseline.

## Claim Evidence

- Requirement satisfied: Map every confirmed requirement to the delivered artifact or behavior and required validation evidence.
- Plan semantics satisfied: For work with approved call chain and pseudocode, verify the final path against both; for changed outcomes, values, or state meanings, verify the final behavior or expression at the approved lifecycle point against the confirmed mapping.
- Implementation complete: Confirm every planned task completed within confirmed scope.
- Generated-code review complete: Confirm targeted after-generation code-standards review for all code generated or edited by Codex; for changed Go files, also confirm formatting completed.
- Bug fixed: Confirm the root-cause correction with final evidence required by the user, confirmed plan, or repository policy. Run a deterministic reproduction only when that scope defines or provides one.
- Unit tests pass: Follow [Unit Test Selection](../writing-plans/SKILL.md#unit-test-selection). Confirm the exact selected cases actually ran after the final relevant code change and passed; a successful command with no matching cases is not passing evidence.
- Unit tests not run: Report no matching existing test, inability to isolate the relevant case, or insufficient relevance evidence explicitly. This does not authorize adding tests and must not be reported as a pass.
- Lint passes: Run the required lint command after the final relevant change and confirm it reports no real issues.
- Build passes: Run the required build command after the final relevant change and confirm it succeeds.
- Review complete: Confirm the entire requested review scope was assessed and report formal findings, open questions, and residual risks accurately.
- Documentation complete: Confirm content, scope, format, and referenced artifacts match confirmed requirements.
- Manual testing: When the confirmed scope includes a `cortex:testing` test-point report, reread that report after the final relevant change. Require every point to be `通过` or `无法实施：<客观原因>`, verify each directory checkbox matches its detail result, and confirm the mapped validation evidence is current.

## Go Validation

- After Go code changes, run `golangci-lint run --new-from-rev=HEAD`.
- Classify every reported lint issue against the confirmed current business change using its approved files, symbols, and diff. A lint issue caused by the current business change is a required validation failure.
- Record a lint issue outside the current business change separately, do not change it or suppress it with `//nolint`, and do not let it block the completion claim scoped to the current business change.
- When evidence cannot classify a lint issue as current-business or outside-scope, report the attribution as insufficient evidence rather than assigning it to the current business change.
- Do not use `//nolint` to suppress a real lint issue.
- If lint fails because of environment, permissions, or cache state, report the cause and any real issues already reported; do not claim lint passes.
- For Go unit tests, select exact test or subtest names for the modified methods under Unit Test Selection. Do not default to unfiltered package tests or `go test ./...`; use a fresh run and verify selected cases executed. Broader runs require a separate explicit user instruction, not merely a previously approved broad command.

## Incomplete Verification

- When a check fails and the cause or impact is unclear, use `cortex:systematic-debugging` for read-only diagnosis within the existing authority. Identify the reachable failure and its effect. If evidence is still missing, report the gap and pause the dependent correction.
- Return incomplete implementation, formatting or generated-code review to its approved execution workflow. For an implementation deviation, first establish that the fix restores the confirmed result within the approved files, key decisions, business contracts, side effects and validation scope. Return the proven correction and any active testing baseline to the original execution mode without another confirmation. Rerun the failed and affected approved checks and reassess the final diff. A standalone read-only review returns findings.
- If the implementation follows the design but misses the requirement, or a remedy would change an approved decision or scope, pause affected work. Explain which assumption failed and its impact, then follow [Proposal Revision](../using-cortex/SKILL.md#proposal-revision) for one revised complete proposal before changed implementation or validation.
- State incomplete validation, failed checks, blocked commands, and residual risk directly.

## Output

Before reporting, verify context contains only the user's explicit current requirements and constraints. Save clarified requirement changes when necessary; unchanged requirements need no rewrite. Do not add completion status, results, remaining work, approval records or testing baseline references. Verify required saves succeeded and report persistence or recovery gaps in the response.

Report only completed or changed work, verification performed and its result, whether the approved complete proposal was satisfied, and uncompleted work, failed checks, blocked validation, or residual risk. Distinguish `passed`, `not run`, `failed`, and `insufficient evidence`. Do not paste large logs, source files, diffs, or command output.

For an implementation delivery, state requirement coverage, actual complexity and impact on existing business behavior in the initial response. Combine supported conclusions when useful, name specific deviations or gaps, and respect the user's requested output format.

## Scope

Define completion evidence and final status reporting. Keep source analysis in `cortex:code-tracing`, code-quality rules in `cortex:code-standards`, debugging in `cortex:systematic-debugging`, planning in `cortex:writing-plans`, execution workflows in `cortex:subagent-driven-development` or `cortex:executing-plans`, and change review in `cortex:requesting-code-review`.
