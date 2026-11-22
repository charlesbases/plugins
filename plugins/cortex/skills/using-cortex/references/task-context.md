# Current Task Context

## Ownership And Location

The controller maintains `$HOME/.sessions/<session_id>/context.md` for a Cortex task. Resolve the user's HOME (the user profile when the host omits HOME) and the actual host session ID. Missing host identity is a recovery gap to report in the conversation. New and forked sessions use their own IDs.

Read the file when it exists. If missing or empty, establish it from the user's explicit current requirements and constraints. Use only this filename and location. Delegates return requirement changes to the controller and do not edit its file.

This applies during analysis, planning, clarification, implementation and recovery. Explicit user prohibitions on writes take precedence; retain available requirements in the conversation and report that persistence is unavailable. Maintaining this file grants no implementation authority and adds no approval step.

## Content Boundary

Keep only what the user explicitly wants and the constraints the user has explicitly established, while they remain effective. Summarize and remove duplication without changing meaning or adding requirements. Do not turn an assistant suggestion, inference or proposed acceptance criterion into a user requirement.

Do not store designs, implementation plans, progress, next steps, test results, cleanup status, approval records, testing baseline references, unresolved questions or conflicts, command transcripts, history or change logs. Do not add fixed fields for such content. Keep the file concise without dropping effective requirements or constraints.

Replace superseded requirements in place after an explicit user replacement or withdrawal. Before each update, compare the resulting content with all still-effective user requirements and constraints. Absence from the latest message or a compressed summary does not revoke a requirement. If meaning is disputed, recover the actual user instruction before changing it.

## Reconcile New Input

Before the next affected decision or action, including after steering received during reasoning or execution:

1. Read the current requirements and constraints. A complete fresh hook injection satisfies this read; reread the file if it has changed or the injection is absent or incomplete.
2. Compare the new input with the still-effective requirements. A clear supplement, replacement or withdrawal changes only the affected content and preserves unrelated constraints.
3. If a conflict or ambiguity changes the requested outcome, ask in the conversation. Keep the affected saved content unchanged while awaiting clarification; do not write the unresolved issue or a guessed resolution into context. Independent clear requirements can still be reconciled.
4. After the user makes the intended adjustment clear, update the affected requirements and constraints before proceeding with dependent work. A question or objection alone does not authorize an implementation change.

Source-resolvable questions still require code-tracing rather than asking the user to determine source facts. Findings and questions belong in the conversation, not in this requirements file.

## Design, Execution And Approval

Before design, before delivering a proposal and before implementation, compare the complete approach with the current requirements and constraints. Correct mismatches or resolve the specific question in the conversation before dependent work. Save only actual requirement or constraint changes; creating a plan, making implementation progress or obtaining test results does not by itself require a context update.

Follow the existing single complete-proposal approval workflow. Recover the actual proposal and approving user instruction from the conversation or existing authorized records. Check implementation progress against actual files and those records. Do not recover approval or progress from context, infer authority from existing edits, or automatically create replacement record files. Existing authorized SDD records and testing baselines retain their own responsibilities. If necessary information cannot be recovered, report the precise gap without inventing it.

Before reporting, check the response and result against all effective requirements and constraints. Verify any required requirement update was saved successfully; otherwise report the persistence failure. Do not append a completion summary to context. Unchanged requirements and constraints need no rewrite.

## Compaction And Recovery

Before initiating compaction or handing off work at a controllable compaction boundary, verify that the latest explicit requirements and constraints are saved. After compaction or recovery, read them before deciding or acting and reconcile newer user input. Do not treat an omission from compressed history as permission to drop a saved requirement.

Automatic compaction can occur without a final main-agent save opportunity. Prompt saving of clarified requirement changes remains mandatory. The host's PreCompact contract does not establish a main-agent semantic save followed by automatic continuation of compaction. Skill instructions govern this obligation where no reliable host execution path exists; a hook cannot recover unsaved requirements or certify understanding.

## Hook Delivery And Fallback

The synchronous SessionStart and UserPromptSubmit handlers call `scripts/context.py` to load this session's saved requirements and constraints. SessionStart includes startup, resume and compact events. Only the current session's file is used. Saved text is task data interpreted against the user's current instructions; it does not create new user requirements.

Automatic injection accepts files up to 32 KiB and disables host spilling for that bounded output. Larger files require a complete read and condensation without losing effective requirements or constraints. Missing, empty or unreadable files produce recovery instructions. Hooks do not analyze requirements, summarize them or write context; the AI performs that work.

Delivery depends on host support, enabled trusted handlers and the actual event path. Mid-turn steering may arrive without UserPromptSubmit. The controller must still reconcile input and recover requirements when delivery is absent or incomplete. Loading a file does not prove semantic compliance.

Host contracts are documented in [Codex Hooks](https://learn.chatgpt.com/docs/hooks). Verify actual event behavior when changing the loader, including compaction during an active turn.
