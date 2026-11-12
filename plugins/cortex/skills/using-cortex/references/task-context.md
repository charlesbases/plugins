# Current Task Context

## Location And Ownership

For a controller handling a Cortex task, maintain one current snapshot at `$HOME/.sessions/<session_id>/context.md`. Resolve HOME from the user environment and use the actual session ID supplied by the host. Record both the resolved path and session ID. The path does not depend on cwd, a Git root, the target repository, or a tool's working directory. If the host ID is unavailable, do not invent one or reuse another session's file; retain state in context and report the recovery gap.

Read the current session's `context.md` when it exists. When it is missing, create it directly from the current task instructions and observed state, marking gaps rather than guessing. Do not search for, migrate, or fall back to other note filenames. Maintaining this operational record is part of the workflow, including analysis before implementation approval; it does not authorize implementation. A read-only workflow stage prohibits changes to the subject files, not maintenance of the controller's own session snapshot. An explicit user prohibition on file writes takes precedence: retain state in context and disclose the persistence limitation.

The controller owns this file. Delegated implementers and reviewers use the applicable requirements in their brief and return changes in state to the controller; they do not concurrently edit its snapshot or create independent copies of the controller's authority. A new or forked session uses its own host ID.

## What To Preserve

Keep enough information to continue correctly without assembling old proposal versions:

- **Outcome and constraints:** the current goal, observable completion conditions, responsibilities, compatibility and scope limits, explicit prohibitions. Give important requirements stable identifiers and user wording or a locatable source. Do not substitute an implementation choice for the user's meaning.
- **Current design:** decisions, reasons, requirements they satisfy, and whether they are proposed or confirmed. Identify the complete executable scope separately from a pending revision.
- **Facts and evidence:** observations, evidence locations and applicable source/runtime state. Keep hypotheses and unresolved interpretations separate; invalidate affected facts when their basis changes.
- **Authority:** the actual approving instruction and corresponding implementation/validation scope. A question, suggestion, old approval or assistant assertion is not approval of a changed proposal.
- **Actual progress:** completed changes, remaining work, and obsolete artifacts still present. Distinguish a decision to delete from an observed deletion.
- **Open questions:** unresolved conflicts, missing inputs or evidence, and the next dependent action. A clarification-only turn can change this state even when no code changes.

This is an index of current task state, not a replacement for original user instructions or current source evidence. When an interpretation is disputed, consult its original source. Never promote an assistant design into a user requirement, an inference into a fact, or planned work into completed work.

## Reconcile A New Message

Before changing the affected design or implementation, recover the relevant existing requirements and determine what the new message changes:

- **Supplement:** merge compatible additions; all unrelated constraints remain active.
- **Replacement or withdrawal:** use the user's explicit change to replace or remove the affected requirement/design and its dependent steps, assumptions and expectations.
- **Question or objection:** investigate the challenged decision; do not treat the question itself as an instruction to implement a guessed alternative.
- **Unresolved conflict:** record the exact competing meanings and missing decision. Pause only dependent work; continue independent authorized work.

Keep one complete, internally consistent current snapshot. Remove superseded designs from the effective plan rather than accumulating V1–V4. Retain only a short exclusion and its reason when needed to avoid reintroducing a rejected idea; consult the original conversation for history. An obsolete implementation that still exists belongs under pending cleanup, not under the current design.

## Re-derive The Affected Design

Use brainstorming's Revision Evidence workflow to connect the new instruction to the original outcome, all still-effective constraints and the affected source path. Explicitly establish what changes, what remains binding, whether the complete affected flow still achieves the outcome, and which old decisions lose their basis. Reread the necessary callers, branches, state and output boundaries; file count, an unchanged file or a renamed method cannot stand in for preserved behavior.

For example, adding conversion methods to a read result does not revoke an existing requirement that callers encode complex values before writing. A revised design must keep that write responsibility intact unless the user changes it.

Present one self-contained current proposal through the existing single approval gate. Updating this record adds no separate approval step, and unchanged authorized work retains its approval.

## Retire Superseded Artifacts

For every replaced design, identify what is retained, adjusted and removed. Include code, calls, fields, configuration, dependencies, tests and expectations, scripts, documentation and references that exist only for that design. Delete exclusively obsolete files; precisely revert obsolete changes inside shared files. Preserve current shared functionality, user changes and other tasks' work. Check compatibility before treating published contracts or persisted data as disposable artifacts.

Record obsolete artifacts as pending until the authorized cleanup is performed and its result observed. Do not inherit tests or completion claims whose underlying requirement or design has been withdrawn.

## Read And Update At Decision Points

- On entering or resuming a Cortex task, including after compaction, read the snapshot before deciding or acting. Do not rely solely on a compressed summary.
- On a requirement change, objection or new unresolved question, read and reconcile it before affected design work; record the resulting state before ending the turn, including clarification-only turns.
- Before proposing a revision, compare it with all effective requirements. Before implementation, reconcile the confirmed scope and actual files with the snapshot.
- Update meaningful progress and cleanup state as it changes; save the latest state before intentional compaction and before ending a turn that changed task state. No content change is needed when task state is unchanged; do not reread on every tool call.
- Before reporting completion, compare actual results with user requirements and pending cleanup, not only the latest design. Preserve unresolved items.

Match every factual claim to its direct evidence. If a counterexample undermines a conclusion, withdraw that conclusion and investigate before offering a replacement explanation. Reading or changing this file, structural checks and hook delivery do not prove that the model correctly understood or followed the requirements.
