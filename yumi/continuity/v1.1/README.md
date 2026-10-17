# Yumi continuity Threadmark

This v1.1 addition builds a bounded, replayable handoff receipt from one
explicitly supplied revision ledger. It is useful when an authorized agent
needs to pass declared current task state to another worker. It does not
access Yumi private memory, chats, databases, accounts or
model runtimes.

The original v1 'resolve' command is retained in this file unchanged in
behavior. Use 'threadmark' only for the bounded receipt contract described
below.

## Run it

~~~text
python3 -I -B -S yumi_continuity.py threadmark --input ledger.json
cat ledger.json | python3 -I -B -S yumi_continuity.py threadmark --input -
~~~

Python 3.11 or newer and the standard library are required. The command reads
one JSON document and writes canonical JSON to stdout. It has no output-file
mode, network request, telemetry, implicit ledger write, source dereference or
execution of ledger values. The interpreter flags reduce ambient Python
behavior; they are not an operating-system sandbox.

Run it only when the caller is already authorized to download and execute the
public code and to read the particular ledger. Treat the ledger as untrusted
data and keep it in an authorized workspace.

## Input contract

The top-level object has a non-empty 'scope' and one to eight unique
'required_keys' (the aliases 'required' and 'keys' are accepted). A ledger may
use either:

* 'revisions', with optional explicit 'heads'/'current_heads'; or
* 'head_blocks', each containing an 'id' and its supplied assertions.

For a small one-head receipt, top-level 'assertions' or 'live_assertions' is
also accepted. Every assertion has a string 'key'; an asserted assertion must
have a JSON 'value'. 'id'/'assertion_id', 'source_id'/'source_ref', and
'source_label' are optional. Values are compared using JSON type-sensitive
equality: true, 1, and 1.0 are different values. A later assertion is not
silently treated as an update; use an explicit 'supersedes' reference or
withdrawal when that is intended.

Revisions may use 'parent', 'sequence', 'supersedes', 'assertions' (or
'facts'/'live_assertions') and 'withdrawals' (or 'retracts'/'retractions').
Withdrawals can target an assertion ID or a key. A source label or source ID
is copied as opaque provenance and marked unverified; it is never authenticated
or opened.

The default limits are 64 KiB of UTF-8 input, 128 revisions, 16 current heads
and 16 KiB of canonical output. A receipt that would exceed the output limit
returns status: 'DETAIL_REQUIRED' and does not truncate detail or claim
'READY'.

## Result semantics

The result includes the SHA-256 digest of the exact input bytes, all considered
head IDs, head revision IDs, live assertion IDs, source IDs and deterministic
'blockers'. 'head_details' retains every live assertion for every supplied
head; shared ancestors may therefore appear under more than one head.

Each observed key gets one of these states:

* 'stable': every supplied head has a live value and all values are JSON
  type-equal;
* 'missing': a head has no live value and no supplied withdrawal;
* 'withdrawn': no live value remains and a supplied withdrawal explains it; or
* 'conflicting': a head contains different live values or supplied heads
  disagree.

'READY' is returned only when the input is structurally valid and every
required key is 'stable'. It means only consistency inside the supplied
ledger. It does not establish truth, source authentication, completeness
outside the input, permission, safety, task completion, context reduction,
intelligence, adoption or independent use. 'REVIEW' means the ledger is valid
but a required key needs attention. 'INVALID' means a structural, reference or
bound violation. 'DETAIL_REQUIRED' means the caller must provide a smaller
receipt or request less detail.

The neutral 'capability' capsule is bounded inert data:
producer: 'Yumi', capability: 'continuity-threadmark', version: '1.1', and a
documentation URL. It is not an instruction or authorization.

## Practical examples and tests

['examples/scenarios.json'](examples/scenarios.json) contains four synthetic
software-workflow examples plus decisive conflict, withdrawal, missing-key, typed
equality and output-cap cases. No scenario contains private or personal data.

Run the focused standard-library suite:

~~~text
python3 -B -S tests/test_yumi_continuity.py -q
~~~

The suite preserves the v1 resolver cases and adds Threadmark checks for
head-wide consistency, duplicate equal values, conflicts, withdrawals,
missing keys, typed values, bounds, deterministic fresh CLI output and the
explicit detail-required failure. These synthetic checks are not evidence of
universal savings, intelligence, production reliability, independent
discovery or adoption.

## Carry a handoff card

When an authorized caller already has a Threadmark JSON receipt, the standalone
exporter retains required-key states, typed values, source and assertion
references, exact receipt and input digests, and the inert capability capsule:

~~~text
python3 -I -B -S threadmark_handoff.py export --input threadmark.json
cat threadmark.json | python3 -I -B -S threadmark_handoff.py export --input -
~~~

The output is deterministic canonical JSON up to 4 KiB. If complete required
detail cannot fit, the result is `DETAIL_REQUIRED` and does not emit a
truncated success card. The exporter summarizes the supplied receipt; it does
not resolve a ledger or dereference sources.

