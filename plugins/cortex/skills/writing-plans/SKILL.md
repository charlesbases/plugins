---
name: writing-plans
description: Build one complete engineering proposal from requirements, design input, and source evidence, then obtain its single implementation approval. Use before file-writing implementation or when a proposal needs revision.
---

# Writing Plans

## Proposal Boundary

- Follow the [single proposal workflow](../using-cortex/SKILL.md#workflow-state). Start from requirements, design input, and source evidence without a prior design approval.
- Do not edit implementation files, generate patches, or execute proposed work. Create a plan file only when explicitly requested as a deliverable.
- Use code-tracing for missing source proof and brainstorming for outcome-affecting unknowns or its Revision Evidence workflow. Resolve evidence and design questions before presenting a complete proposal.
- Preserve established constraints. Proposed behavior, dependencies, tests, artifacts, validation, and scope must map to an explicit requirement or proven prerequisite and remain pending until this complete proposal is approved.
- Do not silently expand a local request into global rules, broad refactors, shared abstractions, batch updates, or adjacent changes.

## Entry And Return

- If complete design input is missing, use brainstorming; do not wait for a separate design approval.
- If the complete current proposal is already presented but unapproved, discuss it or wait; do not generate a duplicate approval stage.
- For objections, new directions, narrowed meaning, or changed proposal decisions, require brainstorming's Revision Evidence before presenting the revised complete proposal. State the revision reason and retain the earlier approved content while the revision is pending.
- With an unchanged approved proposal, resume its execution workflow without another confirmation. The proposal already specifies its mode.
- A delegated implementer returns new decisions or missing authority to the controller rather than replacing the proposal or requesting user approval.

## Plan Construction

For this skill, behavior-changing work includes changes to runtime output (including logs), state, control flow, or external effects.

1. Review the requirements, design input, source evidence, and established constraints.
2. Identify affected files or packages, responsible symbols, entry points, ownership boundaries, state dependencies, and configuration dependencies.
3. Build an ordered task list with one coherent change per task. For behavior-changing work, build both an end-to-end code-level call chain and separate file-by-file pseudocode across the affected tasks; split only when files, dependencies, or ordering require it.
4. Define exact implementation boundaries, behavior that must remain unchanged, and validation evidence for every task.
5. Stop planning when every explicit requirement and proven prerequisite is covered. Present one proposal with context, design decisions, call chain, file changes and pseudocode, impact, validation, and execution mode.

## Required Detail

For every task, specify:

- The explicit requirement or proven prerequisite the task satisfies.
- Exact files or packages and affected symbols.
- Required behavior and unchanged behavior.
- Similar files, call sites, log points, adjacent paths, global rules, or shared abstractions that remain unchanged when they could be mistaken as included.
- For behavior-changing work, this task's segment of the separate call chain and file-by-file pseudocode. Mark applicable additions, changes, and removals; trace the source-supported entry and dispatch through key calls and branches to the changed state, output, persistence, or external boundary. Label new symbols as proposals and omit incidental local details.
- For a changed observable outcome, value, or state meaning, map the requested outcome and lifecycle point to the planned behavior or expression and expected result. If a narrowed proposal conflicts with that mapping, return to `cortex:brainstorming` rather than presenting it as equivalent.
- Relevant configuration, contract, ownership, and error-handling constraints.
- Dependencies on earlier tasks.
- Proposed validation scope, exact commands when required, and expected evidence. For Go formatting, preserve the selected execution workflow's formatter-selection rule; do not hard-code `gofmt`. For unit tests, include the method-to-case mapping or skip decision required by Unit Test Selection below.
- Required restoration artifacts and their paths and ownership. When the user requests SDD, include its progress and task-record work so complete-proposal confirmation authorizes those writes. Specify enough approved task content and constraints to recover all remaining tasks; a conversation-only plan reference is insufficient for that recovery.
- For a user-initiated testing handoff, include the test scheme's points, operation steps, expected results, command mappings, report path, and distinct readable baseline path and creation task in the complete proposal. Its one confirmation approves that scheme and those writes; preserve the prescribed report format. Direct execution does not create unplanned persistent records.

Do not use placeholders such as "add appropriate validation", "handle edge cases", "update related code", or "similar to the existing implementation".

## Call Chain And Pseudocode Output

- For behavior-changing work defined above, select the call-chain form from planned modifications, not files read for evidence. Count only production implementation files planned for modification; exclude tests and documentation.
  1. If the plan modifies at least two production implementation files, or adds or changes a cross-package or external-service call, use a Mermaid sequence diagram.
  2. Otherwise, if one production implementation file adds, changes, or removes a condition, loop, or state transition, use a Mermaid flowchart.
  3. Otherwise, use a fenced code block for the end-to-end call chain.
- An existing cross-package or external-service call encountered during tracing does not select a sequence diagram unless the plan adds or removes the call, changes its callee or dispatch, or changes the cross-boundary operation or contract. Editing only the text or arguments of an existing logging call does not by itself meet this rule.
- Make the call chain code-dominant: show source-supported files and symbols, proposed new symbols marked as such, relevant calls and arguments, predicates, returns or errors, and state or external boundaries. Keep prose secondary and reserve it for constraints, unchanged behavior, reasons, and validation.
- Put file-by-file pseudocode in a separate fenced code block grouped by file and responsible function. Mark additions, changes, and removals that apply; show key expressions, conditions, calls, returns, and state changes without writing complete implementation code.

## Unit Test Selection

- Default to existing unit tests for the methods or functions actually changed by the planned work, then recheck against the final diff. Do not expand selection to a file, directory, package, or repository. A runner's package path is only a location argument, not the test scope.
- Search outward from the changed symbol using references and nearby test naming conventions; follow `cortex:code-tracing` tool selection. Read only candidate tests, their assertions, and necessary helpers. Do not inventory and read every test before matching. Names and locations only locate candidates; actual exercised behavior and assertions establish relevance. An incidental call to the method does not establish relevance.
- Map each changed method to the smallest independently selectable existing test or subtest that validates its changed behavior. Do not run unrelated cases alongside it. If no matching case exists, stop the bounded search and skip unit tests for that method. If a candidate cannot be isolated from unrelated cases, skip it; if dynamic calls prevent proving relevance, report insufficient evidence rather than running a broad suite.
- Record each method, matched case or subtest, what it verifies, the exact selection command, and expected evidence; otherwise record the objective skip reason. After execution, confirm selected cases actually ran. A successful command with no matching tests is not a test pass. Use fresh execution evidence after the final relevant code change.
- Do not add unit tests by default. New tests may be planned only for an explicit user request, an approved new or changed method behavior or contract requiring uncovered validation, or an approved defect-regression need. State the method, independent expected result, and reason as a separate plan task; apply `cortex:code-standards` test-quality rules. Missing tests, a code edit, or a coverage target alone does not justify new tests. Once an approved new test is implemented, apply the same method-level selection and execution checks to it.
- A previously approved broad command does not establish method-level relevance. Broader unit-test execution requires a separate explicit user instruction; surface a conflicting repository-wide requirement rather than silently expanding the run.
- This policy governs unit tests. Keep formatting, lint, build, plugin/schema validation, and explicitly requested manual test methods under their own confirmed scope.

## Manual Testing Handoff

- Do not invoke `cortex:testing` automatically. Process a test-point draft only after the user explicitly invokes `cortex:testing` and it hands the draft to this skill.
- For every executable test point, map its ID to an existing test, approved manual method, or separately justified new test implementation, the approved validation command, and expected evidence. Selecting a unit test must follow Unit Test Selection; a test point does not itself require new unit-test code.
- Present the test-point draft and complete scheme as part of this proposal before its confirmation. Specify report and baseline creation after the implementation tasks they cover; then reread the report against the approved scheme and run mapped tests without another confirmation. Keep the handoff active through completion verification.
- Keep this mapping in the implementation plan; do not add commands to the test-point report format. The approved-scheme baseline records the confirmed mappings and proposal revisions for later comparison, not a new source of command authorization.

## Proposal Output Review And Confirmation

1. Map every explicit requirement to a task.
2. Verify source context, design decisions, files, symbols, order, call chains, data flow, errors, ownership, validation, and constraints. Reject a file-and-action list that omits how the paths connect.
3. Reject a behavior-changing proposal without both the selected code-level call-chain form and separate file-by-file pseudocode, or with a mismatched outcome, expression, value definition, or lifecycle point.
4. For a revised direction, check its reread context, necessary chain, feasibility, semantic comparison, and impact. A previously valid receipt does not establish these conclusions by itself.
5. Resolve unclear implementation decisions; return evidence gaps to code-tracing and design gaps to brainstorming. Keep proposals within the stated scope.
6. State execution mode before confirmation: direct execution by default; user-requested SDD only for tasks satisfying that skill's requirements, with all recovery writes included.
7. Present the complete current proposal. Wait for its single user confirmation, then execute it without separate design, plan, mode, or already-included test-scheme approval. A plan-only request produces the proposal and does not authorize execution.

## Scope

Define complete proposals, their single confirmation, and execution handoff. Keep design analysis in `cortex:brainstorming`, source evidence in `cortex:code-tracing`, execution workflows in `cortex:subagent-driven-development` or `cortex:executing-plans`, code quality in `cortex:code-standards`, and completion evidence in `cortex:verification-before-completion`.
