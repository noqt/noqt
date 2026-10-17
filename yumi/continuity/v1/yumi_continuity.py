#!/usr/bin/env python3
"""Deterministic, offline revision-ledger continuity resolver.

The program intentionally has no dependency on Yumi internals.  A ledger is a
small JSON document supplied by the caller, and the resolver only follows the
links and values present in that document.  ``source_ref`` values are opaque
labels: they are never opened, fetched, authenticated, or executed.

CLI contract::

    python3 -I -B -S yumi_continuity.py resolve --input ledger.json
    cat ledger.json | python3 -I -B -S yumi_continuity.py resolve --input -

The only output is canonical JSON on stdout.  Invalid input also produces a
canonical JSON diagnostic on stdout and exits with status 2.  There is no
output-file option, network path, telemetry, or implicit ledger write.
"""

from __future__ import annotations

import json
import sys
from typing import Any

MAX_INPUT_BYTES = 64 * 1024
MAX_REVISIONS = 128
MAX_ID_LENGTH = 256
MAX_TEXT_LENGTH = 4096

ASSERTED_STATES = frozenset({"assert", "asserted", "set", "active"})
RETRACTED_STATES = frozenset({"retract", "retracted", "withdraw", "withdrawn"})


def _canonical(value: Any) -> str:
    """Return a stable JSON representation for equality and sorting."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_bytes(value: Any) -> bytes:
    return _canonical(value).encode("utf-8")


def _replay_canonical(value: dict[str, Any]) -> str:
    """Canonicalize a revision while treating replayed arrays as sets.

    Revision order, assertion order, and reference-list order carry no meaning
    in this contract.  Normalizing those arrays lets an exact replay remain
    idempotent even when a transport reordered JSON list members.
    """

    copy_value = json.loads(_canonical(value))
    for field in ("assertions", "facts", "supersedes", "retracts", "retractions"):
        members = copy_value.get(field)
        if isinstance(members, list):
            copy_value[field] = sorted(members, key=_canonical)
    return _canonical(copy_value)


def _unique_sorted(values: list[str]) -> list[str]:
    return sorted(set(values))


def _diagnostics() -> dict[str, Any]:
    return {
        "errors": [],
        "invalid_fields": [],
        "missing_references": [],
        "cycles": [],
        "limits": [],
        "duplicate_revisions": [],
    }


def _empty_result(scope: Any = None, *, status: str = "invalid", diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "resolver": "yumi-continuity",
        "schema": "yumi.continuity.result.v1",
        "status": status,
        "scope": scope,
        "heads": [],
        "current_assertions": [],
        "retractions": [],
        "superseded_assertions": [],
        "forks": [],
        "head_results": [],
        "revision_chains": {},
        "source_chains": [],
        "ambiguity": {"is_ambiguous": False, "reasons": []},
        "delta": {
            "status": "not_supplied",
            "from_prior_heads": [],
            "added": [],
            "removed": [],
            "changed": [],
            "new_retractions": [],
        },
        "baseline": {
            "method": "naive_last_row_after_canonical_sort",
            "selected_revision_id": None,
            "asserted": [],
            "retracted": [],
            "limitations": [
                "A row-only baseline does not traverse predecessors or resolve forks.",
                "A real last-input-row implementation is order-sensitive; this baseline sorts revision IDs/sequences first so the result is reproducible.",
            ],
        },
        "multi_session_decision": {
            "action": "repair_ledger",
            "reason": "The ledger is not valid enough to choose a continuation head.",
            "resume_from": [],
        },
        "provenance_policy": {
            "source_labels": "unverified",
            "source_refs": "opaque_and_not_dereferenced",
            "values": "data_only_never_executed",
        },
        "diagnostics": diagnostics if diagnostics is not None else _diagnostics(),
        "limits_policy": {
            "max_input_bytes": MAX_INPUT_BYTES,
            "max_revisions": MAX_REVISIONS,
            "one_scope_only": True,
        },
    }


def _add(diagnostics: dict[str, Any], field: str, value: Any) -> None:
    bucket = diagnostics[field]
    if value not in bucket:
        bucket.append(value)


def _text(value: Any, field: str, diagnostics: dict[str, Any], *, allow_empty: bool = False) -> str | None:
    if not isinstance(value, str):
        _add(diagnostics, "invalid_fields", field)
        return None
    if not allow_empty and not value.strip():
        _add(diagnostics, "invalid_fields", field)
        return None
    if len(value) > MAX_TEXT_LENGTH:
        _add(diagnostics, "limits", f"{field}_length")
        return None
    return value


def _list_value(value: Any, field: str, diagnostics: dict[str, Any]) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        _add(diagnostics, "invalid_fields", field)
        return []
    return value


def _reference_list(value: Any, field: str, diagnostics: dict[str, Any]) -> list[str]:
    items = _list_value(value, field, diagnostics)
    output: list[str] = []
    for index, item in enumerate(items):
        value_text = _text(item, f"{field}[{index}]", diagnostics)
        if value_text is not None:
            output.append(value_text)
    return sorted(set(output))


def _source_value(raw: dict[str, Any], parent: dict[str, Any]) -> tuple[str, str | None]:
    label = raw.get("source_label", raw.get("source", parent.get("source_label", parent.get("source"))))
    if not isinstance(label, str) or not label.strip():
        label = "unspecified"
    reference = raw.get("source_ref", parent.get("source_ref"))
    if not isinstance(reference, str):
        reference = None
    return label, reference


def _normalise_assertion(
    raw: Any,
    revision_id: str,
    revision: dict[str, Any],
    index: int,
    diagnostics: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        _add(diagnostics, "invalid_fields", f"revisions[{revision_id}].assertions[{index}]")
        return None

    key_raw = raw.get("key", raw.get("name"))
    if key_raw is None and raw.get("state", raw.get("status", "asserted")) in RETRACTED_STATES:
        key_raw = raw.get("target")
    key = _text(key_raw, f"revisions[{revision_id}].assertions[{index}].key", diagnostics)
    if key is None:
        return None

    state_raw = raw.get("state", raw.get("status", raw.get("op", "asserted")))
    if not isinstance(state_raw, str):
        _add(diagnostics, "invalid_fields", f"revisions[{revision_id}].assertions[{index}].state")
        return None
    state_value = state_raw.strip().lower()
    if state_value in ASSERTED_STATES:
        state = "asserted"
    elif state_value in RETRACTED_STATES:
        state = "retracted"
    else:
        _add(diagnostics, "invalid_fields", f"revisions[{revision_id}].assertions[{index}].state")
        return None

    assertion_id_raw = raw.get("id", raw.get("assertion_id"))
    assertion_id = _text(assertion_id_raw, f"revisions[{revision_id}].assertions[{index}].id", diagnostics) if assertion_id_raw is not None else f"{revision_id}:{key}"
    if assertion_id is None:
        return None

    source_label, source_ref = _source_value(raw, revision)
    supersedes = _reference_list(raw.get("supersedes", []), f"revisions[{revision_id}].assertions[{index}].supersedes", diagnostics)
    target_raw = raw.get("target", raw.get("retracts"))
    if target_raw is None and state == "retracted":
        target_raw = assertion_id_raw if assertion_id_raw is not None else key
    target = _text(target_raw, f"revisions[{revision_id}].assertions[{index}].target", diagnostics) if target_raw is not None else None
    reason = raw.get("reason")
    if reason is not None:
        reason = _text(reason, f"revisions[{revision_id}].assertions[{index}].reason", diagnostics)

    event: dict[str, Any] = {
        "assertion_id": assertion_id,
        "key": key,
        "state": state,
        "revision_id": revision_id,
        "source_label": source_label,
        "source_ref": source_ref,
        "supersedes": supersedes,
        "target": target,
        "reason": reason,
    }
    if state == "asserted":
        if "value" not in raw:
            _add(diagnostics, "invalid_fields", f"revisions[{revision_id}].assertions[{index}].value")
            return None
        event["value"] = raw["value"]
    return event


def _normalise_revisions(data: dict[str, Any], diagnostics: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    raw_revisions = data.get("revisions")
    if not isinstance(raw_revisions, list):
        _add(diagnostics, "invalid_fields", "revisions")
        return {}, []
    if len(raw_revisions) > MAX_REVISIONS:
        _add(diagnostics, "limits", "revision_count")

    revisions: dict[str, dict[str, Any]] = {}
    duplicate_ids: list[str] = []
    for index, raw in enumerate(raw_revisions):
        if not isinstance(raw, dict):
            _add(diagnostics, "invalid_fields", f"revisions[{index}]")
            continue
        identifier = _text(raw.get("id"), f"revisions[{index}].id", diagnostics)
        if identifier is None or len(identifier) > MAX_ID_LENGTH:
            _add(diagnostics, "limits", f"revisions[{index}].id_length") if isinstance(identifier, str) and len(identifier) > MAX_ID_LENGTH else None
            continue
        if identifier in revisions:
            if _replay_canonical(raw) == revisions[identifier]["raw_canonical"]:
                duplicate_ids.append(identifier)
                continue
            _add(diagnostics, "errors", f"duplicate_revision_conflict:{identifier}")
            continue

        parent_values = []
        if "parent" in raw:
            parent_values.append(raw.get("parent"))
        if "parent_id" in raw:
            parent_values.append(raw.get("parent_id"))
        parent: str | None = None
        if parent_values:
            if len(parent_values) == 2 and parent_values[0] != parent_values[1]:
                _add(diagnostics, "invalid_fields", f"revisions[{identifier}].parent")
            parent_raw = parent_values[0]
            if parent_raw is not None:
                parent = _text(parent_raw, f"revisions[{identifier}].parent", diagnostics)

        supersedes = _reference_list(raw.get("supersedes", []), f"revisions[{identifier}].supersedes", diagnostics)
        retracts = _reference_list(raw.get("retracts", raw.get("retractions", [])), f"revisions[{identifier}].retracts", diagnostics)
        sequence = raw.get("sequence")
        if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int)):
            _add(diagnostics, "invalid_fields", f"revisions[{identifier}].sequence")
            sequence = None
        assertion_raw = raw.get("assertions", raw.get("facts", []))
        assertions_list = _list_value(assertion_raw, f"revisions[{identifier}].assertions", diagnostics)
        assertions: list[dict[str, Any]] = []
        for assertion_index, assertion_raw_item in enumerate(assertions_list):
            event = _normalise_assertion(assertion_raw_item, identifier, raw, assertion_index, diagnostics)
            if event is not None:
                assertions.append(event)
        assertion_keys = [event["key"] for event in assertions]
        if len(assertion_keys) != len(set(assertion_keys)):
            _add(diagnostics, "errors", f"duplicate_assertion_key:{identifier}")
        revision_source_label = raw.get("source_label", raw.get("source"))
        if not isinstance(revision_source_label, str) or not revision_source_label.strip():
            revision_source_label = "unspecified"
        revision_source_ref = raw.get("source_ref")
        if not isinstance(revision_source_ref, str):
            revision_source_ref = None

        revisions[identifier] = {
            "id": identifier,
            "parent": parent,
            "supersedes": supersedes,
            "retracts": retracts,
            "assertions": assertions,
            "sequence": sequence,
            "source_label": revision_source_label,
            "source_ref": revision_source_ref,
            "raw_canonical": _replay_canonical(raw),
        }
    diagnostics["duplicate_revisions"] = _unique_sorted(duplicate_ids)
    return revisions, sorted(revisions)


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    state: dict[str, int] = {}
    stack: list[str] = []
    found: list[list[str]] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for target in sorted(graph.get(node, [])):
            if state.get(target, 0) == 0:
                visit(target)
            elif state.get(target) == 1 and target in stack:
                cycle = stack[stack.index(target) :] + [target]
                if cycle not in found:
                    found.append(cycle)
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            visit(node)
    return sorted(found, key=_canonical)


def _revision_order_key(revision: dict[str, Any]) -> tuple[int, int, str]:
    sequence = revision.get("sequence")
    if isinstance(sequence, int) and not isinstance(sequence, bool):
        return (1, sequence, revision["id"])
    return (0, 0, revision["id"])


def _topological_chain(head: str, revisions: dict[str, dict[str, Any]]) -> list[str]:
    closure: set[str] = set()
    pending = [head]
    while pending:
        current = pending.pop()
        if current in closure or current not in revisions:
            continue
        closure.add(current)
        revision = revisions[current]
        if revision["parent"] is not None:
            pending.append(revision["parent"])
        pending.extend(revision["supersedes"])

    older_to_newer: dict[str, list[str]] = {identifier: [] for identifier in closure}
    indegree = {identifier: 0 for identifier in closure}
    for identifier in sorted(closure):
        revision = revisions[identifier]
        predecessors = []
        if revision["parent"] in closure:
            predecessors.append(revision["parent"])
        predecessors.extend(item for item in revision["supersedes"] if item in closure)
        for predecessor in sorted(set(predecessors)):
            if identifier not in older_to_newer[predecessor]:
                older_to_newer[predecessor].append(identifier)
                indegree[identifier] += 1

    ready = sorted((revisions[item] for item, degree in indegree.items() if degree == 0), key=_revision_order_key)
    result: list[str] = []
    while ready:
        current_revision = ready.pop(0)
        current = current_revision["id"]
        result.append(current)
        for successor in sorted(older_to_newer[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(revisions[successor])
                ready.sort(key=_revision_order_key)
    return result


def _predecessor_closure(start: str, revisions: dict[str, dict[str, Any]]) -> set[str]:
    """Return links reachable behind a revision, tolerating invalid cycles."""

    seen: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current in seen or current not in revisions:
            continue
        seen.add(current)
        revision = revisions[current]
        if revision["parent"] is not None:
            pending.append(revision["parent"])
        pending.extend(revision["supersedes"])
    return seen


def _provenance(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_label": event.get("source_label", "unspecified"),
        "source_ref": event.get("source_ref"),
        "verification": "unverified",
    }


def _render_event(event: dict[str, Any]) -> dict[str, Any]:
    result = {
        "assertion_id": event["assertion_id"],
        "key": event["key"],
        "state": event["state"],
        "revision_id": event["revision_id"],
        "provenance": _provenance(event),
    }
    if event["state"] == "asserted":
        result["value"] = event.get("value")
    if event.get("target") is not None:
        result["target"] = event["target"]
    if event.get("reason") is not None:
        result["reason"] = event["reason"]
    return result


def _render_retraction(revision_id: str, target: str, event: dict[str, Any] | None, *, reason: str | None = None) -> dict[str, Any]:
    source = event if event is not None else {"source_label": "unspecified", "source_ref": None}
    result: dict[str, Any] = {
        "revision_id": revision_id,
        "target": target,
        "status": "retracted",
        "effective": event is not None,
        "provenance": _provenance(source),
    }
    if reason is not None:
        result["reason"] = reason
    return result


def _resolve_head(head: str, revisions: dict[str, dict[str, Any]], known_targets: set[str]) -> dict[str, Any]:
    chain = _topological_chain(head, revisions)
    active: dict[str, dict[str, Any]] = {}
    retractions: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    source_events: list[dict[str, Any]] = []

    def active_key(target: str) -> str | None:
        if target in active:
            return target
        for key, event in active.items():
            if event["assertion_id"] == target:
                return key
        return None

    def remove_for(target: str, *, by: str | None, revision_id: str, kind: str) -> dict[str, Any] | None:
        key = active_key(target)
        removed = active.pop(key) if key is not None else None
        if removed is not None and kind == "superseded":
            superseded.append(
                {
                    "assertion_id": removed["assertion_id"],
                    "by_assertion_id": by,
                    "key": removed["key"],
                    "revision_id": revision_id,
                }
            )
        return removed

    for revision_id in chain:
        revision = revisions[revision_id]
        revision_source = {
            "source_label": revision.get("source_label", "unspecified"),
            "source_ref": revision.get("source_ref"),
        }
        for target in revision["retracts"]:
            removed = remove_for(target, by=None, revision_id=revision_id, kind="retracted")
            retractions.append(_render_retraction(revision_id, target, removed, reason=None))
            source_events.append(
                {
                    "revision_id": revision_id,
                    "assertion_id": None,
                    "key": target,
                    "state": "retracted",
                    "target": target,
                    "provenance": _provenance(revision_source),
                }
            )

        for event in sorted(revision["assertions"], key=lambda item: (item["assertion_id"], item["key"], item["state"])):
            source_events.append(
                {
                    "revision_id": revision_id,
                    "assertion_id": event["assertion_id"],
                    "key": event["key"],
                    "state": event["state"],
                    "target": event.get("target"),
                    "provenance": _provenance(event),
                }
            )
            if event["state"] == "retracted":
                target = event.get("target") or event["assertion_id"] or event["key"]
                removed = remove_for(target, by=None, revision_id=revision_id, kind="retracted")
                retraction = _render_retraction(
                    revision_id, target, event, reason=event.get("reason")
                )
                retraction["effective"] = removed is not None
                retractions.append(retraction)
                continue

            for target in event["supersedes"]:
                remove_for(target, by=event["assertion_id"], revision_id=revision_id, kind="superseded")
            existing = active.get(event["key"])
            if existing is not None and existing["assertion_id"] != event["assertion_id"]:
                remove_for(event["key"], by=event["assertion_id"], revision_id=revision_id, kind="superseded")
            active[event["key"]] = event

    current = [_render_event(active[key]) for key in sorted(active)]
    retractions = sorted(retractions, key=lambda item: (item["revision_id"], item["target"], _canonical(item)))
    superseded = sorted(
        {
            _canonical(item): item for item in superseded
        }.values(),
        key=lambda item: (item["assertion_id"], item["revision_id"], item.get("by_assertion_id") or ""),
    )
    source_chain = {
        "head": head,
        "revisions": chain,
        "events": source_events,
    }
    return {
        "head": head,
        "revision_chain": chain,
        "current_assertions": current,
        "retractions": retractions,
        "superseded_assertions": superseded,
        "source_chain": source_chain,
    }


def _common_assertions(head_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not head_results:
        return []
    by_head = [{item["key"]: item for item in result["current_assertions"]} for result in head_results]
    common: list[dict[str, Any]] = []
    for key in sorted(by_head[0]):
        candidate = by_head[0][key]
        if all(key in mapping and _canonical(mapping[key].get("value")) == _canonical(candidate.get("value")) for mapping in by_head[1:]):
            common.append(candidate)
    return common


def _state_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in items}


def _delta(
    prior_heads: list[str],
    current_heads: list[str],
    prior_results: list[dict[str, Any]],
    current_results: list[dict[str, Any]],
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "ok",
        "from_prior_heads": prior_heads,
        "to_heads": current_heads,
        "added": [],
        "removed": [],
        "changed": [],
        "new_retractions": [],
    }
    if len(prior_results) != 1 or len(current_results) != 1:
        base["status"] = "ambiguous_prior_or_current_heads"
        base["per_current_head"] = []
        for current in current_results:
            base["per_current_head"].append(
                {
                    "head": current["head"],
                    "current_assertions": current["current_assertions"],
                    "retractions": current["retractions"],
                }
            )
        return base
    before = _state_map(prior_results[0]["current_assertions"])
    after = _state_map(current_results[0]["current_assertions"])
    for key in sorted(set(after) - set(before)):
        base["added"].append({"key": key, "after": after[key]})
    for key in sorted(set(before) - set(after)):
        base["removed"].append({"key": key, "before": before[key]})
    for key in sorted(set(before) & set(after)):
        if _canonical(before[key].get("value")) != _canonical(after[key].get("value")):
            base["changed"].append({"key": key, "before": before[key], "after": after[key]})
    prior_retractions = {_canonical(item) for item in prior_results[0]["retractions"]}
    base["new_retractions"] = [item for item in current_results[0]["retractions"] if _canonical(item) not in prior_retractions]
    return base


def _baseline(revisions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(revisions.values(), key=_revision_order_key)
    if not ordered:
        return {
            "method": "naive_last_row_after_canonical_sort",
            "selected_revision_id": None,
            "asserted": [],
            "retracted": [],
            "limitations": [
                "A row-only baseline does not traverse predecessors or resolve forks.",
                "A real last-input-row implementation is order-sensitive; this baseline sorts revision IDs/sequences first so the result is reproducible.",
            ],
        }
    latest = ordered[-1]
    asserted = []
    retracted = []
    for event in sorted(latest["assertions"], key=lambda item: (item["assertion_id"], item["key"])):
        rendered = _render_event(event)
        if event["state"] == "asserted":
            asserted.append(rendered)
        else:
            retracted.append(rendered)
    return {
        "method": "naive_last_row_after_canonical_sort",
        "selected_revision_id": latest["id"],
        "asserted": asserted,
        "retracted": retracted,
        "limitations": [
            "A row-only baseline does not traverse predecessors or resolve forks.",
            "A real last-input-row implementation is order-sensitive; this baseline sorts revision IDs/sequences first so the result is reproducible.",
        ],
    }


def _decision(status: str, heads: list[str], ambiguous: bool) -> dict[str, Any]:
    if status != "ok":
        return {
            "action": "repair_ledger",
            "reason": "The ledger is invalid or exceeds the bounded resolver contract.",
            "resume_from": [],
        }
    if ambiguous:
        return {
            "action": "review_fork",
            "reason": "More than one current head exists; choose a branch before resuming a multi-session task.",
            "resume_from": heads,
        }
    return {
        "action": "resume_from_head",
        "reason": "Exactly one validated current head is available for the next session.",
        "resume_from": heads,
    }


def resolve_ledger(data: Any, *, raw_bytes: bytes | None = None) -> dict[str, Any]:
    """Resolve a JSON ledger into a deterministic, evidence-labelled result."""

    diagnostics = _diagnostics()
    try:
        measured = raw_bytes if raw_bytes is not None else _canonical_bytes(data)
        input_size = len(measured)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        _add(diagnostics, "limits", "input_bytes")
        result = _empty_result(diagnostics=diagnostics)
        result["diagnostics"]["errors"] = ["input_not_canonical_json"]
        return result
    if input_size > MAX_INPUT_BYTES:
        _add(diagnostics, "limits", "input_bytes")
    if not isinstance(data, dict):
        _add(diagnostics, "invalid_fields", "root")
        result = _empty_result(diagnostics=diagnostics)
        result["input_bytes"] = input_size
        return result

    scope = data.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        _add(diagnostics, "invalid_fields", "scope")
        scope = None
    elif len(scope) > MAX_TEXT_LENGTH:
        _add(diagnostics, "limits", "scope_length")

    revisions, revision_ids = _normalise_revisions(data, diagnostics)
    if not revisions:
        _add(diagnostics, "errors", "no_revisions")

    known_ids = set(revision_ids)
    known_assertion_ids: set[str] = set()
    known_keys: set[str] = set()
    assertion_by_id: dict[str, dict[str, Any]] = {}
    for revision in revisions.values():
        for event in revision["assertions"]:
            known_assertion_ids.add(event["assertion_id"])
            known_keys.add(event["key"])
            previous = assertion_by_id.get(event["assertion_id"])
            if previous is not None and _canonical({k: previous.get(k) for k in ("key", "state", "value", "target")}) != _canonical({k: event.get(k) for k in ("key", "state", "value", "target")}):
                _add(diagnostics, "errors", f"duplicate_assertion_conflict:{event['assertion_id']}")
            assertion_by_id[event["assertion_id"]] = event
    known_targets = known_assertion_ids | known_keys

    parent_graph: dict[str, list[str]] = {identifier: [] for identifier in revision_ids}
    all_graph: dict[str, list[str]] = {identifier: [] for identifier in revision_ids}
    superseded_by: dict[str, list[str]] = {identifier: [] for identifier in revision_ids}
    for identifier in revision_ids:
        revision = revisions[identifier]
        parent = revision["parent"]
        if parent is not None:
            if parent not in known_ids:
                _add(diagnostics, "missing_references", parent)
            else:
                parent_graph[identifier].append(parent)
                all_graph[identifier].append(parent)
                superseded_by[parent].append(identifier)
        for target in revision["supersedes"]:
            if target not in known_ids:
                _add(diagnostics, "missing_references", target)
            else:
                all_graph[identifier].append(target)
                superseded_by[target].append(identifier)
        for target in revision["retracts"]:
            if target not in known_targets:
                _add(diagnostics, "missing_references", target)
        for event in revision["assertions"]:
            for target in event["supersedes"]:
                if target not in known_targets:
                    _add(diagnostics, "missing_references", target)
            if event["state"] == "retracted" and event.get("target") not in known_targets:
                _add(diagnostics, "missing_references", event.get("target"))

    # A single-parent revision cannot silently merge a conflicting sibling
    # branch merely by naming it in revision-level ``supersedes``.  There is no
    # merge-resolution payload in this contract, so reject that convergent
    # fork instead of letting a revision-ID order choose a winner.
    for identifier in revision_ids:
        revision = revisions[identifier]
        parent = revision["parent"]
        targets = [target for target in revision["supersedes"] if target in known_ids]
        if parent is not None and targets:
            parent_ancestors = _predecessor_closure(parent, revisions)
            crossing = sorted(target for target in targets if target not in parent_ancestors)
            for target in crossing:
                _add(
                    diagnostics,
                    "errors",
                    f"convergent_fork_requires_explicit_merge:{identifier}:{target}",
                )
        elif parent is None and len(targets) > 1:
            _add(diagnostics, "errors", f"convergent_fork_requires_explicit_merge:{identifier}")

    diagnostics["missing_references"] = _unique_sorted([item for item in diagnostics["missing_references"] if isinstance(item, str)])
    cycles = _find_cycles(all_graph)
    diagnostics["cycles"] = cycles

    children: dict[str, list[str]] = {identifier: [] for identifier in revision_ids}
    for identifier, predecessors in parent_graph.items():
        for predecessor in predecessors:
            children[predecessor].append(identifier)
    forks = [
        {"parent": identifier, "children": sorted(set(children[identifier]))}
        for identifier in sorted(children)
        if len(set(children[identifier])) > 1
    ]

    def id_list(field: str) -> list[str] | None:
        if field not in data:
            return None
        return _reference_list(data.get(field), field, diagnostics)

    explicit_heads = id_list("heads")
    prior_heads = id_list("prior_heads") or []
    for reference in (explicit_heads or []) + prior_heads:
        if reference not in known_ids:
            _add(diagnostics, "missing_references", reference)

    if explicit_heads is None:
        heads = sorted(
            identifier for identifier in revision_ids if not superseded_by[identifier]
        )
    else:
        heads = sorted(explicit_heads)
    if not heads and revisions:
        _add(diagnostics, "errors", "no_current_heads")

    errors_or_invalid = any(diagnostics[key] for key in ("errors", "invalid_fields", "missing_references", "cycles", "limits"))
    result = _empty_result(scope, status="invalid" if errors_or_invalid else "ok", diagnostics=diagnostics)
    result["input_bytes"] = input_size
    result["heads"] = heads if not errors_or_invalid else []
    result["forks"] = forks
    result["baseline"] = _baseline(revisions)

    if errors_or_invalid:
        result["multi_session_decision"] = _decision("invalid", [], False)
        return result

    known_target_set = known_targets
    head_results = [_resolve_head(head, revisions, known_target_set) for head in heads]
    result["head_results"] = head_results
    result["revision_chains"] = {item["head"]: item["revision_chain"] for item in head_results}
    result["source_chains"] = [item["source_chain"] for item in head_results]
    result["current_assertions"] = _common_assertions(head_results)
    if len(head_results) == 1:
        result["retractions"] = head_results[0]["retractions"]
        result["superseded_assertions"] = head_results[0]["superseded_assertions"]
    else:
        result["retractions"] = sorted(
            {_canonical(item): item for result_item in head_results for item in result_item["retractions"]}.values(),
            key=_canonical,
        )
        result["superseded_assertions"] = sorted(
            {_canonical(item): item for result_item in head_results for item in result_item["superseded_assertions"]}.values(),
            key=_canonical,
        )

    ambiguous = len(heads) > 1
    reasons = []
    if forks:
        reasons.append("fork")
    if len(heads) > 1:
        reasons.append("multiple_heads")
    result["ambiguity"] = {"is_ambiguous": ambiguous, "reasons": reasons}
    result["delta"] = {"status": "not_supplied", "from_prior_heads": [], "added": [], "removed": [], "changed": [], "new_retractions": []}
    if "prior_heads" in data:
        prior_results = [_resolve_head(head, revisions, known_target_set) for head in prior_heads]
        result["delta"] = _delta(prior_heads, heads, prior_results, head_results)
    result["multi_session_decision"] = _decision("ok", heads, ambiguous)
    return result


def _parse_json(raw: bytes) -> Any:
    text = raw.decode("utf-8")

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-standard JSON constant: {value}")

    return json.loads(text, parse_constant=reject_constant)


def _cli_error(code: str, detail: str | None = None) -> dict[str, Any]:
    diagnostics = _diagnostics()
    diagnostics["errors"] = [code]
    if detail:
        diagnostics["errors"].append(detail)
    result = _empty_result(diagnostics=diagnostics)
    result["multi_session_decision"] = _decision("invalid", [], False)
    return result


def _read_cli_input(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    with open(path, "rb") as handle:
        return handle.read(MAX_INPUT_BYTES + 1)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "resolve":
        result = _cli_error("usage: resolve --input PATH")
        sys.stdout.write(_canonical(result) + "\n")
        return 2
    args = args[1:]
    if "--output" in args:
        result = _cli_error("unsupported_option:--output")
        sys.stdout.write(_canonical(result) + "\n")
        return 2
    if len(args) != 2 or args[0] != "--input" or not args[1]:
        result = _cli_error("usage: resolve --input PATH")
        sys.stdout.write(_canonical(result) + "\n")
        return 2
    try:
        raw = _read_cli_input(args[1])
    except (OSError, ValueError) as exc:
        result = _cli_error("input_read_failed", type(exc).__name__)
        sys.stdout.write(_canonical(result) + "\n")
        return 2
    if len(raw) > MAX_INPUT_BYTES:
        result = _empty_result(diagnostics=_diagnostics())
        _add(result["diagnostics"], "limits", "input_bytes")
        result["input_bytes"] = len(raw)
        result["multi_session_decision"] = _decision("invalid", [], False)
        sys.stdout.write(_canonical(result) + "\n")
        return 2
    try:
        data = _parse_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        result = _cli_error("invalid_json", type(exc).__name__)
        result["input_bytes"] = len(raw)
        sys.stdout.write(_canonical(result) + "\n")
        return 2
    result = resolve_ledger(data, raw_bytes=raw)
    sys.stdout.write(_canonical(result) + "\n")
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
