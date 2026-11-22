# Architecture Design

## Authority And Application

This is Cortex SE's single source of architecture design constraints. The six design principles and eight Unix principles below are primary; supplements explain their application and must not introduce competing prohibitions. Skills define when to apply this document, not separate architecture policies.

- Apply the principles to the requested design or change and its directly relevant boundaries. User requirements, existing contracts, compatibility and authorized scope remain design inputs; these principles do not authorize unrelated refactoring or implementation.
- Evaluate applicability against actual responsibilities, consumers, dependencies, contracts, state and lifecycle. Use source evidence for existing behavior and identify proposed behavior explicitly. An absent relationship can make a principle inapplicable; do not invent interfaces, extension points or infrastructure to satisfy it.
- Choose the smallest sufficient design within the required scope. Smallest does not mean fewest files, lines or interfaces: necessary separation or dependency changes are valid parts of meeting the requirement. Preserve independently functioning behavior outside that scope.
- Apply the decision and exception rules at the end of this document. A statement such as "complies with SOLID" is not evidence. Keep user-facing explanations proportional to the decision; a trivial change does not require a fourteen-row report.

## Six Design Principles

### D1. Single Responsibility

When defining or changing a module, group behavior by cohesive responsibility and reason to change. Keep transport, business logic, persistence, configuration and presentation responsibilities distinguishable, with explicit owners. Do not split solely by line count or require a separate layer or file for every responsibility.

### D2. Open/Closed

When requirements establish a variation or extension point, use a stable contract so that extending it does not repeatedly disturb unrelated stable logic. Identify the actual variation and consumers. Do not speculate about future variants, build an extension framework without need, or forbid justified changes to an existing implementation.

### D3. Liskov Substitution

Where implementations can substitute for one another, preserve the consumer's contract: accepted inputs, result meaning, errors, side effects, invariants and lifecycle. Do not strengthen preconditions or weaken guarantees. Matching a signature is insufficient; without a substitution relationship, do not create one merely to apply this principle.

### D4. Interface Segregation

Where consumers depend on a contract, shape it around the capabilities they actually need; do not force unrelated methods or dependencies on them. Split by cohesive consumer needs, not mechanically into single-method interfaces or one interface per concrete type.

### D5. Dependency Inversion

When business policy must be isolated from an external mechanism or support a confirmed replacement need, define the required contract from the policy consumer's needs and let the mechanism satisfy it. Make dependency direction and composition explicit. Do not require interfaces for every dependency or create an abstraction that isolates no meaningful responsibility.

### D6. Law Of Demeter

Where collaborators interact, depend on direct collaborators' stable contracts rather than traversing their internal object layout. Judge structural knowledge and coupling, not the number of dots in an expression. A boundary can legitimately delegate; adding forwarding methods without hiding meaningful internal knowledge does not satisfy this principle.

## Eight Unix Principles

### U1. Modularity

Build cohesive components connected by simple contracts. At relevant boundaries, define inputs, outputs, errors and ownership of state and resources. Simplicity of the interface must not conceal obligations its consumer needs to understand.

### U2. Clarity

Prefer direct, understandable and verifiable behavior over cleverness. Justify a less obvious technique with a concrete requirement or demonstrated benefit and explain its relevant constraints. Brevity alone does not justify obscuring control flow.

### U3. Composition

Where components must cooperate, make their contracts compatible and their composition explicit. Avoid hidden global state and unnecessary execution-order coupling. Do not force pipelines, services or a generic composition framework on components without such a requirement.

### U4. Separation

Keep policy separate from mechanism and interfaces separate from execution engines where these responsibilities exist. Locate business decisions with their owner rather than scattering them across transport or persistence. Separation does not mandate an extra class or layer when responsibilities are already clear.

### U5. Simplicity

Choose the simplest structure that meets the requirements and preserves contracts; keep behavior and changes local where possible. Do not trade understandable responsibilities for fewer lines or push complexity into hidden coupling. See U6 for whether an addition is needed at all.

### U6. Parsimony

Require a current need for added code, dependencies and facilities. Reuse proven repository patterns and helpers when their semantics fit the required responsibility and contract. Avoid speculative generalization, but do not force reuse that introduces mismatched behavior or coupling.

### U7. Transparency

Make important control flow, state transitions and failure reasons inspectable and debuggable. Use explicit state and error handling, with observability where the requested behavior needs it. Do not add logging, monitoring systems or instrumentation automatically, or hide failures behind apparently successful defaults.

### U8. Least Surprise

Make names, defaults, errors and side effects consistent with established contracts and project conventions. Expose relevant I/O, mutation, fallback and lifecycle behavior to consumers. Do not preserve an unsuitable convention merely for familiarity, or change a contract silently in pursuit of another principle.

## Supplements

These are applications of the named principles, not an independent architecture standard.

### Abstractions And Delegation — D1, D5, D6, U1, U4, U6

- Introduce layers, factories, interfaces, adapters, generic helpers or cross-package dependencies only for a confirmed responsibility or design need. Apply the same necessity assessment to concurrency, caches and global state; define their state and resource owners under U1 and U3.
- A wrapper can own validation, normalization, permission checks, state transitions, error translation, resource management, a stable domain contract or isolation of internal structure. Explain the actual consumer and boundary it protects. Delegating unchanged arguments can be valid when that boundary has an evidenced responsibility; forwarding alone, a pattern name or hypothetical reuse is insufficient.
- Do not remove an existing wrapper merely because it is thin. Trace its behavior, consumers and ownership first; any removal must remain within the established design or return to the complete-proposal revision workflow.

### Validation And Failure Boundaries — D1, U1, U7, U8

- Validate external inputs at system, API, configuration, persistence, command and user-facing boundaries. Keep the responsibility at the appropriate boundary rather than duplicating it throughout internal logic.
- Do not add defensive handling for inputs or states excluded by proven initialization, constructors, invariants or call chains. Trace an unclear invariant before deciding; an unproven guarantee does not justify removing validation.
- Do not silently ignore impossible internal states with empty returns, default values or swallowed errors. Define failure behavior consistently with the contract; changing that behavior requires the request-scoped proposal workflow.

## Decisions, Exceptions And Verification

- For each applicable principle, connect the relevant design decision to a concrete responsibility, dependency direction, contract or state boundary. Record justified inapplicability where it matters to the decision; do not use it to dismiss a relationship that actually exists.
- Evaluate interacting principles together. For example, dependency inversion may justify an interface while simplicity and parsimony constrain its size and necessity; neither an automatic interface nor an automatic ban resolves the tradeoff.
- Resolve material conflicts before the affected implementation. Explain the requirement, competing principles, chosen design, consequences and validation in the complete plan. Make any necessary exception explicit there; unresolved material choices return to brainstorming. Continue within a clear implementation request without adding architecture, exception, design or plan approvals.
- Carry key decisions and exceptions into implementation and relevant review. Check the actual responsibilities, dependencies, substitution contracts, errors and state transitions against them. Scope-changing corrections follow the existing proposal revision workflow.
- Select validation that demonstrates the affected contract or decision. Rule names, checklists and structural checks do not prove architecture quality or correct runtime behavior. In a code-change review, retain its evidence and finding-classification requirements; a preference or unsupported principle allegation is not a proven runtime defect.
