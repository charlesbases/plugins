---
name: testing
description: Draft test points and execute their approved scheme for business or application changes through the single complete-proposal workflow. Use only when the user explicitly invokes $testing.
---

# Testing

## Gates

- Start only when the user explicitly invokes `$testing`. Requests to verify, check, inspect, or confirm behavior do not implicitly invoke this test-point workflow.
- Follow the [single proposal workflow](../using-cortex/SKILL.md#workflow-state). Draft test points from requirements and design input without a prior design approval; a complete testing scheme must be included in the proposal before its one confirmation.
- Derive proposed points from explicit requirements, established constraints, repository policy, and relevant source evidence. After approval, keep commands and writes within that proposal's testing scope.
- Use code-tracing before deriving points whose behavior, call chains, state, data flow, or boundaries are not proven.
- Explicit invocation does not authorize new unit tests. Apply [Unit Test Selection](../writing-plans/SKILL.md#unit-test-selection) and keep manual methods distinct.
- Before report, baseline, or test implementation writes and mapped test execution, require the approved complete proposal specifying them. Do not ask again for a scheme already included in it.
- Do not execute commands introduced only by report edits; compare with the approved readable baseline and authorized mappings.
- Keep the handoff active from report creation through completion verification. A scope-preserving correction is continuation, not a new invocation.

## Entry And Return

- Without explicit invocation, retain the existing observation or validation workflow.
- For a new invocation, resolve necessary design input with brainstorming and draft test points for writing-plans. Present the complete testing proposal, including mappings and artifact tasks, for one confirmation.
- For an active handoff, recover its approved complete proposal, mode, command mappings, and readable baseline. Resume the pending report or execution step without repeating point design or approval.
- If approved content or authority cannot be recovered, ask only for the missing facts. New report semantics or mappings follow Proposal Revision, not a separate scheme gate.

## Test-Point Design

1. Review explicit requirements, design input, relevant code changes, and required source evidence.
2. Trace only the code paths required to establish the changed behavior and business boundaries.
3. Derive one test point for each independently verifiable behavior, boundary condition, or regression risk in scope.
4. Define concrete operation steps and observable expected results for every test point.
5. Produce a report draft in the required format with every test result set to `未执行`.
6. Hand the draft to writing-plans for inclusion with its complete scheme in the proposal before confirmation.

`cortex:writing-plans` must map every executable test point to an existing test, approved manual method, or separately justified new test implementation and allowed validation commands, and include the test-point file path and creation task in the implementation plan. Do not create unit-test code merely to make every report point executable; use the approved method or report objective unavailability.

## Approved Scheme Baseline

- The complete proposal specifies the readable baseline path and creation or update task. Its one user confirmation approves the included scheme's stable IDs, operations, expected results, command mappings, artifact writes, and scope.
- After proposal approval, create that planned baseline from the exact approved scheme and record its proposal revision and actual approving instruction. Keep the baseline outside the prescribed report format.
- Compare the reread report's points, operations, and expectations and currently authorized mappings with that baseline before execution or resumed execution. Results, checkboxes, run timestamps, and formatting-only changes with identical meaning do not invalidate approval.
- A hash or approval flag cannot replace readable content. Recover a missing baseline only from the actual approved proposal and instruction within its planned artifact authority; do not adopt the current mutable report as an approved baseline.
- Preserve the old baseline while a changed complete proposal awaits confirmation. Replace it only after that revised proposal is approved. An SDD implementation worker cannot approve schemes or update the controller's approval baseline.

## Approved Scheme Handoff

1. Include the draft report, complete scheme, command mappings, report path, and baseline task in the proposal before its one confirmation. Permit user discussion and edits at that stage.
2. After approval and the covered implementation tasks, create the planned report and baseline, then reread the complete report before test execution.
3. If report semantics and mappings still match the approved scheme, execute without another scheme confirmation. General approval only covers a scheme actually presented in the complete proposal.
4. If rereading introduces unplanned code, commands, dependencies, points, expected results, or scope, pause affected testing and return through the complete-proposal revision workflow. Reread affected context and recheck necessary paths and semantics before proposing the changed scheme.
5. After a source correction that preserves the approved proposal and scheme, reread the report and rerun only failed and affected points without another confirmation.
6. Execution-result updates do not create a revised scheme or change the approved baseline.

## Test Execution

For every test point in the reread file:

1. Run its mapped test or approved validation method.
2. Set `测试结果` to `通过` only after the expected result is verified.
3. Set `测试结果` to `失败` when validation fails, and keep its directory checkbox unchecked.
4. Set `测试结果` to `无法实施：<客观原因>` only when the confirmed scope cannot provide a valid test method, and keep its directory checkbox unchecked.
5. Never mark a point as passed based on source review, a successful build, or an unrelated test.
6. Update the directory checkbox to `[x]` only when the matching test result is exactly `通过`.

For every validation failure, use `cortex:systematic-debugging` immediately. Do not stop after marking a point failed or leave failure investigation to the user.

After a root cause is proven:

- Resume the approved proposal's execution workflow only when the proven correction preserves its key decisions, implementation and validation scope, and test-point semantics.
- When correction changes approved decisions, apply Proposal Revision, including affected context reread and necessary chain and semantic rechecks, before presenting a revised complete proposal.
- Rerun the failed point and every affected point after the correction.
- Do not change a test point's expected result merely to make it pass.

## Test Point Report Format

Create the report with this exact structure:

```md
# <功能名称> 测试报告

## 测试点目录

- [ ] TP-001：<测试点名称>
- [ ] TP-002：<测试点名称>

## 测试点详情

### TP-001：<测试点名称>

**操作步骤**

1. <操作一>
2. <操作二>

**预期结果**

<可验证的业务结果>

**测试结果**

未执行
```

- List only the stable test-point ID and name in `测试点目录`.
- Use `TP-NNN` IDs. Do not automatically renumber IDs after points are added, removed, or edited.
- Add one matching `测试点详情` section for every directory entry.
- Keep this exact order in every detail section: `操作步骤`, `预期结果`, `测试结果`.
- Allow only `通过`, `失败`, `未执行`, and `无法实施：<客观原因>` as test results.
- Mark a directory entry `[x]` only when its matching result is exactly `通过`. Keep every other entry `[ ]`.
- Do not add test type, preconditions, evidence, summary, commands, or other fields.

## Completion Handoff

- Do not return to `cortex:verification-before-completion` while any test point is `失败` or `未执行`; continue root-cause resolution or return for the required confirmed scope change.
- After every test point is `通过` or `无法实施：<客观原因>`, return to the selected execution workflow for any remaining confirmed tasks and required review. That workflow invokes `cortex:verification-before-completion` only after its completion conditions are met.
- Update any planned recovery record with the active handoff's stage and approved baseline reference before returning to the selected execution workflow. Report unavailable points directly.
- Do not claim that all functionality is correct beyond executable test points that passed.
