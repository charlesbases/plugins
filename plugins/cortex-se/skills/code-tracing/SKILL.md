---
name: code-tracing
description: Establish repository behavior and complete relevant call paths from source, configuration, and runtime evidence before development conclusions.
---

# Code Tracing

## Evidence Standard

- Define the claim and the conditions that would prove or reject it. Read relevant implementation, not only search results, symbol metadata, or a downstream symptom.
- Trace every repository-owned link needed for the claim: entry point, validation and dispatch or registration, caller-to-callee values, branches, initialization and state mutation, configuration timing, lifecycle callbacks, errors, side effects, and the resulting observable boundary. Include multiple entry or failure paths when the conclusion depends on them.
- For a behavior-changing proposal, establish the complete relevant existing path before planning the change. When a path is new, establish the integration points and state that the proposed segment does not exist yet.
- An available API does not prove the current project calls it. A text-search miss does not exclude dynamic dispatch. Reconcile the strongest relevant counterexample before confirming a claim.
- Stop at framework, generated, vendor, or standard-library internals when their public contract and local use suffice. Do not impose a fixed function count, path count, or hop limit on the relevant repository-owned chain.

## Tools And Batches

- Use a language-aware index or language server for known symbols when available; use repository search to discover entry points, configuration, registration, generated paths, and dynamic calls. Fall back to source search and reading when indexing is unavailable or incomplete.
- Use [batch evidence checkpoints](references/evidence-contract.md) for repository conclusions and change bases. The helper's per-range and per-batch limits control reading size, not the number of batches allowed for a named relevant gap. A receipt proves a range was returned, not that its content supports a claim.
- Determine actual file lengths and execution-relevant ranges before a batch read; do not use guessed end lines. If the helper cannot run because of sandbox permissions, missing runtime, or unavailable hook context, use direct source reading under the same evidence standard and report that programmatic enforcement was unavailable. Do not repeatedly retry unchanged infrastructure or weaken the conclusion to hide the gap.
- Preserve uncertainty when runtime identity, external configuration, timing, or an unexamined branch prevents a conclusion. State the missing evidence instead of inferring it.

## Output

Separate confirmed facts, inferences, and open questions. Cite the key source ranges and show the relevant call path without pasting large files. This skill is read-only. For a requested behavior change, hand the established path to `cortex-se:writing-plans`; for a read-only question, return the supported conclusion to the calling workflow.
