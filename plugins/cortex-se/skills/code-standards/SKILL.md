---
name: code-standards
description: Apply project-grounded, language-neutral quality rules to code being generated or edited.
---

# Code Standards

## Sources Of Standards

- Follow the user's requirements and applicable project instructions first. Read repository formatter, linter, compiler, and test configuration and the conventions in directly related code.
- Use established language and framework conventions where the project has no rule. Check the relevant version's official documentation when an API choice affects semantics, safety, compatibility, or deprecation. Do not invent a language-specific rule or command, and do not build or require a cross-language standards cache.
- Match the project's conventions for comments, documentation, naming, and log language unless the user directs otherwise.

## General Quality

- Keep responsibilities clear and code as simple as the required behavior permits. Prefer names that explain intent and avoid pass-through abstractions without a real responsibility.
- Validate inputs at real external or user-facing boundaries. Respect proven internal invariants; do not conceal impossible states through silent defaults, swallowed errors, or unrelated defensive branches.
- Preserve existing contracts and ownership. Do not add layers, dependencies, concurrency, caches, or global state merely for style. Use `cortex-se:code-tracing` when a boundary or invariant is uncertain.
- For every new or changed API call, check its contract and project compatibility when those facts matter to correctness. Existing local usage is evidence of convention, not proof that an API is suitable for the current change.
- Add comments where intent, constraints, or non-obvious transitions need explanation; do not narrate straightforward code.

## Unit Test Quality

- Apply [Unit Test Selection](../writing-plans/SKILL.md#unit-test-selection); this skill does not authorize new cases. Require a concrete method-level contract, boundary or regression assertion with an independently defined expected result.
- Reject tautological assertions, implementation-mirroring tests and tests added only because code changed or for coverage. Apply the same rule to assertions added to existing files. Do not weaken expectations or delete cases merely to make a run pass.

## Targeted Review

Apply these rules while editing and re-read the changed code with its direct context afterward. Check project-defined formatting and relevant diagnostics. Review only the current change and context needed to judge it; `cortex-se:code-tracing` owns the full behavior-path investigation.

This skill does not grant file-write authority, replace a how-to plan, or establish completion. Return material behavior or scope decisions to `cortex-se:brainstorming`; return scope-preserving corrections to `cortex-se:executing-plans` or the current delegated task.
