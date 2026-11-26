---
name: brainstorming
description: Analyze engineering requirements, clarify outcome-affecting unknowns, and design changes before a complete implementation proposal. Use for ambiguous features, API or configuration changes, compatibility decisions, or revised design directions.
---

# Brainstorming

## Entry And Handoff

- Follow the [single proposal workflow](../using-cortex/SKILL.md#workflow-state). Complete requirement analysis and scope pruning before design, then hand the retained scope and its design to `cortex:writing-plans` for one complete proposal. This ordering adds no separate approval.
- Preserve valid approved content when continuing the same scope. For objections, new directions, narrowed designs, or changed meanings, apply Revision Evidence below before recommending a revised proposal.
- A delegated implementer returns design decisions and evidence to its controller rather than seeking user approval.
- Keep this phase analytical: do not edit implementation files or generate patches. An explicitly requested design document is a permitted deliverable, not implementation authority.
- Use `cortex:code-tracing` when current behavior, call chains, configuration timing, state branches, or impact affect a decision.

## Requirement Analysis And Scope Pruning

Begin design and revisions with [Current Task Context](../using-cortex/references/task-context.md). The SessionStart and UserPromptSubmit hooks supply the saved requirements and constraints. If the snapshot is missing, incomplete or stale, read the current session's `context.md` directly and apply its recovery rules. Reconcile the latest user message, including mid-turn input, before the next affected decision. Clarify conflicts with the user and save only explicit requirement changes. Use the resulting requirements to check the whole design before its single proposal confirmation.

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
- For each effective requirement, identify an observable result and a relevant scenario that could disprove the design. Trace the actual pre-change behavior of affected consumers, including relevant defaults, exits, failures and side effects. Specify the intended differences and the behavior those consumers still rely on. Revise a design that fails this comparison before handoff.
- Choose the smallest repository-owned change that delivers those results. Justify material new branches, state, dependencies or abstractions against that alternative. For hot paths, use input size, call frequency and repeated work to assess time, space, copying or allocation costs. Identify assumptions and how material predictions can be checked. Keep the explanation proportional to the decision.
- Explain the scope when it can be misread. State proven facts, unknown inputs, compatibility, contracts, ownership and validation. Unrelated cleanup or expansion requires explicit user scope.
- In Precision, recommend the smallest valid change; surface a material tradeoff as a specific question needed for the proposal. In Exploration, compare approaches only when requested or needed for the decision.
- Hand the retained scope, its requirement or evidence basis, and the design with impact, constraints, boundary and error behavior, and validation to writing-plans without a generic design-confirmation question.

## Revision Evidence

Apply [Current Task Context](../using-cortex/references/task-context.md) before this sequence: distinguish supplements, replacements, withdrawals, questions and unresolved conflicts. Preserve unaffected user requirements and constraints. Clarify conflicts in the conversation before replacing affected requirements. Describe superseded designs and pending implementation cleanup in the proposal or conversation, not context.

When revising an implemented version, settle two decisions before including changes in the proposal:

- **Business behavior.** Identify the behavior being replaced and trace any independent consumers of its logic, interfaces or storage. Include superseded paths in the removal scope and retain shared behavior that still has a consumer. Reuse the existing representation when it expresses the new meaning. Ask if replacement versus coexistence is still a business decision.
- **Historical data.** Trace old writers through storage or transport to the new reader, including what the values mean. Use the normal new path when old data cannot reach it or is already handled correctly. Honor an existing explicit compatibility choice or commitment. For a genuine undecided case, describe the affected records and consequences, ask the user once how to handle them, and continue independent work. After a reasonable opportunity to reply, use the **new format only** as the proposal default if no answer has arrived. Identify the unsupported records. Include a fallback, dual reads or writes, or a migration tool only if the selected strategy requires it. Deleting, overwriting or migrating stored data requires explicit authority for that operation. Reconcile a later answer under the existing proposal revision rule.

1. On an objection, new direction, narrowed proposal, or changed interpretation, pause affected implementation and recover the original requirement, established constraints, new instruction, and challenged decision.
2. Reread the affected repository-owned implementation context. Reconcile any still-applicable facts with the new decision; a prior receipt or unchanged file alone cannot prove the revised interpretation.
3. Recheck the necessary chain from entry and validation through dispatch, production and mutation of values or state, key branches and errors, lifecycle timing, and final output, persistence, or external boundary. Read newly relevant callers and callees; do not expand into unrelated paths.
4. Check reachability, prerequisites, compatibility, error propagation, ownership, and whether the proposed path can produce the requested result. Mark an unproven feasibility claim as unresolved rather than recommending it.
5. Map requirement → observable result → value/state definition and calculation → lifecycle point → candidate behavior or expression → final consumer. Existing values require source evidence; proposed new values require an explicit design definition.
6. A field, expression, simpler edit, or alternate path suggested by the user is a candidate implementation. If its meaning or effect conflicts with the current target, state the discrepancy and impact as a new design decision; do not describe it as equivalent.
7. If the user explicitly changes the target, record that requirement change and evaluate against the new target. Do not reject it solely because it differs from the original goal.
8. Reapply requirement analysis and scope pruning before revising the affected design. Explain what changes, which existing constraints remain binding, how the complete affected flow still achieves the outcome, and which prior decisions lose their basis. Give writing-plans the retained scope, changed decision, source basis, feasibility, semantic comparison, and explicit retain/adjust/remove artifact scope. It presents one self-contained current proposal for confirmation before changed implementation; do not require the user to assemble historical versions.
9. When the original proposal remains valid, explain the supporting evidence and preserve applicable scope; an unresolved challenged decision is not permission to implement a substitute.

## Scope

Analyze requirements and design, including revision evidence. Keep source traversal in code-tracing, the complete proposal and its confirmation in writing-plans, implementation in the approved execution workflow, and completion evidence in verification-before-completion.
