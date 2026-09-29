---
name: brainstorming
description: Resolve material design choices when a software request is open-ended or ambiguous, or when the user asks for a design.
---

# Brainstorming

## When To Use

- Use this skill when the request leaves a material choice about observable behavior, boundary cases, contracts, compatibility, data ownership, persistence, or scope, or when the user asks for design work.
- Do not introduce a design approval gate for a clear development request. The user's instruction authorizes work within its stated scope. If the user asks only for a design, stop after presenting it.
- Use `cortex-se:code-tracing` when current behavior or integration paths affect the decision. Base source claims on its [evidence contract](../code-tracing/references/evidence-contract.md).

## Design The Change

1. State the requested outcome and relevant proven project context.
2. Identify only unknowns that would change the user's observable result or implementation authority.
3. Recommend an approach that explains behavior, boundaries, error handling, ownership, impact, and how success can be observed. Show alternatives and tradeoffs when the decision genuinely requires them.
4. Consolidate related open choices into one clear decision request. Do not ask for separate approval of the subsequent implementation plan or execution mode.

Preserve explicit user constraints. Do not turn an adjacent observation into scope. Once the user resolves a material choice, use `cortex-se:writing-plans` when implementation is requested. If planning reveals a new material design decision, return here for that decision; scope-preserving details can be resolved during planning or execution.

## Revision Evidence

1. For an objection, new direction, narrowed design, or changed meaning, pause affected implementation and recover the original outcome, established constraints, new instruction, and challenged decision.
2. Perform a new bounded source read of the affected implementation context after the triggering instruction, including when evaluating an alternative without changing the design. Recheck the necessary chain from entry and validation through dispatch, key branches, value/state production and mutation, error paths and lifecycle timing, to the final consumer, output, persistence, or external boundary. Reuse unaffected facts that actually prove the new decision; checking old receipts or unchanged hashes alone does not satisfy this reread.
3. Check reachability, prerequisites, ownership, compatibility, error propagation, and whether the proposed path produces the requested result. Preserve unresolved feasibility gaps rather than accepting a plausible substitute.
4. Map requested outcome → observable result → value/state definition and calculation → lifecycle point → candidate behavior or expression → final consumer. Existing values require source proof; new values require an explicit design definition.
5. A suggested field, expression, or simpler path is a candidate implementation. If it conflicts with the target, show the difference and impact as a material design decision. If the user explicitly changes the target, record that new requirement and evaluate against it.
6. Pass the source basis, feasibility, semantic comparison, impact and resolved direction to writing-plans for an updated code-level chain and separate pseudocode. Ask once only for a genuinely unresolved material choice; otherwise continue within the explicit request. Do not add a separate revised-plan or execution-mode confirmation.
