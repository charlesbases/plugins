---
name: brainstorming
description: Analyze requirements and prune scope before software design, and resolve material choices when the request is open-ended or ambiguous.
---

# Brainstorming

## When To Use

- Use this skill to establish and prune scope before designing changes, including when the implementation request is clear. Resolve material choices about observable behavior, boundary cases, contracts, compatibility, data ownership, persistence, or scope after [Evidence And Clarification](../using-cortex/SKILL.md#evidence-and-clarification). An uninvestigated source fact is not itself a user decision.
- Do not introduce a design approval gate for a clear development request. The user's instruction authorizes work within its stated scope. If the user asks only for a design, stop after presenting it.
- Use `cortex-se:code-tracing` when current behavior or integration paths affect the decision. Base source claims on its [evidence contract](../code-tracing/references/evidence-contract.md).

## Requirement Analysis And Scope Pruning

1. Recover the requested outcomes, established constraints, existing contracts that must be preserved, and relevant current evidence before considering implementation choices. Apply Evidence And Clarification to both initial designs and revisions. Prune proposed additions, not explicit requirements or required existing behavior.
2. Identify only unknowns that would change the user's observable result or implementation authority. Trace source-resolvable details before deciding that a necessary user question remains.
3. For each candidate addition to behavior or scope, determine whether all requested outcomes and constraints can be satisfied without it. If they can, exclude the addition before design. Otherwise identify the explicit requirement or source-proven prerequisite it satisfies; a hypothetical benefit or risk alone does not establish necessity. Do not generate optional additions merely to populate this assessment.
4. Establish both a claimed problem and why existing mechanisms do not satisfy the required outcome before recommending a correction. An explicit new requirement can justify new behavior without an existing defect.
5. Classify potential actions as `Required`, `Blocker`, or `Observation`. Only Required actions enter the retained scope. Resolve a scope-critical Blocker through tracing or necessary user input before designing the dependent part; independent established scope may proceed. Do not investigate or design an Observation.
6. Keep the retained scope and its requirement or evidence basis in task context. Enter design only after this assessment; do not defer necessity decisions to planning, code generation, or review. No additional report or user confirmation is required.

## Design Within Retained Scope

1. Design only the retained scope. If design reveals a new behavior or scope addition, return it to requirement analysis and scope pruning before designing the affected part. Implementation details that satisfy the retained scope do not create new requirements.
2. Check key premises against state transitions, lifecycle timing, consumers, and the strongest relevant counterexample; a missing field or display requirement alone does not prove a missing capability or equivalent business states.
3. Recommend the smallest approach that satisfies the retained scope, explaining behavior, boundaries, error handling, ownership, impact, and how success can be observed. Label new behavior as proposed; preserve unresolved premises as conditional discussion, not required implementation work. Show alternatives and tradeoffs when the decision genuinely requires them.
4. Consolidate genuinely missing material user choices into one clear decision request. State the verified context, exact gap, and its impact; return source gaps to tracing. Do not ask for separate approval of the subsequent implementation plan or execution mode.

Preserve explicit user constraints. Do not turn an adjacent observation into scope. Once material choices are resolved, hand the retained scope, its requirement or evidence basis, and the design to `cortex-se:writing-plans` when implementation is requested. If planning reveals additional scope, return it to requirement analysis before designing it; scope-preserving details can be resolved during planning or execution.

## Revision Evidence

1. For an objection, new direction, narrowed design, or changed meaning, pause affected implementation and recover the original outcome, established constraints, new instruction, and challenged decision.
2. Perform a new bounded source read of the affected implementation context after the triggering instruction, including when evaluating an alternative without changing the design. Recheck the necessary chain from entry and validation through dispatch, key branches, value/state production and mutation, error paths and lifecycle timing, to the final consumer, output, persistence, or external boundary. Reuse unaffected facts that actually prove the new decision; checking old receipts or unchanged hashes alone does not satisfy this reread.
3. Check reachability, prerequisites, ownership, compatibility, error propagation, and whether the proposed path produces the requested result. Preserve unresolved feasibility gaps rather than accepting a plausible substitute.
4. Map requested outcome → observable result → value/state definition and calculation → lifecycle point → candidate behavior or expression → final consumer. Existing values require source proof; new values require an explicit design definition.
5. A suggested field, expression, or simpler path is a candidate implementation. If it conflicts with the target, show the difference and impact as a material design decision. If the user explicitly changes the target, record that new requirement and evaluate against it.
6. Reapply requirement analysis and scope pruning before revising the affected design. Pass the retained scope, source basis, feasibility, semantic comparison, impact and resolved direction to writing-plans for an updated code-level chain and separate pseudocode. Ask once only for a genuinely unresolved material choice; otherwise continue within the explicit request. Do not add a separate revised-plan or execution-mode confirmation.
