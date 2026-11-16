---
name: code-standards
description: Apply quality rules to code generated or edited by Codex. Use immediately before and after code generation; for filesystem changes, use only within an approved execution plan.
---

# Code Standards

## Workflow Boundary

- Do not authorize file writes, decide design approval, define implementation plans, or report completion in this skill.
- Apply these rules before generation and review only code generated or edited in the current task plus the direct context required to assess it after generation.
- A code-only response can use this skill directly. Controller-led file writing requires the approved complete proposal and its scope and mode under using-cortex's single proposal workflow. Missing content returns to analysis and planning; an unapproved proposal waits for its one confirmation. This skill never adds a design, plan, or mode gate.
- An SDD implementer instead uses its controller-issued brief bound to an approved plan revision, task scope, and validation scope. Return missing facts or any design or planning decision below to the controller, without loading the full parent plan or seeking separate user approval.
- With valid authority already established, apply the quality rules and return to the current execution step. Reloading this skill does not restart design, planning, or confirmation.

## General Standards

- Prefer early returns over deeply nested control flow.
- Do not change behavior, data flow, ownership, public contracts, or architecture solely to satisfy style preferences.

## Unit Test Quality

- Apply these rules to unit tests authorized by the confirmed plan under [Unit Test Selection](../writing-plans/SKILL.md#unit-test-selection); this skill does not authorize adding tests.
- Require a concrete method-level behavior, boundary, contract, or defect-regression assertion with an independently defined expected result. Prefer existing suitable tests.
- Do not create tautological assertions, tests that merely restate implementation steps, or tests added only to demonstrate that code changed or increase coverage. Do not automatically add tests for logging, formatting, naming, or other low-impact edits without a confirmed behavioral validation need.
- Review new cases and assertions added to existing test files by the same quality criteria. Do not silently change existing expectations or delete tests to make a run pass.

## Architecture Policy

- Before generation, including a code-only response, read and apply the shared [Architecture Design](../../references/architecture-design.md) policy within the authorized scope. It owns architecture principles, boundary validation, abstraction and wrapper rules, and language-specific architecture supplements; do not define parallel rules here.
- Use the same policy for the after-generation review and preserve the approved design decisions and exceptions. Use code-tracing when applicability depends on an unproven invariant, consumer or ownership boundary. Unconfirmed behavior or design changes return to brainstorming or writing-plans under the existing proposal workflow.

## Naming, Comments, And Logs

- Use names that express domain intent; avoid vague names such as `data`, `info`, `manager`, `helper`, `util`, and `common` unless the existing domain uses them precisely.
- Avoid unclear abbreviations; preserve established initialisms such as `ID`, `HTTP`, `URL`, and `API`.
- Keep local names short only when scope and meaning are obvious.
- Add comments for intent, constraints, non-obvious behavior, required public documentation, or complex method structure; do not restate code.
- Write code comments in Chinese. Write log messages in English. Change either language only when the user explicitly requests it.
- Add a package comment for a new package API.
- Add GoDoc comments to every new or materially changed exported Go type, const, var, function, and method.
- Add a separate comment to an exported struct field only when the type GoDoc does not clearly explain that field's meaning.
- Do not add a redundant exported struct-field comment merely because the field is exported when the type GoDoc already explains its meaning.
- Add comments to unexported Go declarations only when intent, constraints, or behavior are not obvious.
- For complex Go code, document only non-obvious state transitions, business constraints, concurrency, configuration timing, error recovery, or data transformations. Keep public GoDoc focused on the caller-facing contract; use internal comments for implementation constraints that callers do not need to know.

## Language-Specific Rules

- For language-specific rules not defined by repository configuration or local conventions, apply only established community idioms; do not invent language-specific checks.
- When adding or replacing a standard-library, framework, or third-party API call, treat existing repository usage as evidence of local convention and compatibility constraints, not as proof that the API is still the best choice.
- When the API choice affects semantics, safety, compatibility, determinism, concurrency behavior, allocation cost, performance, or long-term maintenance, verify the repository language version, dependency version, local convention, or official documentation before choosing.
- When more than one API can satisfy the same requirement, choose the current non-deprecated API that best matches the confirmed behavior and repository constraints.
- Preserve an existing API choice only when changing it would violate confirmed behavior, compatibility, repository policy, or the approved implementation scope.
- Do not use deprecated, legacy, reflection-heavy, global-state, or weaker-semantic APIs when a current, typed, safer, or more semantically precise alternative is available within the confirmed scope.
- Do not select or run a formatter in this skill. Formatter commands and fallback behavior belong to the selected execution workflow.
- Require changed Go source to follow idiomatic Go, Go Code Review Comments, Google Go Style principles, explicit error handling, and standard Go formatting.
- Avoid `panic` outside initialization or truly unrecoverable programmer errors.

## After-Generation Review

- Re-read generated or edited code and its direct context after generation; for changed Go source, do this after the formatting step.
- Check applicable rules in this skill and the shared Architecture Design policy referenced above, including approved design decisions and exceptions. Keep naming, comments, language-specific quality and formatting checks in this skill.
- Verify every newly introduced API call against the API selection rules in this skill.
- Do not repeat repository-wide analysis or retrace a call chain solely for this review.
- Use `cortex:code-tracing` only when the review cannot determine whether an invariant, boundary, wrapper responsibility, or ownership rule applies.
- When correction changes approved behavior, ownership, contracts, data flow, or architecture, return to the complete-proposal revision workflow, including affected context reread and chain and semantic rechecks.

## Scope

Define generated-code quality constraints and targeted generated-code review. Keep file-write authorization and implementation actions in the selected execution workflow or the controller-authorized SDD task, source analysis in `cortex:code-tracing`, and completion evidence in `cortex:verification-before-completion`.
