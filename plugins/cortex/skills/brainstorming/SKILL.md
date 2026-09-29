---
name: brainstorming
description: Analyze engineering requirements, clarify outcome-affecting unknowns, and design changes before a complete implementation proposal. Use for ambiguous features, API or configuration changes, compatibility decisions, or revised design directions.
---

# Brainstorming

## Entry And Handoff

- Follow the [single proposal workflow](../using-cortex/SKILL.md#workflow-state). Analyze design input without a separate design approval, then hand it to `cortex:writing-plans` for one complete proposal.
- Preserve valid approved content when continuing the same scope. For objections, new directions, narrowed designs, or changed meanings, apply Revision Evidence below before recommending a revised proposal.
- A delegated implementer returns design decisions and evidence to its controller rather than seeking user approval.
- Keep this phase analytical: do not edit implementation files or generate patches. An explicitly requested design document is a permitted deliverable, not implementation authority.
- Use `cortex:code-tracing` when current behavior, call chains, configuration timing, state branches, or impact affect a decision.

## Requirement And Design Analysis

- Preserve established interface, configuration-format, and scope decisions. Implementation convenience or a missing dependency does not justify changing them.
- Treat an unclear target, location, affected path, output, persistence, validation scope, or temporary-versus-permanent behavior as a necessary question when choosing a default would change the visible outcome.
- Map each proposed change to an explicit requirement or proven prerequisite. Check source-based proposals under the [batch evidence contract](../code-tracing/references/evidence-contract.md); a checkpoint cannot establish user approval or semantic correctness.
- Explain interpreted scope when it can be misread. Default to the smallest repository-owned change that satisfies the requirement; unrelated cleanup or expansion does not enter the proposal without explicit user scope.
- Use the invoking skill's mode. Otherwise an explicit target and outcome is `Precision`; diagnosis, comparison, design, or choice is `Exploration`. Ask about classification only when it changes the visible outcome.
- Classify potential actions as `Required`, `Blocker`, or `Observation`. Include only Required actions, turn a Blocker into the necessary question, and do not investigate an Observation.
- State proven facts and identify unknown inputs, boundary behavior, compatibility, contracts, error handling, ownership, and validation.
- In Precision, recommend the smallest valid change; surface a material tradeoff as a specific question needed for the proposal. In Exploration, compare approaches only when requested or needed for the decision.
- Form design input with impact, constraints, boundary and error behavior, and validation. Hand it to writing-plans without a generic design-confirmation question.

## Revision Evidence

1. On an objection, new direction, narrowed proposal, or changed interpretation, pause affected implementation and recover the original requirement, established constraints, new instruction, and challenged decision.
2. Reread the affected repository-owned implementation context. Reconcile any still-applicable facts with the new decision; a prior receipt or unchanged file alone cannot prove the revised interpretation.
3. Recheck the necessary chain from entry and validation through dispatch, production and mutation of values or state, key branches and errors, lifecycle timing, and final output, persistence, or external boundary. Read newly relevant callers and callees; do not expand into unrelated paths.
4. Check reachability, prerequisites, compatibility, error propagation, ownership, and whether the proposed path can produce the requested result. Mark an unproven feasibility claim as unresolved rather than recommending it.
5. Map requirement → observable result → value/state definition and calculation → lifecycle point → candidate behavior or expression → final consumer. Existing values require source evidence; proposed new values require an explicit design definition.
6. A field, expression, simpler edit, or alternate path suggested by the user is a candidate implementation. If its meaning or effect conflicts with the current target, state the discrepancy and impact as a new design decision; do not describe it as equivalent.
7. If the user explicitly changes the target, record that requirement change and evaluate against the new target. Do not reject it solely because it differs from the original goal.
8. Give writing-plans the changed decision, source basis, feasibility, semantic comparison, and affected implementation and validation. It presents the complete revised proposal for one confirmation before changed work.
9. When the original proposal remains valid, explain the supporting evidence and preserve applicable scope; an unresolved challenged decision is not permission to implement a substitute.

## Scope

Analyze requirements and design, including revision evidence. Keep source traversal in code-tracing, the complete proposal and its confirmation in writing-plans, implementation in the approved execution workflow, and completion evidence in verification-before-completion.
