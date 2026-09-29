---
name: requesting-code-review
description: Review a requested code change or a delegated implementation for concrete defects, regressions, and residual risk.
---

# Requesting Code Review

## Scope

- Use this skill when the user requests review or a user-requested delegated workflow requires it. Review is read-only; do not edit files or run Git mutation commands.
- Identify the exact working tree, commit, pull request, merge request, or Git range. For a working tree, inspect status and staged and unstaged changes. For a range, confirm base and head, then inspect the complete changed-file list before selected diffs.
- Review all changed files and the repository-owned paths needed to assess their behavior. Use `cortex-se:code-tracing` for reachability, state, configuration, lifecycle, or error-propagation claims, including its evidence checkpoint.

## Findings

- Compare the change with the requested outcome, applicable how-to plan, project contracts, and validation evidence.
- A formal finding needs a realistic trigger, concrete impact, changed location, and supporting diff and source evidence. Do not report style preference or an unproven scenario as a defect.
- Classify remaining concerns as open questions when requirements or reachability are unknown, or residual risks when validation is incomplete without a proven defect.
- Give a concrete correction for each formal finding that preserves the requested outcome. State the missing evidence for an open question.

Present supported findings by severity, followed by open questions and residual risks. If no defect is proven, state the reviewed scope and any validation gap. Do not automatically dispatch another reviewer; delegation requires the user's request or the already selected delegated workflow. Use `cortex-se:verification-before-completion` before claiming the review is complete.
