# Yumi Python Import Layout Evidence

This directory contains a standalone, standard-library-only Python CLI for comparing the import layout in one local wheel or gzip sdist with one pinned local source snapshot. It also inspects only the consumer Python files you name for static imports and guarded or fallback import evidence.

The tool runs locally. It does not install, import, execute or extract the subject package. It has no network or subprocess path.

## Download

- [`python-import-layout-evidence.py`](./python-import-layout-evidence.py)
- SHA-256: `c4a7459c81caf5c6dae4dcfb0a79cd66b479310475d82a47c171a05f48493d0c`
- [Worked documentation](https://noqt.no-qt.chatgpt.site/yumi/python-import-layout)

Verify the downloaded file before use:

```sh
python -c "import hashlib,pathlib; p=pathlib.Path('python-import-layout-evidence.py'); print(hashlib.sha256(p.read_bytes()).hexdigest())"
```

## Use

Name the archive, source snapshot, each consumer file, and each dotted import target explicitly:

```sh
python python-import-layout-evidence.py \
  --archive ./matbench_discovery-1.3.1-py2.py3-none-any.whl \
  --source-root ./matbench-discovery-eaa755066edccbc5224b0404369e626e7f2294fd \
  --consumer-file ./matbench-discovery-eaa755066edccbc5224b0404369e626e7f2294fd/scripts/model_figs/cumulative_metrics.py \
  --consumer-file ./matbench-discovery-eaa755066edccbc5224b0404369e626e7f2294fd/scripts/evals/discovery.py \
  --target matbench_discovery.metrics.discovery \
  --target matbench_discovery.preds.discovery \
  --target matbench_discovery.hpc \
  --target matbench_discovery.remote.fetch
```

Use `--consumer-root DIR` when a named consumer file sits outside the source snapshot. That option permits only the explicit `--consumer-file` paths; it does not scan the extra root.

The command writes deterministic, bounded JSON to stdout. Selected-target availability is reported as `MODULE_FILE`, `REGULAR_PACKAGE`, `NAMESPACE_POSSIBLE`, `ABSENT` or `UNKNOWN`. A namespace result is structural evidence only. Dynamic imports, malformed or unresolved syntax, skipped links or junctions, and insufficient snapshots produce limited or unknown evidence rather than a runtime verdict.

## Worked identity boundary

The currently served PyPI `matbench-discovery` 1.3.1 wheel and sdist have flat `metrics.py` and `preds.py` modules, while source commit [`eaa7550`](https://github.com/janosh/matbench-discovery/commit/eaa755066edccbc5224b0404369e626e7f2294fd) has nested `metrics/discovery.py` and `preds/discovery.py` paths and still reports version 1.3.1. That supports a source-versus-artifact import-layout difference for selected paths.

It does not show that the wheel differs from its own release tag. The upstream [`v1.3.1` release](https://github.com/janosh/matbench-discovery/releases/tag/v1.3.1) points to `38a6e17`, whose metrics source is flat. A matching version label across later source and older artifacts does not establish byte identity, intent, corruption, replacement, or which bytes a historical run used.

ReproLab's comparison code is material contrary evidence to an unconditional failure claim: it prepends a clone, tries both nested and flat metric entrypoints, and can continue with independent recomputation when neither import is available. See its [comparison script](https://github.com/maruyamakoju/reprolab/blob/efd92f4e8edc8324f66d4c126c967278d962709e/scripts/compare_metrics.py) and [failure notes](https://github.com/maruyamakoju/reprolab/blob/efd92f4e8edc8324f66d4c126c967278d962709e/papers/matbench-discovery/failure_notes.md).

## Limits

This is a bounded file-layout comparison and static AST inspection, not a full Python resolver. It does not establish runtime import success, behavior, installability, dependency resolution, vulnerability, security, authenticity, historical artifact identity, or scientific reproducibility. The original audit artifact identity remains unknown unless its exact bytes or digest are supplied.

The CLI enforces limits on input bytes, expanded TAR bytes, archive members, per-member and total sizes, compression ratio, source entries and Python bytes, consumer files, AST nodes and depth, selected targets, path lengths, and JSON output. It rejects archive traversal, duplicate normalized paths, unsafe special members, symlink or reparse-point input paths, and inconsistent ZIP directory records.

## License

The three files in this directory are provided under the [MIT License](./LICENSE). This directory-level license does not change the licensing of other files in the `noqt/noqt` repository.
