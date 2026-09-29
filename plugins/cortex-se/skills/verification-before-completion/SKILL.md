---
name: verification-before-completion
description: Check fresh source and validation evidence before reporting a software change, analysis, fix, or review as complete.
---

# Verification Before Completion

## Evidence

- Map each final claim to the smallest evidence that can establish it: current source and diff, targeted test, build or static check, reproducible runtime observation, or a requested review.
- For repository conclusions, finish the relevant [evidence checkpoint](../code-tracing/references/evidence-contract.md) after the final relevant change. A structurally valid checkpoint does not prove the source claim or runtime result; reconcile the actual path and counterexamples.
- For behavior changes, compare the final entry-to-boundary path with the how-to plan's chain and separate file-level logic. Verify actual value/state definitions, calculations/expressions, lifecycle and final consumer against the requested outcome; successful tests or structural checks alone do not establish semantic equivalence.
- Re-read changed artifacts and relevant context after the last change. Read complete command results and exit status. Distinguish a passed check from a check not run, failed, or blocked by the environment.

## Effective Validation

- Follow project-defined required checks and the requested scope. For unit tests use [Unit Test Selection](../writing-plans/SKILL.md#unit-test-selection): exact method-to-case mapping, independently selectable cases and fresh evidence that those cases ran. Report absent, non-isolatable or unproven matches as skipped or insufficient evidence, not passing; do not add cases by default.
- Select the level that observes the changed behavior. A unit test of an internal helper does not by itself establish an end-to-end claim. A successful build or lint run does not by itself prove runtime behavior.
- Do not run unrelated full suites or invent language-specific formatter and lint commands. Use project configuration and the requested scope. If a required check fails, use `cortex-se:systematic-debugging`; if it cannot run, report the limitation without claiming success.
- Confirm the targeted `cortex-se:code-standards` review of generated or edited code and any project-defined formatting completed.
- Use `cortex-se:code-tracing` for a remaining source-path gap. Return incomplete standalone code review to `cortex-se:requesting-code-review` without creating implementation authority. Return incomplete authorized implementation or its generated-code quality check to `cortex-se:executing-plans` or the current delegated workflow. A new material outcome or scope decision returns to `cortex-se:brainstorming`.

## Report

State what was completed, which checks ran and their actual results, and any incomplete work or evidence gap. Use `passed`, `failed`, `not run`, or `insufficient evidence` accurately.
