---
name: code-tracing
description: Analyze repository-owned source, call chains, control flow, data flow, state, and configuration. Use whenever current code-path evidence is needed for engineering decisions or claims.
---

# Code Tracing

## Evidence Rules

- Before a source conclusion, identify the claim and the conditions that would make it true or false. An available API is not evidence that the current project invokes it; a downstream fix is not evidence that the failing path reaches it.
- For each relevant entry, inspect upstream validation, dispatch/registration, state initialization and mutation, and lifecycle callbacks needed by the claim. A text-search miss does not prove the absence of dynamic dispatch.
- Use [batch evidence checkpoints](references/evidence-contract.md) for repository conclusions. Register actual ranges in useful batches, reuse fresh receipts, and validate the declared obligations before a definitive conclusion or a proposal based on it.
- Structural validation does not prove that the cited code supports the claim. Reconcile the evidence with the strongest relevant counterexample before classifying it as confirmed.

- Treat tracing as read-only analysis. Do not edit files, generate patches, or create implementation plans.
- Do not claim current behavior, reachability, impact, root cause, regression risk, state, or completion evidence without supporting source evidence.
- Read every repository-owned implementation, or every execution-relevant range when an implementation exceeds one evidence unit, used as evidence for a conclusion.

## Tool Selection

- For a Go request with an exact package, type, method, or function symbol, use gopls first to resolve definitions, references, implementations, and static incoming or outgoing calls.
- Do not use repository text search as the first symbol-analysis action when an exact Go symbol is known.
- Use repository search first only when no exact Go symbol is known, or when locating entry points, registration, configuration, strings, generated code, dynamic dispatch, or non-Go paths.
- After gopls resolves the static relation, use repository search or source reading only for dynamic calls, reflection, registration, configuration-driven behavior, runtime reachability, or an unresolved relation.
- When gopls cannot analyze the target build configuration, cannot resolve the target, or is unavailable, record the specific cause and then fall back to repository search and source reading.

## Trace Boundary

- Start from the user question, symptom, diff hunk, failing command, or target requirement.
- Trace only the files and functions required to prove or reject the current claim.
- Use the mode supplied by the invoking Cortex skill. If none is supplied, treat an explicit target and outcome as `Precision`, and a request to diagnose, compare, design, or choose as `Exploration`; ask one clarifying question only when the classification changes the user-visible outcome, otherwise use `Precision`.
- In `Precision` mode, begin with the requested target and only the direct callers or callees required to prove the requested impact. Expand a path only when it is a proven prerequisite for an acceptance criterion, and state that criterion before expanding.
- In `Exploration` mode, broaden only until the requested diagnosis, comparison, or design decision is supported. Default to one complete repository-owned path from entry point through business logic to the relevant state, configuration, persistence, or external boundary.
- In `Exploration` mode, prefer three to seven repository-owned functions and one to two entry paths for ordinary tracing.
- Expand one additional hop only when the conclusion depends on the callee's internal behavior.
- Stop at generic SDK, framework, generated, vendor, or standard-library internals; use their public contract, type signature, local wrapper usage, official documentation, or repository usage instead.
- Stop when evidence proves the claim. Do not continue solely to increase confidence.
- State why each additional path is required before tracing more than one independent path.

## Context Budget

- Read source, diff, and report content in units of at most 4 KiB.
- Load no more than 16 KiB of that content for one trace or recovery pass; symbol metadata, file names, and type signatures do not count toward this limit.
- For an implementation larger than one unit, read only the execution-relevant ranges and state any unexamined range that prevents a broader conclusion.
- When the evidence budget cannot prove the claim, state the next required range or path as an evidence gap. Do not continue into unbounded context or infer the missing behavior.
- Treat these limits as per-pass reading limits, not proof of completeness. Continue with another bounded pass only for a named, claim-relevant gap within the authorized investigation; otherwise narrow the conclusion.

## Tracing Workflow

1. Identify the target language, entry point, known symbols, and current claim.
2. Resolve static relationships with the selected tool and identify unproven dynamic paths.
3. Read the repository-owned implementation for every function used as evidence.
4. Trace caller-to-callee parameters, return values, errors, side effects, state changes, initialization order, configuration timing, ownership, and lifecycle boundaries when relevant.
5. List key state variables and possible values when state affects behavior; identify reachable, impossible, and unproven branches.

## Output

- State whether the conclusion is confirmed or inferred.
- Separate confirmed facts, reasoned inferences, and open questions.
- Provide only the key source references and call-chain nodes.
- Do not paste large source, diff, log, or test output.

## Scope

Provide source and call-chain evidence. Keep design in `cortex:brainstorming`, planning in `cortex:writing-plans`, debugging diagnosis in `cortex:systematic-debugging`, review conclusions in `cortex:requesting-code-review`, and completion claims in `cortex:verification-before-completion`.
