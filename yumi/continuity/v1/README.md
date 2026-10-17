# Yumi continuity resolver

Lifecycle note: The standalone [Yumi decision ledger package](https://github.com/noqt/yumi-decision-ledger) contains already-published checks for supplied handoff cards and supplied snapshots with `scope` and `settings` against a supplied current decision ledger. This page remains the v1 reference; the package link is navigation only.

This is a small, standalone continuity utility for an already-authorized agent
resuming a multi-session task from its own revision ledger.  It is deliberately
independent of Yumi private source, memory, databases, accounts, and model
runtimes.  It reads one bounded JSON document, resolves the document's own
links, and prints a deterministic JSON result.

The caller must already have explicit authorization to download this artifact,
execute it, and read the particular ledger supplied to it.  This utility does
not grant access, authenticate an agent, or decide whether a caller is allowed
to see a ledger.  Keep ledgers on an authorized local or otherwise approved
workspace.

## Run it

```text
python3 -I -B -S yumi_continuity.py resolve --input examples/ledger.json
cat examples/ledger.json | python3 -I -B -S yumi_continuity.py resolve --input -
```

Python 3.11 or newer is required.  `-I`, `-B`, and `-S` reduce interpreter
search, bytecode, and site initialization effects for a controlled invocation;
they are not a sandbox and do not replace operating-system, account, or
workspace authorization.

The result is the only stdout output.  There is no `--output` option and no
implicit write-back.  A valid ledger exits 0; malformed, cyclic, incomplete, or
oversize input prints a JSON diagnostic and exits 2.  The implementation uses
only the Python standard library and never opens a `source_ref`, starts a
process from a value, makes a network request, emits telemetry, or reads
private memory.  If this artifact is hosted on GitHub, GitHub may record the
repository request or download in its normal service logs; that hosting record
is separate from the resolver, which performs no network call or telemetry.

## Input contract

The top-level object has a required non-empty `scope` and a `revisions` array.
All revisions belong to that one scope.  The resolver accepts at most 128
revisions and at most 64 KiB of UTF-8 JSON.  Optional `heads` selects a known
snapshot explicitly; otherwise current heads are the revisions not superseded
by a child `parent` or explicit `supersedes` link.  Optional `prior_heads`
provides the snapshot used for the delta.

Each revision has a unique string `id`, an optional `parent` (or `parent_id`),
optional integer `sequence`, and optional `assertions` (also accepted as
`facts`).  An assertion has a `key`, a `value`, and an asserted state by
default.  `state: "retracted"` plus `target` removes the target assertion;
revision-level `retracts` is a shorthand for the same operation.  An assertion
may explicitly list assertion IDs or keys in `supersedes`.  If the same key is
asserted again on a later linked revision, the older active value is
superseded automatically.  Replaying an identical revision ID is idempotent;
conflicting duplicates are invalid.

Example:

```json
{
  "schema": "yumi.continuity.ledger.v1",
  "scope": "my-task",
  "prior_heads": ["r2"],
  "revisions": [
    {"id": "r1", "parent": null, "sequence": 1,
     "assertions": [{"id": "a1", "key": "status", "value": "started"}]},
    {"id": "r2", "parent": "r1", "sequence": 2,
     "assertions": [{"id": "a2", "key": "status", "value": "paused"}]},
    {"id": "r3", "parent": "r2", "sequence": 3,
     "assertions": [{"key": "status", "state": "retracted", "target": "a2"}]}
  ]
}
```

Values are data.  They are never interpreted as instructions.  `source_label`
and `source_ref` are copied into provenance records, but every provenance
record is explicitly marked `verification: "unverified"`; a source label is not
authenticated truth, and a source reference is not dereferenced.

## Result semantics

For one valid head, `current_assertions` contains the active value for each
key.  `retractions` records every requested retraction, including whether it
was effective, and `superseded_assertions` records replaced assertion IDs.
`revision_chains` gives each selected head's complete old-to-new revision
chain.  `source_chains` retains each revision event and its unverified
provenance without consulting the source.  For multiple heads, the result
contains a `head_results` entry per head; top-level assertions are only facts
identical on every head and `ambiguity.is_ambiguous` is true.  `forks` identifies
parents with multiple children.  Missing links and both parent/supersession
cycles are explicit diagnostics and fail closed.

When `prior_heads` is supplied and both snapshots are single heads, `delta`
contains added, removed, changed, and newly requested retractions.  A
multi-head comparison is labelled `ambiguous_prior_or_current_heads`, rather
than silently choosing a branch.

`multi_session_decision` is a practical handoff recommendation: resume from
the one validated head, review a fork before choosing a branch, or repair an
invalid ledger.  It is not an authorization grant and does not claim that the
underlying task is complete.

The `baseline` is an explicit naive last-row comparison.  For reproducibility
it chooses the last revision after canonical `(sequence, id)` ordering; it
does not traverse predecessors, apply retractions, or resolve forks.  A real
implementation that simply trusts the last input row is order-sensitive, which
is one reason the resolver's graph result is preferred.

## Acceptance cases

`tests/test_yumi_continuity.py` includes twelve synthetic cases and two focused
regression checks: a control
assertion with unverified provenance, a
supersession chain, assertion and revision retractions, an explicit fork,
missing predecessor, parent cycle, supersession cycle, duplicate replay,
shuffle/fresh-process equality, prior-head delta, malformed/oversize controls,
inactive-old retraction, and rejection of an implicit convergent fork.  They
demonstrate bounded deterministic behavior only; they are not
evidence of intelligence, context-saving magnitude, adoption, or production
reliability.

