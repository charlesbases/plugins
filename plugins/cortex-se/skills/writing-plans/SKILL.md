---
name: writing-plans
description: Build a complete engineering plan from requirements, design input and source evidence, then execute within a clear implementation request without separate plan approval.
---

# Writing Plans

## Proposal Boundary

- Follow the [request-scoped workflow](../using-cortex/SKILL.md#workflow-state). Start from brainstorming's retained scope, its requirement or evidence basis and the design produced within that scope, without a separate scope or design approval.
- Do not edit implementation files, generate patches, or execute proposed work. Create a plan file only when explicitly requested as a deliverable.
- Use code-tracing for missing source proof and brainstorming for outcome-affecting unknowns or its Revision Evidence workflow. Resolve evidence and design questions before presenting a complete proposal.
- Apply [Evidence And Clarification](../using-cortex/SKILL.md#evidence-and-clarification) when accepting design input. Preserve the distinction between confirmed facts, explicit requirements, hypotheses, external evidence gaps, and proposed behavior throughout the proposal.
- Preserve established constraints. Proposed behavior, dependencies, tests, artifacts, validation and scope must map to an explicit requirement or proven prerequisite. A plan-only request does not authorize implementing the plan.
- Do not silently expand a local request into global rules, broad refactors, shared abstractions, batch updates, or adjacent changes.

## Entry And Return

- If scope pruning or complete design input is missing, return to brainstorming before constructing the proposal. Return unsupported additions to requirement analysis; do not establish their necessity by turning them into design decisions or implementation tasks. Do not request a separate scope or design approval.
- If the complete current plan exists, reuse it. For a plan-only request, discuss or deliver the plan; for an implementation request, continue within its scope without a new approval stage.
- For objections, new directions, narrowed meaning, or changed proposal decisions, require brainstorming's Revision Evidence before presenting the revised complete proposal. State the revision reason and retain the earlier authorized content while the revision is pending.
- With an unchanged request-scoped plan, resume its execution workflow without another confirmation. The proposal already specifies its mode.
- A delegated implementer returns new decisions or missing authority to the controller rather than replacing the proposal or requesting user approval.

## Plan Construction

Read the controller's [Current Task Context](../using-cortex/references/task-context.md) before constructing or revising a proposal. Map it to all effective user requirements and constraints, not only the latest message. Keep facts, user requirements and proposed choices distinct. Include removal of artifacts and expectations made obsolete by a replaced design; preserve shared and independently owned work. Context stores only clarified user requirements and constraints, not this proposal or its approval record. Before delivering the proposal or implementing it, check the entire plan against those requirements. Recover the full request-scoped plan and actual authorizing user request from the conversation or existing authorized records when execution requires them; do not create another record automatically.

For this skill, behavior-changing work includes changes to runtime output (including logs), state, control flow, or external effects.

1. Review the retained scope and its basis before constructing tasks. Verify that the design stays within it and that its key premises remain supported, including whether existing mechanisms already satisfy the requirement. Return scope additions to brainstorming's requirement analysis, critical source gaps to tracing, and genuine user decisions to brainstorming.
2. Identify affected files or packages, responsible symbols, entry points, ownership boundaries, state dependencies, and configuration dependencies.
3. Build an ordered task list with one coherent change per task. For behavior-changing work, build both an end-to-end code-level call chain and separate file-by-file pseudocode across the affected tasks; split only when files, dependencies, or ordering require it.
4. Define exact implementation boundaries, behavior that must remain unchanged, and validation evidence for every task.
5. Stop planning when every explicit requirement and proven prerequisite is covered. Present one proposal with context, design decisions, call chain, file changes and pseudocode, impact, validation, and execution mode.

## Required Detail

For every task, specify:

- The explicit requirement or proven prerequisite the task satisfies, with the source and evidence status of its key premises. A corrective task must identify the confirmed problem and why the modification is necessary; a new feature task must identify the explicit requirement without inventing an existing defect.
- Exact files or packages and affected symbols.
- Required behavior and unchanged behavior.
- Similar files, call sites, log points, adjacent paths, global rules, or shared abstractions that remain unchanged when they could be mistaken as included.
- For behavior-changing work, this task's segment of the separate call chain and file-by-file pseudocode. Mark applicable additions, changes, and removals; trace the source-supported entry and dispatch through key calls and branches to the changed state, output, persistence, or external boundary. Label new symbols as proposals and omit incidental local details.
- For a changed observable outcome, value, or state meaning, map the requested outcome and lifecycle point to the planned behavior or expression and expected result. If a narrowed proposal conflicts with that mapping, return to `cortex-se:brainstorming` rather than presenting it as equivalent.
- For a migration or rename that must preserve behavior, compare each changed observable assignment with the current path. A required protocol or field migration does not justify an additional state reset. Ambiguous descriptions of an existing action do not override a preservation constraint; preserve the current effect or resolve an explicit conflicting requirement before including that operation.
- Relevant configuration, contract, ownership, and error-handling constraints.
- Dependencies on earlier tasks.
- Proposed validation scope, exact commands when required, and expected evidence. For unit tests, include the method-to-case mapping or skip decision required by Unit Test Selection below.
- Required restoration artifacts and their paths and ownership. When the user requests SDD, include its progress and task-record work within that request's scope. Specify enough task content and constraints to recover all remaining tasks; a conversation-only plan reference is insufficient for that recovery.

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
- Search outward from the changed symbol using references and nearby test naming conventions; follow `cortex-se:code-tracing` tool selection. Read only candidate tests, their assertions, and necessary helpers. Do not inventory and read every test before matching. Names and locations only locate candidates; actual exercised behavior and assertions establish relevance. An incidental call to the method does not establish relevance.
- Map each changed method to the smallest independently selectable existing test or subtest that validates its changed behavior. Do not run unrelated cases alongside it. If no matching case exists, stop the bounded search and skip unit tests for that method. If a candidate cannot be isolated from unrelated cases, skip it; if dynamic calls prevent proving relevance, report insufficient evidence rather than running a broad suite.
- Record each method, matched case or subtest, what it verifies, the exact selection command, and expected evidence; otherwise record the objective skip reason. After execution, confirm selected cases actually ran. A successful command with no matching tests is not a test pass. Use fresh execution evidence after the final relevant code change.
- Do not add unit tests by default. New tests may be planned only for an explicit user request, a requested new or changed method behavior or contract requiring uncovered validation, or an established defect-regression need. State the method, independent expected result, and reason as a separate plan task; apply `cortex-se:code-standards` test-quality rules. Missing tests, a code edit, or a coverage target alone does not justify new tests. Once an authorized new test is implemented, apply the same method-level selection and execution checks to it.
- A previously authorized broad command does not establish method-level relevance. Broader unit-test execution requires a separate explicit user instruction; surface a conflicting repository-wide requirement rather than silently expanding the run.
- This policy governs unit tests. Keep formatting, lint, build, plugin/schema validation, and explicitly requested manual test methods under their own confirmed scope.

## Proposal Output Review And Handoff

Apply the shared [Architecture Design](../../references/architecture-design.md) policy to architecture-relevant proposals. Include the key principle-to-decision links, evidence or proposed validation, and material exceptions required by that policy; return unresolved critical conflicts to brainstorming. Keep this within the current complete plan, without a separate checklist or approval stage.

1. Map every explicit requirement to a task.
2. Verify source context, design decisions, files, symbols, order, call chains, data flow, errors, ownership, validation, and constraints. Reject a file-and-action list that omits how the paths connect.
3. Check the mapping from requirement or confirmed problem to design decision to modification task, including each added observable state operation within a task. Reject dependent operations or tasks whose critical premises remain inferred or unresolved, whose necessity is unsupported, or whose strongest relevant counterexample has not been reconciled. Structural checkpoint success or a self-declared status does not resolve these gaps; conditional design discussion is not a complete implementation proposal.
4. Reject a behavior-changing proposal without both the selected code-level call-chain form and separate file-by-file pseudocode, or with a mismatched outcome, expression, value definition, or lifecycle point.
5. For a revised direction, check its reread context, necessary chain, feasibility, semantic comparison, and impact. A previously valid receipt does not establish these conclusions by itself.
6. Resolve unclear implementation decisions; return evidence gaps to code-tracing and design gaps to brainstorming. Keep proposals within the stated scope.
7. State execution mode before execution: direct execution by default; user-requested SDD only for tasks satisfying that skill's requirements, with all scoped recovery writes included.
8. Present the complete current plan. A clear implementation request authorizes its stated scope; execute without separate design, plan or mode confirmation. Ask only for a material unresolved user decision. A plan-only request produces the plan and does not authorize execution.

## Scope

Define complete plans and execution handoff within the user-requested scope. Keep design analysis in `cortex-se:brainstorming`, source evidence in `cortex-se:code-tracing`, execution workflows in `cortex-se:subagent-driven-development` or `cortex-se:executing-plans`, code quality in `cortex-se:code-standards`, and completion evidence in `cortex-se:verification-before-completion`.
