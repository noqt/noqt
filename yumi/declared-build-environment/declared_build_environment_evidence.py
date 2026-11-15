#!/usr/bin/env python3
"""Conservative, read-only evidence for declared build requirements.

This tool reads only pyproject.toml plus paths explicitly listed in a JSON
route manifest. It never imports or executes repository files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python before 3.11 has no stdlib TOML reader.
    tomllib = None  # type: ignore[assignment]


MAX_CONTROL_BYTES = 512 * 1024
MAX_SOURCE_FILE_BYTES = 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 8 * 1024 * 1024
MAX_ROUTES = 64
MAX_DECLARATIONS_PER_ROUTE = 32
MAX_REQUIREMENTS = 256
MAX_EVIDENCE_ITEMS = 32
MAX_SELECTED_LINES = 5000

STATUS_DECLARED = "DECLARED"
STATUS_GAP = "DECLARATION_GAP"
STATUS_UNKNOWN = "UNKNOWN"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQ_HEAD = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.\-]+\])?(.*)$"
)
_SIMPLE_VERSION = re.compile(
    r"^[\s]*(?:(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_\-]+"
    r"(?:\s*,\s*(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_\-]+)*)?\s*$"
)
_FLAG = re.compile(r"(?<![A-Za-z0-9_-])--no-build-isolation(?![A-Za-z0-9_-])")


class InputError(ValueError):
    """The supplied input is invalid or exceeds a documented bound."""


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_bytes(path: Path, limit: int, label: str) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise InputError(f"cannot read {label}: {path}: {exc.strerror or type(exc).__name__}") from None
    if len(data) > limit:
        raise InputError(f"{label} exceeds the {limit}-byte limit: {path}")
    return data


def _repo_file(root: Path, value: Any) -> tuple[str, Path]:
    if not isinstance(value, str) or not value:
        raise InputError("repository file paths must be non-empty strings")
    if "\\" in value or ":" in value:
        raise InputError(f"repository path must use a relative POSIX path: {value}")
    rel = PurePosixPath(value)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise InputError(f"repository path must stay below the root: {value}")
    candidate = root
    for part in rel.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise InputError(f"symlink repository paths are unsupported: {value}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise InputError(f"repository path is missing or escapes the root: {value}") from None
    if not resolved.is_file():
        raise InputError(f"repository path is not a file: {value}")
    return rel.as_posix(), resolved


@dataclass
class Facts:
    unconditional: set[str] = field(default_factory=set)
    conditional: set[str] = field(default_factory=set)
    uncertain: set[str] = field(default_factory=set)
    opaque: bool = False
    issues: list[str] = field(default_factory=list)

    def issue(self, message: str, *, opaque: bool = False) -> None:
        if opaque:
            self.opaque = True
        if message not in self.issues and len(self.issues) < MAX_EVIDENCE_ITEMS:
            self.issues.append(message)

    def add(self, name: str, state: str) -> None:
        if state == "unconditional":
            self.unconditional.add(name)
        elif state == "conditional":
            self.conditional.add(name)
        else:
            self.uncertain.add(name)


def _strip_comment(line: str) -> str:
    """Remove a pip-style comment outside quotes; keep marker syntax intact."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _parse_requirement(value: str) -> tuple[str | None, str]:
    """Return normalized name and unconditional/conditional/uncertain state."""
    text = value.strip()
    if not text:
        return None, "uncertain"
    match = _REQ_HEAD.match(text)
    if not match:
        return None, "uncertain"
    name = normalize_name(match.group(1))
    remainder = match.group(2).strip()
    if " @ " in remainder or remainder.startswith("@"):
        return name, "uncertain"
    marker = None
    if ";" in remainder:
        remainder, marker = remainder.split(";", 1)
        if not marker.strip():
            return name, "uncertain"
    if not _SIMPLE_VERSION.fullmatch(remainder):
        return name, "uncertain"
    if marker is not None:
        return name, "conditional"
    return name, "unconditional"


def parse_pyproject(data: bytes) -> tuple[list[dict[str, str]], bool, list[str]]:
    """Read only [build-system].requires from TOML; never evaluate providers."""
    issues: list[str] = []
    if tomllib is None:
        return [], True, ["Python 3.11+ is required for stdlib TOML parsing"]
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        return [], True, [f"pyproject.toml is not supported TOML ({type(exc).__name__})"]
    system = parsed.get("build-system")
    if not isinstance(system, dict):
        return [], True, ["[build-system] is missing or not a table"]
    raw_requires = system.get("requires")
    if not isinstance(raw_requires, list):
        return [], True, ["[build-system].requires is not a literal array"]
    if len(raw_requires) > MAX_REQUIREMENTS:
        return [], True, ["[build-system].requires exceeds the requirement limit"]
    requirements: list[dict[str, str]] = []
    incomplete = False
    for raw in raw_requires:
        if not isinstance(raw, str):
            incomplete = True
            issues.append("a build requirement is not a string")
            continue
        name, state = _parse_requirement(raw)
        if name is None:
            incomplete = True
            issues.append("a build requirement uses unsupported syntax")
            continue
        requirements.append({"raw": raw, "name": name, "state": state})
        if state == "uncertain":
            incomplete = True
            issues.append(f"unsupported build requirement syntax for {name}")
    return requirements, incomplete, issues[:MAX_EVIDENCE_ITEMS]


def parse_requirements_file(data: bytes, label: str) -> Facts:
    facts = Facts()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        facts.issue(f"{label}: non-UTF-8 requirements file", opaque=True)
        return facts
    for number, raw_line in enumerate(text.splitlines(), 1):
        if len(raw_line) > 16_384:
            facts.issue(f"{label}:{number}: line exceeds parser bound", opaque=True)
            continue
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("-"):
            facts.issue(f"{label}:{number}: include, option, or editable requirement is unsupported", opaque=True)
            continue
        name, state = _parse_requirement(line)
        if name is None:
            facts.issue(f"{label}:{number}: requirement syntax is unsupported", opaque=True)
        else:
            facts.add(name, state)
            if state == "uncertain":
                facts.issue(f"{label}:{number}: unsupported specifier for {name}")
    return facts


def _simple_conda_item(text: str) -> tuple[str | None, str]:
    value = text.strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        end = value.find(quote, 1)
        if end < 0:
            return None, "uncertain"
        spec = value[1:end]
        tail = value[end + 1 :].strip()
        if tail:
            return None, "uncertain"
        value = spec
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\s+(.+))?", value)
    if not match:
        return None, "uncertain"
    name = normalize_name(match.group(1))
    spec = (match.group(2) or "").strip()
    if spec and not re.fullmatch(r"(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_\-]+", spec):
        return name, "uncertain"
    return name, "unconditional"


def parse_conda_meta(data: bytes, label: str) -> Facts:
    """Read only a literal requirements.host YAML block in a narrow subset."""
    facts = Facts()
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        facts.issue(f"{label}: non-UTF-8 YAML", opaque=True)
        return facts
    requirements_indent: int | None = None
    host_indent: int | None = None
    in_requirements = False
    in_host = False
    host_seen = False
    for number, raw in enumerate(lines, 1):
        if len(raw) > 16_384:
            if in_host:
                facts.issue(f"{label}:{number}: YAML line exceeds parser bound", opaque=True)
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key = re.fullmatch(r"([A-Za-z0-9_-]+)\s*:\s*", stripped)
        if key and key.group(1) == "requirements":
            requirements_indent = indent
            host_indent = None
            in_requirements = True
            in_host = False
            continue
        if in_requirements and requirements_indent is not None and indent <= requirements_indent:
            in_requirements = False
            in_host = False
            host_indent = None
        if not in_requirements:
            continue
        if key and key.group(1) == "host" and indent > (requirements_indent or -1):
            host_seen = True
            host_indent = indent
            in_host = True
            tail = stripped.split(":", 1)[1].strip()
            if tail:
                facts.issue(f"{label}:{number}: inline host YAML is unsupported", opaque=True)
            continue
        if in_host and host_indent is not None and indent <= host_indent:
            in_host = False
            host_indent = None
        if not in_host:
            continue
        item = re.match(r"^\s*-\s*(.*?)\s*(?:#\s*\[([^\]]+)\])?\s*$", raw)
        if not item:
            facts.issue(f"{label}:{number}: host requirements use unsupported YAML", opaque=True)
            continue
        value, selector = item.group(1).strip(), item.group(2)
        if not value:
            facts.issue(f"{label}:{number}: empty host requirement", opaque=True)
            continue
        if "{{" in value or "}}" in value or "${" in value or "&" in value or "*" in value:
            facts.issue(f"{label}:{number}: dynamic or aliased host requirement", opaque=True)
            continue
        name, state = _simple_conda_item(value)
        if name is None:
            facts.issue(f"{label}:{number}: host requirement syntax is unsupported", opaque=True)
            continue
        if state == "uncertain":
            facts.add(name, "uncertain")
            facts.issue(f"{label}:{number}: unsupported host specifier for {name}")
        elif selector:
            facts.add(name, "conditional")
        else:
            facts.add(name, "unconditional")
    if not host_seen:
        facts.issue(f"{label}: requirements.host is not a supported literal list", opaque=True)
    return facts


def _install_command(tokens: list[str]) -> tuple[int, int] | None:
    if len(tokens) >= 3 and tokens[0] == "uv" and tokens[1:3] == ["pip", "install"]:
        return 3, 3
    if len(tokens) >= 2 and tokens[:2] == ["pip", "install"]:
        return 2, 2
    if len(tokens) >= 4 and tokens[:3] in (["python", "-m", "pip"], ["python3", "-m", "pip"]) and tokens[3] == "install":
        return 4, 4
    return None


def parse_markdown_commands(data: bytes, label: str, line_offset: int = 0) -> Facts:
    """Read literal pip package-install commands inside fenced Markdown blocks."""
    facts = Facts()
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        facts.issue(f"{label}: non-UTF-8 Markdown", opaque=True)
        return facts
    in_fence = False
    fence_char = ""
    fence_len = 0
    pending = ""
    for number, line in enumerate(lines, 1 + line_offset):
        if len(line) > 16_384:
            if in_fence:
                facts.issue(f"{label}:{number}: Markdown line exceeds parser bound", opaque=True)
            continue
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
                pending = ""
            continue
        if not in_fence:
            continue
        command_part = _strip_comment(line).rstrip()
        if not command_part and not pending:
            continue
        continued = command_part.endswith("\\")
        if continued:
            pending += command_part[:-1] + " "
            continue
        command = pending + command_part
        pending = ""
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            facts.issue(f"{label}:{number}: shell command quoting is unsupported", opaque=True)
            continue
        located = _install_command(tokens)
        if located is None:
            head = tokens[:5]
            if (
                len(head) >= 2
                and head[:2] in (
                    ["conda", "install"],
                    ["mamba", "install"],
                    ["micromamba", "install"],
                    ["poetry", "add"],
                    ["uv", "add"],
                    ["uv", "sync"],
                )
            ) or (
                "install" in head
                and (tokens[0].startswith("pip") or any(token == "pip" for token in head))
            ):
                facts.issue(f"{label}:{number}: unsupported package installation command", opaque=True)
            continue
        start, _ = located
        args = tokens[start:]
        options_without_value = {"--no-build-isolation", "--no-deps", "--upgrade", "--user", "-U"}
        options_with_value = {"--config-settings", "--index-url", "--extra-index-url", "--constraint", "-c"}
        packages: list[str] = []
        index = 0
        unsupported = False
        project_install = False
        while index < len(args):
            token = args[index]
            if token in ("-e", "--editable"):
                project_install = True
                if index + 1 >= len(args):
                    unsupported = True
                    break
                index += 2
                continue
            if token in (".", "./", "-."):
                project_install = True
                index += 1
                continue
            if token in options_without_value:
                index += 1
                continue
            if token in options_with_value:
                if index + 1 >= len(args):
                    unsupported = True
                    break
                index += 2
                continue
            if token in ("-r", "--requirement", "--editable", "-e") or token.startswith("-"):
                unsupported = True
                break
            if "$" in token or "`" in token:
                unsupported = True
                break
            packages.append(token)
            index += 1
        if unsupported:
            facts.issue(f"{label}:{number}: pip command contains unsupported options or dynamic values", opaque=True)
            continue
        if project_install:
            if packages:
                facts.issue(f"{label}:{number}: pip command mixes a project and package installation", opaque=True)
            # Installing the project itself is a build invocation, not a
            # declaration of its build requirements.
            continue
        for package in packages:
            name, state = _parse_requirement(package)
            if name is None:
                facts.issue(f"{label}:{number}: pip package token is unsupported", opaque=True)
            else:
                facts.add(name, state)
                if state == "uncertain":
                    facts.issue(f"{label}:{number}: unsupported pip specifier for {name}")
    if pending:
        facts.issue(f"{label}: unterminated shell continuation", opaque=True)
    return facts


def parse_declaration_file(path: str, data: bytes, line_offset: int = 0) -> Facts:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in (".txt", ".pip"):
        return parse_requirements_file(data, path)
    if suffix in (".yaml", ".yml"):
        return parse_conda_meta(data, path)
    if suffix == ".md":
        return parse_markdown_commands(data, path, line_offset)
    facts = Facts()
    facts.issue(f"{path}: declaration format is unsupported", opaque=True)
    return facts


def _manifest(data: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InputError(f"route manifest is not valid UTF-8 JSON ({type(exc).__name__})") from None
    if not isinstance(decoded, dict) or set(decoded) != {"schema", "routes"}:
        raise InputError("route manifest must contain only schema and routes")
    if decoded.get("schema") != 1:
        raise InputError("route manifest schema must be 1")
    routes = decoded.get("routes")
    if not isinstance(routes, list) or not routes or len(routes) > MAX_ROUTES:
        raise InputError(f"route manifest must contain 1..{MAX_ROUTES} routes")
    seen: set[str] = set()
    normalized_routes: list[dict[str, Any]] = []
    for index, route in enumerate(routes):
        allowed = {"name", "isolation", "entrypoints", "declarations", "declarations_complete"}
        if not isinstance(route, dict) or set(route) != allowed:
            raise InputError(f"route {index + 1} has missing or unsupported fields")
        name = route.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise InputError(f"route {index + 1} has an invalid name")
        if name in seen:
            raise InputError(f"duplicate route name: {name}")
        seen.add(name)
        if route.get("isolation") not in ("non-isolated", "isolated", "unknown"):
            raise InputError(f"route {name}: isolation must be non-isolated, isolated, or unknown")
        entrypoints = route.get("entrypoints")
        if (
            not isinstance(entrypoints, list)
            or not 1 <= len(entrypoints) <= MAX_DECLARATIONS_PER_ROUTE
            or any(not isinstance(item, str) or not item for item in entrypoints)
        ):
            raise InputError(f"route {name}: entrypoints must contain 1..{MAX_DECLARATIONS_PER_ROUTE} paths")
        if len(set(entrypoints)) != len(entrypoints):
            raise InputError(f"route {name}: duplicate entrypoint path")
        declarations = route.get("declarations")
        if (
            not isinstance(declarations, list)
            or not 1 <= len(declarations) <= MAX_DECLARATIONS_PER_ROUTE
        ):
            raise InputError(f"route {name}: declarations must contain 1..{MAX_DECLARATIONS_PER_ROUTE} inputs")
        normalized_declarations: list[dict[str, Any]] = []
        seen_declarations: set[tuple[str, tuple[int, int] | None]] = set()
        for item in declarations:
            if isinstance(item, str):
                path = item
                line_range = None
            elif isinstance(item, dict) and set(item) == {"path", "lines"}:
                path = item.get("path")
                raw_range = item.get("lines")
                if (
                    not isinstance(raw_range, list)
                    or len(raw_range) != 2
                    or any(not isinstance(value, int) or isinstance(value, bool) for value in raw_range)
                ):
                    raise InputError(f"route {name}: declaration lines must be [start, end]")
                start, end = raw_range
                if start < 1 or end < start or end - start + 1 > MAX_SELECTED_LINES:
                    raise InputError(f"route {name}: declaration line range is invalid or exceeds the bound")
                line_range = (start, end)
            else:
                raise InputError(f"route {name}: declaration input must be a path or path plus line range")
            if not isinstance(path, str) or not path:
                raise InputError(f"route {name}: declaration path must be a non-empty string")
            identity = (path, line_range)
            if identity in seen_declarations:
                raise InputError(f"route {name}: duplicate declaration input")
            seen_declarations.add(identity)
            normalized_declarations.append(
                {"path": path, "lines": list(line_range) if line_range is not None else None}
            )
        if not isinstance(route.get("declarations_complete"), bool):
            raise InputError(f"route {name}: declarations_complete must be boolean")
        normalized_routes.append(
            {
                "name": name,
                "isolation": route["isolation"],
                "entrypoints": list(entrypoints),
                "declarations": normalized_declarations,
                "declarations_complete": route["declarations_complete"],
            }
        )
    return {"schema": 1, "routes": normalized_routes}


def _selected_source(data: bytes, line_range: list[int] | None, label: str) -> tuple[bytes, int]:
    if line_range is None:
        return data, 0
    try:
        lines = data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        raise InputError(f"line-selected declaration is not UTF-8: {label}") from None
    start, end = line_range
    if end > len(lines):
        raise InputError(f"declaration line range exceeds file length: {label}")
    selected = "".join(lines[start - 1 : end]).encode("utf-8")
    return selected, start - 1


def _token_present(data: bytes, token: re.Pattern[str]) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if token.search(line):
            return True
    return False


def analyze(root_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    root = Path(root_path).resolve(strict=True)
    if not root.is_dir():
        raise InputError(f"repository root is not a directory: {root}")
    manifest_file = Path(manifest_path).resolve(strict=True)
    if not manifest_file.is_file():
        raise InputError(f"route manifest is not a file: {manifest_file}")
    manifest_bytes = _read_bytes(manifest_file, MAX_CONTROL_BYTES, "route manifest")
    manifest = _manifest(manifest_bytes)
    manifest_digest = sha256(manifest_bytes)

    source_bytes: dict[str, bytes] = {}
    roles: dict[str, set[str]] = {}
    total_bytes = 0

    def load_repo_file(value: str, role: str) -> tuple[str, bytes]:
        nonlocal total_bytes
        rel, file_path = _repo_file(root, value)
        roles.setdefault(rel, set()).add(role)
        if rel not in source_bytes:
            data = _read_bytes(file_path, MAX_SOURCE_FILE_BYTES, "repository source")
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_SOURCE_BYTES:
                raise InputError(f"explicit repository inputs exceed the {MAX_TOTAL_SOURCE_BYTES}-byte total limit")
            source_bytes[rel] = data
        return rel, source_bytes[rel]

    _, pyproject_bytes = load_repo_file("pyproject.toml", "build-system")
    build_requirements, build_incomplete, build_issues = parse_pyproject(pyproject_bytes)
    if not build_requirements:
        build_incomplete = True
        if not build_issues:
            build_issues.append("no literal build requirements were parsed")

    parsed_declarations: dict[tuple[str, tuple[int, int] | None], Facts] = {}
    route_inputs: list[dict[str, Any]] = []
    for raw_route in manifest["routes"]:
        route = dict(raw_route)
        entrypoint_paths: list[str] = []
        declaration_inputs: list[dict[str, Any]] = []
        for value in route["entrypoints"]:
            rel, _ = load_repo_file(value, "entrypoint")
            entrypoint_paths.append(rel)
        for declaration in route["declarations"]:
            rel, data = load_repo_file(declaration["path"], "declaration")
            line_range = tuple(declaration["lines"]) if declaration["lines"] is not None else None
            identity = (rel, line_range)
            declaration_inputs.append(
                {"path": rel, **({"lines": list(line_range)} if line_range is not None else {})}
            )
            if identity not in parsed_declarations:
                selected, line_offset = _selected_source(data, declaration["lines"], rel)
                parsed_declarations[identity] = parse_declaration_file(rel, selected, line_offset)
        route_inputs.append(
            {
                "name": route["name"],
                "isolation": route["isolation"],
                "entrypoints": sorted(entrypoint_paths),
                "declarations": sorted(declaration_inputs, key=lambda item: (item["path"], item.get("lines", []))),
                "declarations_complete": route["declarations_complete"],
            }
        )

    file_digests = [
        {
            "path": path,
            "roles": sorted(roles[path]),
            "sha256": sha256(source_bytes[path]),
            "size_bytes": len(source_bytes[path]),
        }
        for path in sorted(source_bytes)
    ]
    digest_by_path = {item["path"]: item["sha256"] for item in file_digests}
    required_names = {item["name"] for item in build_requirements}
    results: list[dict[str, Any]] = []
    for route_input in route_inputs:
        isolation = route_input["isolation"]
        route_issues: list[str] = []
        mode_supported = True
        if isolation == "non-isolated":
            found_flag = any(
                _token_present(source_bytes[path], _FLAG) for path in route_input["entrypoints"]
            )
            if not found_flag:
                mode_supported = False
                route_issues.append("no literal --no-build-isolation token was found in the mapped entrypoints")
        declaration_facts = [
            parsed_declarations[
                (item["path"], tuple(item["lines"]) if "lines" in item else None)
            ]
            for item in route_input["declarations"]
        ]
        aggregate = Facts()
        for facts in declaration_facts:
            aggregate.unconditional.update(facts.unconditional)
            aggregate.conditional.update(facts.conditional)
            aggregate.uncertain.update(facts.uncertain)
            aggregate.opaque = aggregate.opaque or facts.opaque
            for issue in facts.issues:
                aggregate.issue(issue)

        requirement_results: list[dict[str, str]] = []
        for requirement in build_requirements:
            name = requirement["name"]
            state = STATUS_UNKNOWN
            reason = "route or build requirements could not be resolved conservatively"
            if build_incomplete:
                reason = "pyproject build requirements contain unsupported or dynamic syntax"
            elif requirement["state"] == "conditional":
                reason = "the build requirement itself is conditional"
            elif requirement["state"] == "uncertain":
                reason = "the build requirement syntax is unsupported"
            elif isolation == "isolated":
                state = STATUS_DECLARED
                reason = "the literal build requirement is in pyproject.toml for the mapped isolated route"
            elif isolation == "unknown" or not mode_supported:
                reason = "the mapped route isolation mode is unknown"
            elif name in aggregate.unconditional:
                state = STATUS_DECLARED
                reason = "the package name appears in an unconditional mapped environment declaration"
            elif name in aggregate.conditional or name in aggregate.uncertain:
                reason = "the package has only a conditional or unsupported mapped declaration"
            elif aggregate.opaque:
                reason = "an opaque mapped declaration could affect whether this package is supplied"
            elif not route_input["declarations_complete"]:
                reason = "the route mapping does not attest that all environment declarations were supplied"
            else:
                state = STATUS_GAP
                reason = "the complete mapped declarations do not name this build requirement"
            requirement_results.append(
                {"requirement": requirement["raw"], "name": name, "status": state, "reason": reason}
            )

        statuses = {item["status"] for item in requirement_results}
        if STATUS_GAP in statuses:
            route_status = STATUS_GAP
        elif STATUS_UNKNOWN in statuses or not requirement_results:
            route_status = STATUS_UNKNOWN
        else:
            route_status = STATUS_DECLARED
        if route_issues:
            route_status = STATUS_UNKNOWN
            for issue in route_issues:
                aggregate.issue(issue)
        if build_issues:
            for issue in build_issues:
                aggregate.issue(issue)

        files_for_route = sorted(
            set(route_input["entrypoints"] + [item["path"] for item in route_input["declarations"]])
        )
        route_binding = {
            "root": str(root),
            "name": route_input["name"],
            "isolation": isolation,
            "entrypoints": route_input["entrypoints"],
            "declarations": route_input["declarations"],
            "declarations_complete": route_input["declarations_complete"],
            "files": [{"path": path, "sha256": digest_by_path[path]} for path in files_for_route],
            "pyproject_sha256": digest_by_path["pyproject.toml"],
            "manifest_path": str(manifest_file),
            "manifest_sha256": manifest_digest,
        }
        route_digest = sha256(canonical_json(route_binding))
        results.append(
            {
                "name": route_input["name"],
                "isolation": isolation,
                "status": route_status,
                "digest": route_digest,
                "entrypoints": route_input["entrypoints"],
                "declarations": route_input["declarations"],
                "declarations_complete": route_input["declarations_complete"],
                "requirements": requirement_results,
                "issues": sorted(aggregate.issues)[:MAX_EVIDENCE_ITEMS],
            }
        )

    overall_status = (
        STATUS_GAP
        if any(route["status"] == STATUS_GAP for route in results)
        else STATUS_UNKNOWN
        if any(route["status"] == STATUS_UNKNOWN for route in results)
        else STATUS_DECLARED
    )
    binding = {
        "root": str(root),
        "pyproject_sha256": digest_by_path["pyproject.toml"],
        "manifest": {"path": str(manifest_file), "sha256": manifest_digest},
        "files": [{"path": item["path"], "sha256": item["sha256"]} for item in file_digests],
        "routes": [{"name": route["name"], "digest": route["digest"]} for route in results],
    }
    evidence: list[str] = []
    for route in results:
        pieces = [f"{route['name']}: {route['status']} ({route['isolation']})"]
        for requirement in route["requirements"]:
            pieces.append(f"{requirement['name']} {requirement['status']}")
        evidence.append("; ".join(pieces))
    return {
        "schema": 1,
        "tool": "declared-build-environment-evidence",
        "status": overall_status,
        "scope_note": (
            "A declaration gap means the supplied complete mapping omits a literal build requirement; "
            "it does not establish actual environment absence, build failure, runtime behavior, or installability."
        ),
        "root": str(root),
        "pyproject": {"path": "pyproject.toml", "sha256": digest_by_path["pyproject.toml"]},
        "manifest": {"path": str(manifest_file), "sha256": manifest_digest},
        "binding_sha256": sha256(canonical_json(binding)),
        "files": file_digests,
        "build_requirements": build_requirements,
        "build_requirements_incomplete": build_incomplete,
        "build_requirement_issues": sorted(set(build_issues))[:MAX_EVIDENCE_ITEMS],
        "routes": results,
        "maintainer_evidence": evidence,
        "limits": {
            "source_parser": "literal stdlib-only subset; dynamic, opaque, conditional, or unsupported relevant syntax yields UNKNOWN",
            "version_compatibility": "package names are compared; version satisfiability is not evaluated",
            "execution": "no repository code, shell script, build, installer, or network operation was run",
            "bounds": {
                "manifest_bytes": MAX_CONTROL_BYTES,
                "source_file_bytes": MAX_SOURCE_FILE_BYTES,
                "total_source_bytes": MAX_TOTAL_SOURCE_BYTES,
                "routes": MAX_ROUTES,
                "paths_per_route_field": MAX_DECLARATIONS_PER_ROUTE,
                "selected_lines_per_declaration": MAX_SELECTED_LINES,
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare literal PEP 517 build requirements to explicitly mapped route declarations."
    )
    parser.add_argument("--root", required=True, help="exact repository root containing pyproject.toml")
    parser.add_argument("--routes", required=True, help="explicit schema-1 JSON route manifest")
    args = parser.parse_args(argv)
    try:
        result = analyze(args.root, args.routes)
    except (InputError, OSError) as exc:
        print(json.dumps({"schema": 1, "status": STATUS_UNKNOWN, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print("Maintainer evidence:", file=sys.stderr)
    for line in result["maintainer_evidence"]:
        print(f"- {line}", file=sys.stderr)
    print(f"- binding sha256: {result['binding_sha256']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
