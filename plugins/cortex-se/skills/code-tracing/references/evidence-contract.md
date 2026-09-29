# Lightweight evidence checkpoints

Use this contract for claims about repository behavior, defects, root causes,
or necessary changes. It does not apply to ordinary naming, translation, or
questions that make no repository claim. It supplements the user's requested
scope and any material decisions resolved through Cortex SE; it never grants
authority to expand that scope.

## Minimal workflow

1. State the claim and its necessary conditions. Resolve known symbols with
   an available language-aware index or server under code-tracing's tool rules.
2. Replace ordinary source-range reads with one useful batch through the
   evidence helper. SessionStart supplies the actual script path, data directory,
   and session id; append the command read and send a JSON object on stdin.
3. Reuse receipts whose files are unchanged. After investigation, append check
   to that same command prefix and send claims on stdin. Check once before a
   conclusion or source-based proposal, not after every tool.
4. If a check identifies a gap, read just that range or classify the claim as
   inferred/unresolved. Do not start another model review by default.

The helper uses Python 3.10 or newer and only its standard library. Installed
hooks provide PLUGIN_DATA; source-based tests may pass an explicit isolated
data directory. Do not use a different directory/session id to bypass a failed
checkpoint. Records contain paths, ranges, fingerprints and declared claims;
they do not retain source contents or send data to another service.

Example read input:

~~~json
{
  "scope": "Determine whether the missing-table branch is reachable",
  "files": [
    {"path": "table.py", "start": 1, "end": 35},
    {"path": "startup.py", "start": 1, "end": 24}
  ]
}
~~~

Use at most 4 KiB per range and 16 KiB of source per batch. Leave enough tool
output budget for the returned content and metadata; truncated output must not
be treated as inspected evidence. A receipt proves that the helper read and
returned a range, not that the model understood it.

Determine the actual file length before choosing ranges; do not use oversized
placeholder end lines. If a sandbox prevents writes to the supplied plugin data
directory, do not change the directory to bypass enforcement. Use direct source
reads under the same semantic standard and disclose the unavailable checkpoint.

The response supplies receipt ids, exact paths, line ranges, whole-file hashes,
and the source text. A new scope clears earlier scope receipts; additional
batches in the same scope preserve existing receipts. A batch failure leaves
the investigation pending and registers no partial batch.

## Claims

Each claim declares an id, text, kind, status, checks, and gaps.

| Kind | Required checks for confirmed/refuted status |
| --- | --- |
| behavior | entry, flow, boundary |
| defect | entry, preconditions, failure_path |
| root_cause | entry, failure_path, symptom |
| necessity | current_contract, caller, alternatives |

Each check contains a non-empty reason and receipt ids. One range may support
multiple checks only when its actual content does so. Do not collect unrelated
files, repeat ids, or fill explanations merely to satisfy the schema.

Example analysis input, replacing receipt ids with actual returned ids:

~~~json
{
  "purpose": "analysis",
  "claims": [{
    "id": "C1",
    "text": "The current project requires a missing-table recovery branch",
    "kind": "necessity",
    "status": "unresolved",
    "checks": {
      "current_contract": {
        "reason": "The lookup API reports whether a table exists.",
        "receipts": ["actual-receipt-id"]
      }
    },
    "gaps": ["No current-project deletion path has been established."]
  }]
}
~~~

Confirmed and refuted claims require all their declared kind's checks and no
gaps. Inferred and unresolved claims require a specific gap; they may pass an
analysis checkpoint because reporting uncertainty is a valid outcome.
Passing that checkpoint does not promote them to confirmed.

For a proposal or impending write, use purpose change and identify confirmed
claim ids in basis. For a new feature based solely on an explicit user request,
user_requirement may quote that request instead. The program cannot establish
that this quotation is authentic or that a material choice was resolved:
recover the actual request and any subsequent user decision from task context.
Do not fabricate a defect or requirement to obtain a passing check.

When a previously established design choice applies, include constraints:

~~~json
{
  "name": "configuration-format",
  "source": "The user's identified instruction establishing YAML",
  "expected": "YAML",
  "proposed": "YAML"
}
~~~

The constraints array belongs on the checkpoint object. A mismatch fails.
The helper compares declared values; it cannot extract or authenticate every
natural-language decision. Before updating a changed constraint, follow
brainstorming's revision evidence workflow and resolve any material unanswered
behavior or scope decision with the user; do not silently rewrite expected
values. A scope-preserving implementation detail needs no new approval.

## What the program does and does not establish

The helper checks schema, referenced receipts, current file hashes, missing
obligations, unresolved change bases, and declared constraint mismatches.
Its ok result means structural checks passed. It does not prove reachability,
exclude all dynamic calls, validate an explanation, certify runtime identity,
or establish user approval. The agent must still read the returned source and
reconcile prerequisites, caller/callee behavior, and relevant counterexamples.

If runtime state, external configuration, timing, or instance identity is
unknown, preserve that gap. An available API is not a current caller; a
downstream improvement is not proof that an upstream failure is repaired.

## Lightweight hooks

- SessionStart exposes the helper prefix and pending scope, including on resume.
- PreToolUse checks registered investigations before apply_patch/Edit/Write.
  It requires a current change checkpoint; this is not a general filesystem
  security boundary and does not intercept shell writes.
- Stop checks registered investigations once. It asks for one continuation
  when evidence is incomplete. A second unsuccessful stop ends continuation
  with an explicit incomplete message rather than silently passing or looping.

There is no PostToolUse scan of every command and no automatic extra agent.
An unregistered investigation is outside programmatic coverage: routing still
requires registration. A hook cannot guarantee that unsupported tools or
already-visible prose were blocked. Records can also be modified by a process
with the user's filesystem permissions; this is an error-reduction workflow,
not an adversarial security boundary.

Hooks must be trusted in Codex after installation. A helper check reports
whether SessionStart was observed, but this alone does not prove every hook is
enabled. Verify actual event behavior when installing or testing the plugin.
If the host skips hooks, the helper fails, or evidence is unavailable, report
the enforcement/knowledge gap. Never claim the guard was active merely because
its configuration file exists.
