---
name: requesting-code-review
description: Review a working-tree diff, commit, pull request, merge request, or explicit Git range for defects, regressions, and production risk. Use when a code-change review is requested.
---

# Requesting Code Review

## Review Dispatch

- When this skill is invoked by a controller and a fresh subagent is available, dispatch one read-only reviewer with the review target, confirmed requirements, base and head revisions when applicable, and any task-scoped artifact paths it may read. Do not pass controller history.
- A reviewer that receives a scoped review does not dispatch another reviewer; it follows the remaining sections of this skill.
- The controller rechecks the diff and source evidence cited by every formal finding before presenting it.
- A read-only reviewer returns its complete review result to the controller and never writes controller-owned artifacts. The controller owns any review-result persistence.
- When no fresh subagent is available, perform the review directly under the same evidence and output rules.

## Review Gate

- Treat code review as read-only. Do not edit files, generate patches, or run Git mutation commands.
- Do not run tests, lint, builds, or other validation outside the confirmed review scope.
- Do not make a formal finding without a realistic trigger, concrete impact, and diff and source evidence.
- Do not treat unclear requirements, unproven behavior, or style preference as a formal finding.
- Do not require new tests or expanded validation unless a confirmed requirement or repository policy requires them.

## Review Scope

1. Identify whether the target is a working tree, commit, pull request, merge request, or explicit Git range.
2. Ask for base and head revisions when the range is unclear.
3. For `git diff <base> <head>`, inspect `git diff --stat <base> <head>` and `git diff --name-status <base> <head>` before selected files.
4. For a working-tree review, inspect `git status --short`, staged changes, and unstaged changes separately.
5. Inspect relevant per-file diffs only after confirming the complete scope.
6. Identify generated files, dependencies, lock files, and vendored files; do not perform routine line-by-line review unless the change itself creates direct risk.

## Evidence Review

1. Read changed implementation and directly relevant repository-owned code.
2. Compare changes with confirmed requirements, plans, contracts, and behavior that must remain unchanged.
3. Use `cortex:code-tracing` before a finding that depends on entry points, reachability, initialization, lifecycle, state, configuration timing, data flow, or error propagation.
4. Check compatibility, ownership, error handling, state transitions, configuration semantics, and validation evidence when relevant.
5. Stop tracing when evidence proves or rejects the conclusion.

## Completeness Gate

- Inspect all changed files and every repository-owned call chain needed to evaluate changed behavior after the scope is confirmed.
- Reconcile confirmed requirements, changed behavior, affected entry points, state transitions, configuration paths, and validation evidence before output.
- Return all supported findings in one response. Add a later finding only when review scope, source evidence, runtime evidence, or confirmed requirements changed, and state that new evidence.

## Finding Classification

Classify every concern as exactly one of:

- Formal finding: A proven reachable path causes incorrect behavior, regression, security risk, data risk, or concrete operational impact.
- Open question: Required behavior, ownership, contract, or relevant reachability cannot be proven from available requirements and source.
- Residual risk: Validation or evidence is incomplete, but incorrect behavior is not proven.

For every formal finding, provide changed file and line, trigger path, impact, key diff and source evidence, and a concrete solution that preserves confirmed requirements and ownership boundaries. Use pseudocode only for a non-trivial fix and derive it from proven entry points, state flow, and existing abstractions. Do not propose a solution for an open question; state the missing evidence or decision.

## Output

- Present formal findings first, ordered by severity.
- Follow findings with open questions and then residual risks or validation gaps.
- State clearly when no formal finding is proven, including reviewed scope and remaining risk.
- Keep the summary secondary and concise.
- Do not paste large diffs, source files, logs, or test output.

## Handoff

- Use `cortex:verification-before-completion` before claiming review or work is complete, ready, or passing.
- Return to `cortex:code-tracing` when evidence is insufficient.
- Return feedback with the current complete proposal and approval scope when applicable. Changes to approved decisions follow [Proposal Revision](../using-cortex/SKILL.md#proposal-revision): reread affected context, recheck the necessary chain and semantics with brainstorming, then present one revised complete proposal through writing-plans.
- Proven corrections within an approved implementation's unchanged key decisions, file and validation scope return to its execution workflow without another confirmation. A standalone read-only review never authorizes implementation.
- Do not use `cortex:code-standards` to review existing user changes.

## Scope

Define review scope, evidence standards, and output. Keep source analysis in `cortex:code-tracing`, planning in `cortex:writing-plans`, implementation in `cortex:executing-plans`, and completion evidence in `cortex:verification-before-completion`.
