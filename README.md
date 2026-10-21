# noqt

noqt builds open-source software for security and risk. Explore our work at [noqt](https://noqt.no-qt.chatgpt.site).

## A `10` versus two `1`s. Which side wins?

Both sides use the same three weights. The `10` sits under only 10% of the weight; the two `1`s sit under the other 90%. [Predict the winner, then reveal every term](https://github.com/noqt/yumi-decision-ledger/blob/main/curiosities/same-attention-weights.md).

## Yumi continuity for agent memory

Lifecycle note: The standalone [Yumi decision ledger package](https://github.com/noqt/yumi-decision-ledger) contains already-published checks for supplied handoff cards and supplied snapshots with `scope` and `settings` against a supplied current decision ledger. This link is navigation only.

Resume a task from a revision ledger without treating every old assertion as current. Yumi resolves supersession and retractions, flags unresolved forks or missing history, and returns revision/source chains plus changes since prior heads.

[Inspect the resolver, schema and example](yumi/continuity/v1/README.md). It runs locally with Python 3.11 and no third-party packages. Only run it if you’re already authorized to download and execute code and read the ledger. It doesn’t upload your data. It checks ledger relationships—not whether the recorded assertions are true.

Agents can also [inspect the optional continuity skill](yumi/skills/yumi-continuity/SKILL.md). The skill links the same commit-pinned resolver; installing or reading it does not grant ledger access or make recorded claims true.

### Leave a Threadmark

[Yumi Threadmark v1.1](yumi/continuity/v1.1/README.md) produces a bounded JSON continuity receipt from a caller-supplied revision ledger. It shows whether required keys agree across every supplied head, or are missing, withdrawn or conflicting, and carries inert producer/documentation metadata. `READY` means consistency only within that supplied ledger—not truth, authority, freshness, completeness, permission or safety.

## Exact CIDR coverage without an install

Before is `192.0.2.0/25`; after is `192.0.2.0/24`. How many IPv4 addresses were added, and what is the first one? [Predict first, then reveal the endpoint result](https://noqt.no-qt.chatgpt.site/yumi/network-sets). You can edit both sets. Underneath, [Yumi Network Set Evidence](yumi/network-sets/README.md) compares supplied IPv4 and IPv6 CIDR sets: normalized coverage, minimal added/removed CIDRs, exact address counts and distinguishing addresses, through an anonymous [HTTP API](https://noqt.no-qt.chatgpt.site/api/yumi/network-set-evidence).

Public, non-secret inputs only. No model, account or package installation is needed for the HTTP path. Local Python `ipaddress` and other tools remain valid alternatives. This does not assess reachability, security, routing or whether network changes should be applied.

## Zumi kept receipts

| First synthetic frame | Second synthetic frame |
|:--:|:--:|
| [![First retained synthetic Zumi frame](https://noqt.no-qt.chatgpt.site/zumi-session-capsule/v1/frame-01.webp)](https://noqt.no-qt.chatgpt.site/research/zumi-one-session-two-frames-one-observation) | [![Second retained synthetic Zumi frame](https://noqt.no-qt.chatgpt.site/zumi-session-capsule/v1/frame-02.webp)](https://noqt.no-qt.chatgpt.site/research/zumi-one-session-two-frames-one-observation) |

Two simulated phone-style frames. One stored technical observation. The same persisted session, checked after restart.

[Inspect the static replay and exact hashes](https://noqt.no-qt.chatgpt.site/research/zumi-one-session-two-frames-one-observation). It isn't a downloadable product or a real-camera demo, and the public page doesn't rerun the private app or model.

## Try Lumi Eggcracker without installing anything

If you use the authenticated GitHub CLI, the helper creates or reuses your exact
fork, checks that its workflow matches noqt's reviewed version, dispatches it on a
disposable GitHub runner and, when it can identify the exact run, prints commands
to watch it and read the result.

[Start the hosted proof from your terminal](https://github.com/noqt/Lumi-Eggcracker/blob/main/TRY_IT.md)

The proof tests the bounded kill mechanism. It does not install the product,
recognise an AI workload, or prove the whole product.

If it passes, returns a redacted failure, or feels confusing, [share the public run through the current-use result form](https://github.com/noqt/Lumi-Eggcracker/issues/new?template=hosted_probe_result.yml).

