---
name: brainstorming
description: Analyze engineering requirements, clarify outcome-affecting unknowns, and design changes before a complete implementation proposal. Use for ambiguous features, API or configuration changes, compatibility decisions, or revised design directions.
---

# Brainstorming

## Entry And Handoff

- Follow the [request-scoped workflow](../using-cortex/SKILL.md#workflow-state). Complete requirement analysis and scope pruning before design, then hand the retained scope and its design to `cortex-se:writing-plans` for one complete plan. This ordering adds no separate approval.
- Preserve valid authorized content when continuing the same scope. For objections, new directions, narrowed designs, or changed meanings, apply Revision Evidence below before recommending a revised proposal.
- A delegated implementer returns design decisions and evidence to its controller rather than seeking user approval.
- Keep this phase analytical: do not edit implementation files or generate patches. An explicitly requested design document is a permitted deliverable, not implementation authority.
- Use `cortex-se:code-tracing` when current behavior, call chains, configuration timing, state branches, or impact affect a decision.

## Requirement Analysis And Scope Pruning

Apply the mandatory [Current Task Context](../using-cortex/references/task-context.md) reconciliation procedure before considering a new direction, including mid-turn input. Resolve outcome-affecting conflicts in the conversation, then save only clarified user requirements and constraints before dependent design. Check the whole design against them before presenting its complete plan.

- Apply [Evidence And Clarification](../using-cortex/SKILL.md#evidence-and-clarification) to questions and design premises in both initial designs and revisions.
- Preserve established interface, configuration-format, and scope decisions. Implementation convenience or a missing dependency does not justify changing them.
- Establish the requested outcomes, constraints, and existing contracts that must be preserved before considering implementation choices. Prune proposed additions, not explicit requirements or required existing behavior.
- Treat an unclear target, location, affected path, output, persistence, validation scope, or temporary-versus-permanent behavior as an evidence gap first. Recover established context and trace source-resolvable details before deciding whether a necessary user question remains.
- For each candidate addition to behavior or scope, determine whether all requested outcomes and constraints can be satisfied without it. If they can, exclude the addition before design. Otherwise identify the explicit requirement or source-proven prerequisite it satisfies; a hypothetical benefit or risk alone does not establish necessity. Do not generate optional additions merely to populate this assessment.
- For a claimed correction, establish both the current problem and why existing mechanisms do not satisfy the required outcome; an explicit new requirement does not need evidence of an existing defect. Check source-based conclusions under the [batch evidence contract](../code-tracing/references/evidence-contract.md); a checkpoint cannot establish user approval or semantic correctness.
- Inherit the invoking skill's mode. When invoked directly without one, use the single [Divergence Control](../using-cortex/SKILL.md#divergence-control) definition; do not independently reinterpret its categories or request a mode confirmation.
- Classify potential actions as `Required`, `Blocker`, or `Observation`. Only Required actions enter the retained scope. Resolve a scope-critical Blocker through tracing or necessary user input before designing the dependent part; independent established scope may proceed. Do not investigate or design an Observation.
- Keep scope analysis and evidence in the conversation or the requested proposal. Context contains only the user's explicit effective requirements and constraints. Enter design only after this assessment; do not defer necessity decisions to planning, code generation, or review. No additional report or user confirmation is required.

## Design Within Retained Scope

- Read and apply the shared [Architecture Design](../../references/architecture-design.md) policy when designing the retained scope. Use its applicability, decision and exception rules; hand the key decisions and tradeoffs to writing-plans. Keep architecture definitions in that policy rather than duplicating them here.

- Design only the retained scope. If design reveals a new behavior or scope addition, return it to requirement analysis and scope pruning before designing the affected part. Implementation details that satisfy the retained scope do not create new requirements.
- Check key design premises against state transitions, lifecycle timing, consumers, and the strongest relevant counterexample before recommending a change. A missing field or display requirement alone does not prove a missing capability or equivalent business states. Preserve unresolved premises as conditional discussion, not required implementation work.
- Explain interpreted scope when it can be misread. Default to the smallest repository-owned change that satisfies the requirement; unrelated cleanup or expansion does not enter the proposal without explicit user scope.
- State proven facts and identify unknown inputs, boundary behavior, compatibility, contracts, error handling, ownership, and validation.
- In Precision, recommend the smallest valid change; surface a material tradeoff as a specific question needed for the proposal. In Exploration, compare approaches only when requested or needed for the decision.
- Hand the retained scope, its requirement or evidence basis, and the design with impact, constraints, boundary and error behavior, and validation to writing-plans without a generic design-confirmation question.

## Revision Evidence

Apply [Current Task Context](../using-cortex/references/task-context.md) before this sequence: distinguish supplements, replacements, withdrawals, questions and unresolved conflicts. Preserve unaffected user requirements and constraints. Clarify conflicts in the conversation before replacing affected requirements. Describe superseded designs and pending implementation cleanup in the proposal or conversation, not context.

1. On an objection, new direction, narrowed proposal, or changed interpretation, pause affected implementation and recover the original requirement, established constraints, new instruction, and challenged decision.
2. Reread the affected repository-owned implementation context. Reconcile any still-applicable facts with the new decision; a prior receipt or unchanged file alone cannot prove the revised interpretation.
3. Recheck the necessary chain from entry and validation through dispatch, production and mutation of values or state, key branches and errors, lifecycle timing, and final output, persistence, or external boundary. Read newly relevant callers and callees; do not expand into unrelated paths.
4. Check reachability, prerequisites, compatibility, error propagation, ownership, and whether the proposed path can produce the requested result. Mark an unproven feasibility claim as unresolved rather than recommending it.
5. Map requirement → observable result → value/state definition and calculation → lifecycle point → candidate behavior or expression → final consumer. Existing values require source evidence; proposed new values require an explicit design definition.
6. A field, expression, simpler edit, or alternate path suggested by the user is a candidate implementation. If its meaning or effect conflicts with the current target, state the discrepancy and impact as a new design decision; do not describe it as equivalent.
7. If the user explicitly changes the target, record that requirement change and evaluate against the new target. Do not reject it solely because it differs from the original goal.
8. Reapply requirement analysis and scope pruning before revising the affected design. Explain what changes, which existing constraints remain binding, how the complete affected flow still achieves the outcome, and which prior decisions lose their basis. Give writing-plans the retained scope, changed decision, source basis, feasibility, semantic comparison and explicit retain/adjust/remove artifact scope. It presents one self-contained current plan before affected implementation. Ask once only for a material unresolved user decision; otherwise continue within the explicit implementation request.
9. When the original proposal remains valid, explain the supporting evidence and preserve applicable scope; an unresolved challenged decision is not permission to implement a substitute.

## Scope

Analyze requirements and design, including revision evidence. Keep source traversal in code-tracing, the complete plan in writing-plans, implementation in the selected execution workflow, and completion evidence in verification-before-completion.
