---
name: writing-plans
description: Specify how a behavior-changing implementation will work, or produce an implementation plan when the user requests one.
---

# Writing Plans

## Entry

- Start from the user's requested outcome and any material design decisions already resolved. A clear implementation request needs no separate design approval. If a decision would change the outcome, contract, ownership, or scope, use `cortex-se:brainstorming` and ask for that decision before implementing it.
- Read project instructions and use `cortex-se:code-tracing` to establish existing behavior, configuration, state, and the complete relevant call chain before making source-based plan claims. For new functionality with no existing path, establish its integration points and label the new chain as proposed.
- Apply [Evidence And Clarification](../using-cortex/SKILL.md#evidence-and-clarification) when accepting design input. Preserve the distinction between confirmed facts, explicit requirements, hypotheses, external evidence gaps, and proposed behavior throughout the plan.
- For objections, new directions, narrowed designs, or changed meanings, require brainstorming's [Revision Evidence](../brainstorming/SKILL.md#revision-evidence) before presenting the updated plan. Include its source basis, feasibility, semantic comparison and impact without changing SE's confirmation policy.
- A request for a plan only ends with the plan. Otherwise, complete the plan and continue to `cortex-se:executing-plans` without a separate plan confirmation or execution-mode question. Create a plan file only when requested.

## Describe How, Not Merely What

For behavior-changing work, the plan must include:

1. The requested observable outcome and the point in the lifecycle where it must hold. For changed output, values or state meaning, connect the source-proven or explicitly proposed definition and calculation to the planned behavior/expression and expected result; do not treat a different meaning as equivalent.
2. An end-to-end, code-level chain from source-supported entry and dispatch through relevant branches, calls, state changes, and error or output boundary. Mark proposed symbols and links as proposals.
3. Separate file-by-file pseudocode grouped by responsible symbol. Show the planned additions, changes, or removals; key conditions, expressions, calls, returns, and state transitions. This is implementation logic, not complete source code.
4. Relevant configuration, ownership, compatibility, and error-handling constraints, including behavior that must remain unchanged.
5. An ordered implementation sequence where dependencies matter, plus targeted validation and the evidence expected from each check.

A list such as "edit these files, add tests, run checks" is not an implementation plan. Map each step to the explicit requirement or proven prerequisite, with the source and evidence status of its key premises; omit unrelated cleanup. A corrective step must identify the confirmed problem and why the modification is necessary. A new feature step must identify the explicit requirement without inventing an existing defect.

Before the first behavior-changing write or delegated dispatch, present both the call-chain representation and a separate fenced pseudocode block grouped by changed file and responsible symbol. This is a concrete execution reference, not a confirmation request. Do not replace the pseudocode with prose, a file list, or the call-chain diagram, even for a small behavior change. If either representation is missing, complete the plan and then continue without waiting for another approval.

## Check The Plan's Basis

- Verify the key premises of each design decision, including whether existing mechanisms already satisfy the requirement. Check the mapping from requirement or confirmed problem to design decision to modification step, including each added observable state operation within a step.
- Reject dependent operations or steps whose critical premises remain inferred or unresolved, whose necessity is unsupported, or whose strongest relevant counterexample has not been reconciled. Structural checkpoint success or a self-declared status does not resolve these gaps; conditional design discussion is not a complete how-to plan.
- For a migration or rename that must preserve behavior, compare each changed observable assignment with the current path. A required protocol or field migration does not justify an additional state reset. Ambiguous descriptions of an existing action do not override a preservation constraint; preserve the current effect or resolve an explicit conflicting requirement before including that operation.
- Return source gaps to `cortex-se:code-tracing` and genuine material user decisions to `cortex-se:brainstorming`. Continue authorized independent work; once the required premises and decisions are established, continue execution without a separate plan confirmation or execution-mode question.

## Call-Chain Form

- Apply these cases in order to the proposed production changes: when at least two production implementation files change, or a cross-package or external-service call is added or changed, use a Mermaid sequence diagram for the end-to-end path.
- Otherwise, when a single production file adds, changes, or removes a condition, loop, or state transition, use a Mermaid flowchart for the branches.
- For all other changes, use a fenced code block for the end-to-end chain and no diagram. Existing conditions or state mutations merely traversed by the chain do not meet the flowchart trigger; changing only a logged value or output expression follows this case. In every case keep file-by-file pseudocode separate from the call-chain representation.
- Do not choose a diagram based on files merely read for evidence. Show source-supported files and symbols, key predicates and values, and the changed boundary. A diagram with only file names or task labels does not satisfy the call-chain requirement.

## Validation Planning

- Apply Unit Test Selection below to unit tests. Keep formatting, lint, build, integration checks and requested manual methods in their own project-defined and requested scope; broader observation does not authorize unrelated unit cases.
- Prefer the check that observes the changed contract at the right level; a cross-boundary behavior may need a broader check than a private-function unit test. State what each validation proves and what it cannot prove.
- If the environment cannot perform a required check, plan to report that gap accurately rather than substituting an unrelated passing command.

## Unit Test Selection

- Start from the methods/functions actually changed, and recheck that mapping against the final diff. A file, directory, package or repository is not the unit-test selection boundary; a runner's package path only locates the cases.
- Search outward from the changed symbol using references and nearby test conventions under code-tracing's tool rules. Read only candidate assertions and necessary helpers. Names, locations or incidental calls locate candidates but do not establish relevance; do not read every test before matching.
- Select the smallest independently runnable existing case or subtest whose assertions verify the changed method behavior. Record method → case/subtest → assertion purpose → exact selector/command → expected evidence.
- With no matching existing case, stop the bounded search and skip unit tests for that method. Skip a case inseparable from unrelated cases; report insufficient evidence when relevance cannot be established. Do not compensate by running a file, package or full suite.
- Do not add unit tests by default. New cases need an explicit request, uncovered validation of a requested new/changed method contract, or a proven defect-regression need. State the method, independently defined expected result and reason as a distinct how-to-plan task before implementation. Missing tests, a code edit or coverage alone does not justify additions; SE's existing authorization rules still apply.
- After the final relevant change, verify the selected cases actually executed and passed. A zero exit with no matching tests is not a test pass.
- Broader unit-test runs require an explicit user instruction. Surface a conflicting project-wide unit-test requirement rather than silently expanding scope; this rule does not change separately required build, lint, integration or manual validation.
