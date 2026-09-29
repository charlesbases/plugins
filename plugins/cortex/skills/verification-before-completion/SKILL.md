---
name: verification-before-completion
description: Verify fresh evidence before claiming work is complete, fixed, validated, reviewed, ready, or passing. Use after implementation, debugging, review, documentation updates, or other final conclusions.
---

# Verification Before Completion

## Verification Gate

- For repository conclusions, finish the [batch evidence checkpoint](../code-tracing/references/evidence-contract.md) using evidence current after the relevant changes. Reuse unchanged receipts; reread only invalidated or missing ranges.
- Report structural checkpoint status separately from semantic correctness and task completion. An analysis checkpoint may legitimately contain inferred or unresolved claims; do not present those as established facts or use them as a change basis.
- For implementation governed by an approved call chain and separate pseudocode, compare the final path with both. For a changed observable outcome, value, or state meaning, compare the final behavior or expression and lifecycle point with the confirmed mapping. Structural checkpoints, format checks, and tests alone do not prove semantic equivalence.
- If a checkpoint fails, correct its specific evidence gap or narrow the claim. Do not add unrelated reads, a new review agent, or repeated automatic review rounds merely to make the gate pass.

- Identify the evidence required for every completion, fix, implementation, validation, review, readiness, or passing claim.
- Use evidence produced after the final relevant change.
- Derive scope only from user instructions, confirmed plans, and repository policy.
- Do not add tests, expand test scope, run full suites, contact external services, or run validation outside that scope.
- Read complete command results, including exit status, failures, and claim-specific evidence.
- Do not infer build success from lint, requirement satisfaction from tests, or bug resolution from a code change alone.
- Do not claim success when evidence is stale, incomplete, failed, or environment-blocked.
- Use `cortex:code-tracing` before claiming behavior, reachability, impact, state, configuration, or compatibility is correct when source evidence does not prove it.
- For code generated or edited by Codex, confirm the targeted after-generation `cortex:code-standards` review completed. For changed Go files, confirm the Go formatting step required by the selected execution workflow completed.
- Verify language-specific checks and formatters only when Cortex, the confirmed plan, or repository configuration defines them. Do not treat an undefined language-specific step as incomplete verification.

## Implementation Context

- For an implementation completion claim, recover the complete proposal revision, actual approving instruction, file and validation scope, execution mode, remaining work, and active testing baseline. Use task context or authorized recovery records; this skill does not create write or test authority or another approval stage.
- Do not require an implementation plan or its approvals merely to verify a read-only analysis, review, or code-only response. Verify the evidence appropriate to that requested result.
- If implementation state is incomplete, identify the missing fact and the appropriate upstream stage before further writes or tests. A previous `approved` flag or existing source change does not establish the missing authority.

## Verification Workflow

1. Re-read the request, established constraints, and applicable approved complete proposal.
2. Identify every final claim.
3. Map each claim to the smallest sufficient evidence: source review, diff review, targeted command, manual verification, or approved external interaction.
4. Inspect final artifacts and relevant context; inspect status and diffs when relevant.
5. Run or review every validation in scope.
6. Reconcile requirements, implementation, validation results, and unresolved items before reporting.

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

- Use `cortex:systematic-debugging` when required validation fails and the root cause is not proven.
- Resume the approved execution workflow for incomplete implementation, formatting, generated-code review, or proven in-scope corrections when key decisions and scope remain valid. Pass the incomplete step, reason, applicable approval, and active testing baseline; do not restart confirmation.
- Changed decisions follow [Proposal Revision](../using-cortex/SKILL.md#proposal-revision), including affected context reread and necessary chain, feasibility, and semantic rechecks. Present one revised complete proposal before changed implementation or validation.
- State incomplete validation, failed checks, blocked commands, and residual risk directly.

## Output

Report only completed or changed work, verification performed and its result, whether the approved complete proposal was satisfied, and uncompleted work, failed checks, blocked validation, or residual risk. Distinguish `passed`, `not run`, `failed`, and `insufficient evidence`. Do not paste large logs, source files, diffs, or command output.

## Scope

Define completion evidence and final status reporting. Keep source analysis in `cortex:code-tracing`, code-quality rules in `cortex:code-standards`, debugging in `cortex:systematic-debugging`, planning in `cortex:writing-plans`, execution workflows in `cortex:subagent-driven-development` or `cortex:executing-plans`, and change review in `cortex:requesting-code-review`.
