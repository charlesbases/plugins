---
name: using-cortex
description: Route engineering tasks to the required Cortex workflow skills and enforce their confirmation gates. Use at the start of feature work, source analysis, planning, implementation, debugging, review, or completion verification.
---

# Using Cortex

## Routing Rules

- Treat a request to verify existing behavior as observation-only unless the user explicitly invokes `$testing`, requests a file change, test-point report, or diagnostic instrumentation. Use `cortex:code-tracing` only when source or configuration evidence is needed, perform only the bounded check requested by the user, then use `cortex:verification-before-completion`.
- Do not add instrumentation, temporary configuration, test artifacts, or source changes to an observation-only verification. When existing evidence is insufficient, state the evidence gap and wait for the user to confirm the scope expansion.
- For a file-writing requirement, select the next stage from Workflow State below. Use `cortex:code-tracing` first only when current behavior, call chains, state, configuration, ownership, impact, or risk are not already proven.
- Use `cortex:brainstorming` to analyze requirements and resolve outcome-affecting unknowns, then `cortex:writing-plans` to present one complete proposal. After the user confirms that proposal, continue with its execution mode; do not request separate design, plan, or mode confirmation.
- During implementation, use `cortex:code-standards` before generating code and for the targeted review after generation.
- For an existing-behavior explanation, use `cortex:code-tracing`.
- For an observed failure with an unproven cause, use `cortex:systematic-debugging`. After it proves the cause, resume the approved execution workflow for an in-scope correction; otherwise use the Proposal Revision rule below before further implementation.
- For a requested diff, commit, pull request, merge request, or code-change review, use `cortex:requesting-code-review`; use `cortex:code-tracing` only when review evidence requires source or runtime-path proof.
- Use `cortex:testing` only when the user explicitly invokes `$testing`.
- For a code-only response that does not write files, use `cortex:code-standards` directly.
- Before claiming work is complete, fixed, implemented, validated, reviewed, ready, or passing, use `cortex:verification-before-completion`.

## Workflow State

- For conclusions about existing repository behavior, use the batch evidence contract in [code-tracing](../code-tracing/references/evidence-contract.md). Ordinary translation, naming, and non-source questions do not need evidence records.
- Register the investigation when source claims begin; reuse current receipts and check at investigation, proposal, and completion boundaries rather than after every tool. Hook coverage applies only to registered investigations.
- The installed plugin's evidence store is an authorized operational record for this workflow, not permission to add files to the user's repository. Recover its pending scope after resume; do not interpret a previous check as current design or write approval.
- If the helper or trusted hooks are unavailable, retain the same source-evidence rules and disclose that programmatic enforcement is unavailable. Do not invent a successful check or repeatedly retry unavailable infrastructure.

- Identify the role and requested scope before routing: an ordinary task uses a controller; an SDD implementer uses a controller-authorized brief; a scoped reviewer remains read-only. Observation-only work and code-only responses do not require an implementation approval.
- Keep one complete proposal containing its revision, requirements, constraints, source context, design decisions, end-to-end call chain, file-by-file changes and pseudocode, impact, validation, and execution mode. Keep the actual user instruction that approved that revision and its file and validation scope. Design remains proposal content, not a separate approval state.
- A clarification answer or choice of direction updates proposal input; it does not authorize implementation. An unambiguous `y` or instruction to implement after the current complete proposal approves that proposal. A request, skill invocation, helper flag, or existing edit cannot establish approval without the presented content and approving instruction.
- Put execution mode in the proposal before confirmation. Default to direct execution; use SDD when the user requests it and tasks satisfy that skill's independence requirements. Include its required recovery artifacts and writes in that proposal. Do not ask for a missing mode after approval.

| Current state for this scope | Next action |
| --- | --- |
| Required user input remains unresolved after evidence triage | Follow Evidence And Clarification, then use brainstorming to ask the necessary specific question. |
| No complete current proposal exists | Analyze and gather source evidence, then use writing-plans to present it. |
| The complete current proposal is presented but unapproved | Discuss, revise, or wait for its confirmation; do not implement. |
| The complete current proposal is approved | Execute its approved scope and validation without another confirmation. |
| A proposal decision is challenged or a new direction is supplied | Apply Proposal Revision before presenting a revision or acting on that direction. |

- Skill re-entry and task recovery preserve an applicable approval. Recover the actual proposal, approving instruction, remaining work, mode, and active testing baseline before resuming. Ask only for genuinely missing approval context; do not infer it from an approval flag or existing edits.
- An SDD implementer receives the approved proposal revision, task scope, and validation authority in its brief. Return missing facts or new decisions to the controller; do not request independent user approval.
- Keep state in task context unless the approved proposal authorizes persistent records. Routing alone does not authorize recovery files.

## Evidence And Clarification

- Before raising a question, reporting a problem, or proposing a change, recover explicit requirements, established constraints, and relevant current evidence. Distinguish confirmed source facts, user requirements, hypotheses, external or runtime facts, and proposed new designs; use this distinction to guide decisions without adding a mandatory report template.
- For source-resolvable uncertainty, use `cortex:code-tracing` on the bounded path needed for the claim. Reuse fresh evidence where it proves that claim. An unread implementation, search miss, missing field, or unverified caller is an evidence gap, not an established defect or a decision to delegate to the user. If authorized tracing cannot close the gap, identify the specific missing path or unavailable evidence.
- Preserve evidence status through analysis, design, and planning. Inferred or unresolved claims may guide investigation or explicitly conditional design discussion, but cannot become confirmed problems or implementation bases. A method's existence does not prove its runtime invocation; a structural checkpoint or a self-declared `confirmed` status does not prove semantic correctness.
- Distinguish whether a problem exists from whether a proposed modification is necessary. Before calling a change required, check the current contract, existing mechanisms, and strongest relevant counterexample within the requested scope. A new design choice must be identified as proposed, with its constraints and observable meaning, rather than described as current behavior or a proven correction.
- An explicit new requirement can justify new behavior without proving an existing defect. Preserve known behavior outside that requirement; do not invent a problem or add an optional behavior change to make a proposal appear necessary.
- Ask only for remaining outcome-affecting input that requires user intent, access, or external or runtime evidence unavailable within the authorized scope. State the verified context, exact gap, and its effect on the decision. Necessary external information may be requested in parallel with independent tracing; continue work that does not depend on the reply. Do not ask the user to choose among causes or behaviors that available source or established context can determine.
- A critical unresolved premise blocks the dependent implementation task from a complete proposal. Continue independent analysis and return source gaps to tracing or genuine decision gaps to clarification; do not silently resolve either by assumption.

## Proposal Revision

- When the user objects, supplies a new direction, or a proposal narrows or changes meaning, pause affected implementation. Recover the original outcome, established constraints, new instruction, and challenged decision.
- Use brainstorming's Revision Evidence workflow: reread the affected implementation context and recheck the necessary caller-to-callee chain before proposing a revision. Previously read facts may be reused only where they actually prove the new decision; an unchanged file hash does not prove a new interpretation.
- Present the changed decision, source basis, feasibility, semantic differences, and impact in one revised complete proposal. An explicit user change to the goal becomes the new requirement; do not silently replace the goal with an implementation suggestion.
- Changes to target or file scope, key calls, branches, state transitions, output or value meaning, contracts or external boundaries, validation scope, or execution mode require approval of the revised complete proposal before those changes are executed. A previous approval does not approve a changed revision.
- Local implementation details that preserve the approved key decisions, execution progress, and test-result updates do not create a new approval gate. For a challenge that leaves the proposal unchanged, explain the evidence; recover whether the user's approval still applies rather than assuming an objection authorizes a replacement.
- Brainstorming, planning, execution, debugging, testing, review, and verification use this single proposal gate. A changed proposal receives one confirmation, never serial design and plan confirmations.

## Divergence Control

- Before the first scoped investigation, classify the request as `Precision`, `Exploration`, or `Ambiguous`. Use `Precision` when the user specifies a target and outcome; use `Exploration` when the user asks to diagnose, compare, design, or choose among alternatives.
- For an `Ambiguous` request, recover available requirements and context under Evidence And Clarification before asking one minimal question when the remaining classification would change the user-visible outcome; otherwise use `Precision`.
- When invoking another Cortex skill, include the active mode. A skill that does not receive one must derive it from the user request.
- Only an explicit user requirement or a proven prerequisite may justify investigation or a proposed task. Writes, tests, and validation must remain within the approved proposal or explicit diagnostic authority. A material tradeoff requires a specific question or proposal decision, never silent scope expansion.
- Treat related findings as observations, not scope. Do not investigate, plan, or fix an observation. Do not surface it in the final response unless it blocks the requested outcome, is an allowed out-of-scope risk, or the user confirms the expansion.
- In `Precision`, optimize for the smallest safe change. Do not propose alternatives, refactors, abstractions, adjacent cleanup, or future-proofing unless source evidence shows that the smallest change has a material safety, compatibility, or user-visible tradeoff; then surface the specific decision needed to complete the proposal.
- In `Exploration`, broaden investigation only until the requested decision is supported. Stop when every acceptance criterion is proven; do not continue quality discovery after that point.

## Gates

- Before any engineering response or action, identify and invoke the Cortex skill required by the routing rules.
- After invoking a Cortex skill, announce its name and purpose in the user's language before following its instructions or taking an action.
- After a stable tool or environment failure, retain a task-local record of the failed mechanism, proven cause, and compatible fallback. Use the fallback on later actions; retry only if workspace, path, permissions, version, or inputs materially change. Keep recoverable tool failures and fallbacks out of user-facing updates unless they block the requested result.
- When a cache-like tool fails because its default external cache path is inaccessible, use a task-scoped environment variable supported by that tool to place the cache under the current project's `.cache/`. Do not persist the environment change; record and reuse the fallback until conditions materially change.
- Every controller-led implementation requires one user confirmation of the complete current proposal. Requirement clarity never removes that gate.
- Analysis, clarification, and writing a proposal in the response do not require a prior design approval. Create a design or plan file only when the user explicitly requests that deliverable; it does not authorize implementation.
- Do not begin implementation writes or mapped tests until the complete proposal is approved. A code-only response is not file-writing implementation.
- Before a write or resumed validation, recover the current approval and applicable scope. Follow Proposal Revision for changed decisions; route evidence gaps to code-tracing, design questions to brainstorming, and proposal construction to writing-plans.

## Scope

Route Cortex work and enforce cross-skill gates. Keep source evidence in `cortex:code-tracing`, design in `cortex:brainstorming`, planning in `cortex:writing-plans`, execution in `cortex:subagent-driven-development` or `cortex:executing-plans`, and completion evidence in `cortex:verification-before-completion`.
