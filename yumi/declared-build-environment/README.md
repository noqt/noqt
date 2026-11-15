# Declared build environment evidence

`declared_build_environment_evidence.py` is a read-only, stdlib-only CLI for
checking whether explicitly mapped build environments name the literal
requirements in a repository's `pyproject.toml` `[build-system]` table. It
does not import or execute repository code, scripts, build backends, or
installers, and it makes no network requests.

Use Python 3.11 or later for `tomllib`:

```sh
python -B declared_build_environment_evidence.py \
  --root /path/to/repository \
  --routes /path/to/routes.json
```

The JSON result is written to stdout. Short maintainer evidence lines and the
overall binding digest are written to stderr. The CLI reads only the required
`pyproject.toml`, the route manifest, and repository files named in that
manifest. Repository paths must be relative POSIX paths below `--root`; symlink
paths and parent traversal are rejected.

## Route manifest

The manifest is explicit input, not an inferred workflow graph. Each route
names its entrypoint evidence and all files that declare the environment for
that route. `declarations_complete` is the maintainer's assertion that those
files are the complete declarations for that route. A `non-isolated` route
also needs a literal `--no-build-isolation` token in one of its mapped
entrypoints; otherwise its result is `UNKNOWN`. The selected route and the
completeness assertion are included in the route digest.

```json
{
  "schema": 1,
  "routes": [
    {
      "name": "editable build",
      "isolation": "non-isolated",
      "entrypoints": ["scripts/build.sh"],
      "declarations": ["reqs/build.pip"],
      "declarations_complete": true
    }
  ]
}
```

`isolation` accepts `non-isolated`, `isolated`, or `unknown`. It is a supplied
route mapping. For an isolated route, the tool checks the build requirements
declared in `pyproject.toml`; it does not independently prove how an external
frontend ran. For a non-isolated route, it checks the mapped environment
declarations. When one document contains unrelated workflows, a declaration
input can select inclusive one-based line bounds explicitly:

```json
"declarations": [
  {"path": "BUILDING.md", "lines": [53, 57]}
]
```

The selected source bytes, full file digest, and exact line range are all
bound. A line selection may cover at most 5,000 lines. The CLI does not infer
which section belongs to a route; maintainers supply that mapping.

## Status meanings

- **`DECLARED`** — the literal requirement appears in the supplied declaration
  set, or in `pyproject.toml` for a mapped isolated route.
- **`DECLARATION_GAP`** — the route is explicitly non-isolated, the mapping is
  marked complete, the supported declaration inputs are parseable for this
  package, and none names the build requirement.
- **`UNKNOWN`** — the route or build requirement is ambiguous, conditional,
  dynamic, opaque, unsupported, or the supplied declaration set is not marked
  complete.

A declaration gap is evidence only about the supplied mapping. It does not
prove actual environment absence, build failure, runtime behavior, version
compatibility, or installability. Package names are compared after PEP 503
normalization; version constraints are recorded as source text but are not
resolved against each other.

## Supported literal declaration forms

- `pyproject.toml`: a literal `[build-system].requires` array of requirement
  strings.
- `.pip` and `.txt`: simple requirement names with optional extras and version
  specifiers. Markers make that package `UNKNOWN`; includes, editable entries,
  and pip options make absent package results `UNKNOWN`.
- Conda `.yaml` / `.yml`: a simple `requirements.host` list. Selectors make
  that listed package `UNKNOWN`; Jinja, aliases, inline lists, and unsupported
  list syntax are opaque.
- `.md`: simple package installs inside fenced blocks using `pip install`,
  `uv pip install`, or `python -m pip install`. Editable project installs are
  not counted as declarations of the project's build tools.

An unconditional literal declaration for a package remains `DECLARED` even if
an unrelated package uses a marker. A conditional or unsupported declaration
for the requirement in question never becomes a forced gap. Unsupported file
formats are treated as opaque.

## Binding and bounds

The output records SHA-256 digests for the exact `pyproject.toml`, route
manifest, each explicitly mapped file, each route mapping, and the combined
binding. Route digests include the exact repository root, manifest digest,
route fields, file paths, and file content digests. Results are deterministic
for identical bytes and root path. Inputs are bounded to a 512 KiB manifest,
1 MiB per source file, 8 MiB total source bytes, 64 routes, and 32 paths per
route field.

## Validation status

This experimental command has been checked on Python 3.12.5 with nine focused
tests and one frozen public Core ML Tools declaration snapshot. Other
Python/platform combinations are unverified. These are static declaration
checks, not build, installation or runtime-compatibility tests. Route mapping,
line selection and completeness assertions remain caller supplied; their
manual cost is a real limitation.
