#!/usr/bin/env python3
"""Bounded, local-only Python import-layout evidence for explicit inputs."""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import sys
import tarfile
import zipfile
import zlib


# Every scan and report has an explicit ceiling.
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_MEMBER_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_TAR_DECOMPRESSED_BYTES = MAX_ARCHIVE_TOTAL_BYTES
MAX_ARCHIVE_INSPECTED_PY_BYTES = 12 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200
MAX_MEMBER_PATH_CHARS = 240
MAX_SOURCE_ENTRIES = 20000
MAX_SOURCE_PY_FILES = 1000
MAX_SOURCE_FILE_BYTES = 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 20 * 1024 * 1024
MAX_CONSUMERS = 16
MAX_CONSUMER_FILE_BYTES = 512 * 1024
MAX_CONSUMER_TOTAL_BYTES = 4 * 1024 * 1024
MAX_AST_NODES = 20000
MAX_AST_DEPTH = 80
MAX_TARGETS = 32
MAX_TARGET_CHARS = 200
MAX_OUTPUT_BYTES = 256 * 1024
MAX_OUTPUT_PATHS = 120
MAX_OUTPUT_IMPORTS = 100
MAX_OUTPUT_MATCHES_PER_TARGET = 8
MAX_IMPORT_LABEL_CHARS = 200


class InputError(Exception):
    """An invalid, unsafe, or over-bound input."""


def _is_reparse_point(info) -> bool:
    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attribute and getattr(info, "st_file_attributes", 0) & attribute)


def _path_has_link_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path)))
    components = list(reversed(absolute.parents)) + [absolute]
    for component in components:
        try:
            info = component.lstat()
        except OSError as exc:
            raise InputError("a supplied path component cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            return True
        is_junction = getattr(component, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
    return False


def _checked_path(value: str, kind: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(os.fspath(path)))
    if _path_has_link_component(path):
        raise InputError(f"{kind} path contains a symlink, junction, or reparse point")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InputError(f"{kind} path does not exist or cannot be resolved") from exc
    if kind == "archive":
        if not resolved.is_file():
            raise InputError("archive input must be a regular file")
    elif not resolved.is_dir():
        raise InputError(f"{kind} input must be a directory")
    return resolved


def _checked_consumer(value: str, roots: list[tuple[str, Path]]) -> tuple[Path, str, str]:
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(os.fspath(path)))
    if _path_has_link_component(path):
        raise InputError("consumer path contains a symlink, junction, or reparse point")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InputError("consumer file does not exist or cannot be resolved") from exc
    if not resolved.is_file() or resolved.suffix != ".py":
        raise InputError("each consumer must be an explicit regular .py file")
    for label, root in roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        relative_text = relative.as_posix()
        if len(relative_text) > MAX_MEMBER_PATH_CHARS:
            raise InputError("consumer path exceeds the path-length bound")
        return resolved, label, relative_text
    raise InputError(
        "consumer file must be inside --source-root or the explicit --consumer-root"
    )


def _normal_archive_path(raw: str) -> str:
    if not raw or len(raw) > MAX_MEMBER_PATH_CHARS:
        raise InputError("archive member path is empty or exceeds the path-length bound")
    if "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise InputError("archive member path contains NUL, backslash, or an absolute path")
    if PureWindowsPath(raw).drive:
        raise InputError("archive member path contains a drive prefix")
    parts = raw.split("/")
    if any(part == ".." for part in parts):
        raise InputError("archive member path contains traversal")
    normalized_parts = [part for part in parts if part not in ("", ".")]
    if not normalized_parts:
        raise InputError("archive member path normalizes to an empty path")
    return PurePosixPath(*normalized_parts).as_posix()


def _check_path_conflicts(files: set[str], directories: set[str]) -> None:
    if files.intersection(directories):
        raise InputError("archive path is listed as both a file and a directory")
    for path in files:
        parts = path.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in files:
                raise InputError("archive file path conflicts with a parent file")


def _read_exact_bounded(stream, expected: int, total_so_far: int) -> int:
    read_count = 0
    while True:
        block = stream.read(min(65536, expected - read_count + 1))
        if not block:
            break
        read_count += len(block)
        if read_count > expected:
            raise InputError("archive member exceeded its declared size")
        if total_so_far + read_count > MAX_ARCHIVE_INSPECTED_PY_BYTES:
            raise InputError("inspected Python archive bytes exceed the bound")
    if read_count != expected:
        raise InputError("archive member size did not match its declaration")
    return read_count


def _inspect_zip(fileobj, archive_size: int) -> tuple[set[str], set[str], dict]:
    py_files: set[str] = set()
    files: set[str] = set()
    directories: set[str] = set()
    seen: set[str] = set()
    total_uncompressed = 0
    total_compressed = 0
    inspected_py = 0
    with zipfile.ZipFile(fileobj, mode="r") as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise InputError("archive member count exceeds the bound")
        for info in infos:
            raw = getattr(info, "orig_filename", info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            has_directory_spelling = info.is_dir() or raw.endswith("/")
            has_directory_mode = file_type == stat.S_IFDIR
            if has_directory_mode and not raw.endswith("/"):
                raise InputError("ZIP directory mode conflicts with a no-slash member name")
            if has_directory_spelling and file_type == stat.S_IFREG:
                raise InputError("ZIP regular-file mode conflicts with directory spelling")
            is_directory = has_directory_spelling or has_directory_mode
            normalized = _normal_archive_path(raw.rstrip("/") if is_directory else raw)
            if normalized in seen:
                raise InputError("archive contains duplicate normalized paths")
            seen.add(normalized)
            if file_type == stat.S_IFLNK:
                raise InputError("archive contains a symlink member")
            if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise InputError("archive contains a non-regular member")
            if info.flag_bits & 0x1:
                raise InputError("encrypted ZIP members are not supported")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise InputError("ZIP compression method is outside the supported bound")
            if info.file_size < 0 or info.compress_size < 0:
                raise InputError("archive member has an invalid size")
            if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise InputError("archive member exceeds the per-member size bound")
            total_uncompressed += info.file_size
            total_compressed += info.compress_size
            if total_uncompressed > MAX_ARCHIVE_TOTAL_BYTES:
                raise InputError("archive declared uncompressed bytes exceed the bound")
            if total_uncompressed > archive_size * MAX_ARCHIVE_COMPRESSION_RATIO:
                raise InputError("archive compression ratio exceeds the bound")
            if is_directory:
                if info.file_size:
                    raise InputError("ZIP directory member has content")
                directories.add(normalized)
                continue
            files.add(normalized)
            if normalized.endswith(".py"):
                with archive.open(info, "r") as member:
                    inspected_py += _read_exact_bounded(member, info.file_size, inspected_py)
                py_files.add(normalized)
    _check_path_conflicts(files, directories)
    return py_files, directories, {
        "kind": "wheel_or_zip",
        "member_count": len(seen),
        "declared_uncompressed_bytes": total_uncompressed,
        "inspected_python_bytes": inspected_py,
        "compressed_member_bytes": total_compressed,
        "layout_root_removed": None,
    }


def _decompress_gzip_tar_bounded(fileobj, archive_size: int):
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    expanded = io.BytesIO()
    expanded_count = 0
    while True:
        chunk = fileobj.read(65536)
        if not chunk:
            break
        pending = chunk
        drain = True
        while pending or drain:
            allowance = min(65536, MAX_TAR_DECOMPRESSED_BYTES - expanded_count + 1)
            decoded = decompressor.decompress(pending, max(1, allowance))
            pending = decompressor.unconsumed_tail
            if decompressor.unused_data:
                raise InputError("TAR input contains trailing or concatenated gzip data")
            expanded_count += len(decoded)
            if expanded_count > MAX_TAR_DECOMPRESSED_BYTES:
                raise InputError("expanded TAR stream exceeds the decompression bound")
            if expanded_count > archive_size * MAX_ARCHIVE_COMPRESSION_RATIO:
                raise InputError("archive compression ratio exceeds the bound")
            expanded.write(decoded)
            drain = not pending and len(decoded) == max(1, allowance)
            if decompressor.eof:
                break
        if decompressor.eof:
            if fileobj.read(1):
                raise InputError("TAR input contains trailing gzip data")
            break
    if not decompressor.eof:
        raise InputError("gzip TAR stream is incomplete")
    expanded.seek(0)
    return expanded, expanded_count


def _tar_octal_size(field: bytes) -> int:
    if field and field[0] & 0x80:
        raise InputError("base-256 TAR sizes are outside the supported bound")
    raw = field.strip(b"\x00 ")
    if not raw:
        return 0
    if any(byte < ord("0") or byte > ord("7") for byte in raw):
        raise InputError("TAR member has a malformed size field")
    return int(raw, 8)


def _preflight_tar_headers(expanded: io.BytesIO, archive_size: int) -> tuple[int, int]:
    data = expanded.getbuffer()
    offset = 0
    members = 0
    declared_total = 0
    while offset < len(data):
        if offset + 512 > len(data):
            raise InputError("TAR stream ends inside a header block")
        header = data[offset:offset + 512]
        if not any(header):
            if any(data[offset:]):
                raise InputError("TAR contains non-zero data after its end marker")
            break
        member_type = bytes(header[156:157])
        if member_type not in (b"\x00", b"0", b"5", b"x", b"g"):
            raise InputError("TAR extended or non-regular member types are rejected")
        size = _tar_octal_size(bytes(header[124:136]))
        if member_type == b"5" and size != 0:
            raise InputError("TAR directory member has content")
        if size > MAX_ARCHIVE_MEMBER_BYTES:
            raise InputError("TAR member exceeds the per-member size bound")
        members += 1
        if members > MAX_ARCHIVE_MEMBERS:
            raise InputError("archive member count exceeds the bound")
        declared_total += size
        if declared_total > MAX_ARCHIVE_TOTAL_BYTES:
            raise InputError("archive declared uncompressed bytes exceed the bound")
        if declared_total > archive_size * MAX_ARCHIVE_COMPRESSION_RATIO:
            raise InputError("archive compression ratio exceeds the bound")
        data_offset = offset + 512
        next_offset = data_offset + ((size + 511) // 512) * 512
        if next_offset > len(data):
            raise InputError("TAR member data is truncated")
        offset = next_offset
    data.release()
    expanded.seek(0)
    return members, declared_total


def _strip_common_tar_root(
    python_paths: set[str],
    directories: set[str],
    regular_files: set[str],
) -> tuple[set[str], set[str], str | None]:
    relevant = regular_files | directories
    if not relevant:
        return python_paths, directories, None
    first = sorted(relevant)[0].split("/")[0]
    if not first:
        return python_paths, directories, None
    has_child = False
    for path in relevant:
        if path == first and path in directories:
            continue
        if not path.startswith(first + "/"):
            return python_paths, directories, None
        has_child = True
    if not has_child:
        return python_paths, directories, None
    stripped_python = {path[len(first) + 1:] for path in python_paths}
    stripped_dirs = {
        path[len(first) + 1:] if path.startswith(first + "/") else path
        for path in directories
    }
    stripped_dirs.discard("")
    return stripped_python, stripped_dirs, first


def _inspect_tar_gz(fileobj, archive_size: int) -> tuple[set[str], set[str], dict]:
    expanded, expanded_bytes = _decompress_gzip_tar_bounded(fileobj, archive_size)
    preflight_count, preflight_total = _preflight_tar_headers(expanded, archive_size)
    py_files: set[str] = set()
    files: set[str] = set()
    directories: set[str] = set()
    seen: set[str] = set()
    inspected_py = 0
    total_uncompressed = 0
    member_count = 0
    with tarfile.open(fileobj=expanded, mode="r:") as archive:
        for member in archive:
            member_count += 1
            if member_count > MAX_ARCHIVE_MEMBERS:
                raise InputError("archive member count exceeds the bound")
            is_directory = member.isdir()
            normalized = _normal_archive_path(
                member.name.rstrip("/") if is_directory else member.name
            )
            if normalized in seen:
                raise InputError("archive contains duplicate normalized paths")
            seen.add(normalized)
            if is_directory:
                if member.size != 0:
                    raise InputError("TAR directory member has content")
                directories.add(normalized)
                continue
            if not member.isfile() or getattr(member, "sparse", None):
                raise InputError("TAR extended or non-regular member types are rejected")
            if member.size < 0 or member.size > MAX_ARCHIVE_MEMBER_BYTES:
                raise InputError("TAR member has an invalid or over-bound size")
            total_uncompressed += member.size
            if total_uncompressed > MAX_ARCHIVE_TOTAL_BYTES:
                raise InputError("archive declared uncompressed bytes exceed the bound")
            files.add(normalized)
            if normalized.endswith(".py"):
                member_stream = archive.extractfile(member)
                if member_stream is None:
                    raise InputError("TAR Python member could not be read")
                with member_stream:
                    inspected_py += _read_exact_bounded(
                        member_stream, member.size, inspected_py
                    )
                py_files.add(normalized)
    _check_path_conflicts(files, directories)
    py_files, directories, root = _strip_common_tar_root(
        py_files, directories, files
    )
    return py_files, directories, {
        "kind": "sdist_tar_gz",
        "member_count": member_count,
        "declared_uncompressed_bytes": total_uncompressed,
        "raw_header_count": preflight_count,
        "raw_declared_header_bytes": preflight_total,
        "decompressed_tar_bytes": expanded_bytes,
        "inspected_python_bytes": inspected_py,
        "compressed_member_bytes": archive_size,
        "layout_root_removed": root,
    }


def inspect_archive(path: Path) -> tuple[set[str], set[str], dict]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InputError("archive size could not be read") from exc
    if size > MAX_ARCHIVE_BYTES:
        raise InputError("archive input exceeds the 20 MiB bound")
    with path.open("rb") as fileobj:
        signature = fileobj.read(4)
        fileobj.seek(0)
        if signature.startswith(b"PK"):
            return _inspect_zip(fileobj, size)
        if signature.startswith(b"\x1f\x8b"):
            return _inspect_tar_gz(fileobj, size)
    raise InputError("archive content is neither a ZIP wheel nor a gzip TAR sdist")


def scan_source(root: Path) -> tuple[set[str], set[str], dict]:
    py_files: set[str] = set()
    directories: set[str] = set()
    stack = [root]
    entries_seen = 0
    total_bytes = 0
    symlinks_skipped = 0
    junctions_skipped = 0
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise InputError("source snapshot directory could not be scanned") from exc
        for entry in entries:
            entries_seen += 1
            if entries_seen > MAX_SOURCE_ENTRIES:
                raise InputError("source snapshot entry count exceeds the bound")
            rel = Path(entry.path).relative_to(root).as_posix()
            if len(rel) > MAX_MEMBER_PATH_CHARS:
                raise InputError("source snapshot path exceeds the path-length bound")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise InputError("source snapshot entry could not be inspected") from exc
            if stat.S_ISLNK(info.st_mode):
                symlinks_skipped += 1
                continue
            if _is_reparse_point(info):
                junctions_skipped += 1
                continue
            is_junction = getattr(os.path, "isjunction", None)
            if callable(is_junction) and is_junction(entry.path):
                junctions_skipped += 1
                continue
            if stat.S_ISDIR(info.st_mode):
                directories.add(rel)
                stack.append(Path(entry.path))
                continue
            if not stat.S_ISREG(info.st_mode) or not rel.endswith(".py"):
                continue
            if info.st_size > MAX_SOURCE_FILE_BYTES:
                raise InputError("source Python file exceeds the per-file size bound")
            total_bytes += info.st_size
            if total_bytes > MAX_SOURCE_TOTAL_BYTES:
                raise InputError("source Python file bytes exceed the total bound")
            py_files.add(rel)
            if len(py_files) > MAX_SOURCE_PY_FILES:
                raise InputError("source Python file count exceeds the bound")
    return py_files, directories, {
        "status": "LIMITED_LINKS_SKIPPED" if symlinks_skipped or junctions_skipped else (
            "UNKNOWN_EMPTY_PYTHON_LAYOUT" if not py_files else "BOUNDED_LAYOUT"
        ),
        "entry_count": entries_seen,
        "python_file_count": len(py_files),
        "python_bytes": total_bytes,
        "symlinks_skipped": symlinks_skipped,
        "junctions_skipped": junctions_skipped,
        "links_skipped": symlinks_skipped + junctions_skipped,
    }


def module_name(path: str) -> str:
    if not path.endswith(".py"):
        return ""
    no_suffix = path[:-3]
    if no_suffix == "__init__":
        return ""
    if no_suffix.endswith("/__init__"):
        no_suffix = no_suffix[: -len("/__init__")]
    return no_suffix.replace("/", ".")


def _is_target_name(value: str) -> bool:
    parts = value.split(".")
    return bool(parts) and all(part.isidentifier() for part in parts)


def classify_target(target: str, files: set[str], directories: set[str], uncertain: bool) -> dict:
    if uncertain:
        return {"status": "UNKNOWN", "reason": "snapshot traversal skipped a symlink or junction"}
    if not files:
        return {"status": "UNKNOWN", "reason": "no Python files establish a usable layout"}
    path = target.replace(".", "/")
    module_file = path + ".py"
    package_init = path + "/__init__.py"
    leaf_kind = None
    if module_file in files and package_init in files:
        return {"status": "UNKNOWN", "reason": "both module and package forms are present"}
    if module_file in files:
        leaf_kind = "MODULE_FILE"
    elif package_init in files:
        leaf_kind = "REGULAR_PACKAGE"
    elif path in directories or any(item.startswith(path + "/") for item in files):
        leaf_kind = "NAMESPACE_PACKAGE"
    else:
        return {"status": "ABSENT", "reason": "no matching module file or package directory in this layout"}

    segments = target.split(".")
    namespace_parent = False
    for index in range(1, len(segments)):
        parent = "/".join(segments[:index])
        if parent + "/__init__.py" in files:
            continue
        if parent in directories or any(item.startswith(parent + "/") for item in files):
            namespace_parent = True
        else:
            return {"status": "ABSENT", "reason": f"parent path {parent!r} is absent"}
    if leaf_kind == "NAMESPACE_PACKAGE" or namespace_parent:
        return {
            "status": "NAMESPACE_POSSIBLE",
            "leaf_kind": leaf_kind,
            "reason": "directory layout permits a namespace-package interpretation; runtime import is untested",
        }
    return {
        "status": leaf_kind,
        "leaf_kind": leaf_kind,
        "reason": "structural file-layout evidence only",
    }


def _expr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expr_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _dynamic_import_aliases(tree: ast.AST) -> set[str]:
    aliases = {"import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "import_module":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    pending = [handler.type]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Name) and node.id in ("ImportError", "ModuleNotFoundError"):
            return True
        if isinstance(node, ast.Attribute) and node.attr in ("ImportError", "ModuleNotFoundError"):
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _walk_ast_bounded(tree: ast.AST) -> tuple[int, int]:
    count = 0
    max_depth = 0
    pending = [(tree, 0)]
    while pending:
        node, depth = pending.pop()
        count += 1
        max_depth = max(max_depth, depth)
        if count > MAX_AST_NODES:
            raise InputError("consumer AST node count exceeds the bound")
        if depth > MAX_AST_DEPTH:
            raise InputError("consumer AST depth exceeds the bound")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return count, max_depth


def inspect_consumer(path: Path, root_label: str, relative: str, source: bytes, targets: list[str]) -> dict:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": relative,
            "root": root_label,
            "parse_status": "UNKNOWN",
            "reason": "consumer is not UTF-8 text",
            "imports": [],
            "dynamic_or_unresolved": [],
            "_target_match_counts": {},
            "_target_match_first": {},
        }
    try:
        tree = ast.parse(text, filename=relative)
        node_count, max_depth = _walk_ast_bounded(tree)
    except (SyntaxError, RecursionError, MemoryError, InputError) as exc:
        return {
            "path": relative,
            "root": root_label,
            "parse_status": "UNKNOWN",
            "reason": "consumer could not be parsed within static bounds",
            "imports": [],
            "dynamic_or_unresolved": [],
            "_target_match_counts": {},
            "_target_match_first": {},
        }

    imports: list[dict] = []
    uncertain: list[dict] = []
    dynamic_import_names = _dynamic_import_aliases(tree)

    def add_import(node: ast.AST, context: str) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                imports.append({
                    "line": node.lineno,
                    "statement": f"import {name}"[:MAX_IMPORT_LABEL_CHARS],
                    "module": name[:MAX_IMPORT_LABEL_CHARS],
                    "imported_name": None,
                    "candidates": [name],
                    "try_except": context,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if node.level:
                uncertain.append({
                    "line": node.lineno,
                    "kind": "relative_import_unresolved",
                    "try_except": context,
                })
                return
            if not module:
                uncertain.append({
                    "line": node.lineno,
                    "kind": "module_unresolved",
                    "try_except": context,
                })
                return
            for alias in node.names:
                candidates = [module]
                if alias.name != "*":
                    candidates.append(f"{module}.{alias.name}")
                imports.append({
                    "line": node.lineno,
                    "statement": f"from {module} import {alias.name}"[:MAX_IMPORT_LABEL_CHARS],
                    "module": module[:MAX_IMPORT_LABEL_CHARS],
                    "imported_name": alias.name[:MAX_IMPORT_LABEL_CHARS],
                    "candidates": [item[:MAX_TARGET_CHARS] for item in candidates],
                    "try_except": context,
                })

    def visit(node: ast.AST, context: str = "NORMAL") -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            add_import(node, context)
            return
        if isinstance(node, ast.Call):
            name = _expr_name(node.func) or ""
            if (
                name == "__import__"
                or name in dynamic_import_names
                or name.endswith(".import_module")
            ):
                uncertain.append({
                    "line": getattr(node, "lineno", 0),
                    "kind": "dynamic_import_call",
                    "try_except": context,
                })
        if isinstance(node, ast.Try):
            for child in node.body:
                visit(child, "TRY_GUARD")
            for handler in node.handlers:
                role = "EXCEPT_FALLBACK" if _handler_catches_import_error(handler) else "EXCEPT_HANDLER"
                for child in handler.body:
                    visit(child, role)
            for child in node.orelse:
                visit(child, "TRY_ELSE")
            for child in node.finalbody:
                visit(child, "TRY_FINALLY")
            return
        try_node = getattr(ast, "TryStar", None)
        if try_node is not None and isinstance(node, try_node):
            for child in node.body:
                visit(child, "TRY_GUARD")
            for handler in node.handlers:
                role = "EXCEPT_FALLBACK" if _handler_catches_import_error(handler) else "EXCEPT_HANDLER"
                for child in handler.body:
                    visit(child, role)
            for child in node.orelse:
                visit(child, "TRY_ELSE")
            for child in node.finalbody:
                visit(child, "TRY_FINALLY")
            return
        for child in ast.iter_child_nodes(node):
            visit(child, context)

    visit(tree)
    # Keep the public report bounded even when the inspected source has many imports.
    imports.sort(key=lambda item: (item["line"], item["statement"]))
    uncertain.sort(key=lambda item: (item["line"], item["kind"]))
    selected_match_counts = {target: 0 for target in targets}
    selected_match_first = {}
    for item in imports:
        for target in targets:
            if target in item["candidates"]:
                evidence = {
                    "target": target,
                    "file": relative,
                    "line": item["line"],
                    "import": item["statement"],
                    "try_except": item["try_except"],
                }
                selected_match_counts[target] += 1
                selected_match_first.setdefault(target, evidence)
    selected_matches = [
        selected_match_first[target]
        for target in targets
        if target in selected_match_first
    ]
    selected_match_total = sum(selected_match_counts.values())
    return {
        "path": relative,
        "root": root_label,
        "parse_status": "STATIC_AST_PARSED",
        "ast_nodes": node_count,
        "ast_depth": max_depth,
        "imports": imports[:MAX_OUTPUT_IMPORTS],
        "imports_omitted": max(0, len(imports) - MAX_OUTPUT_IMPORTS),
        "dynamic_or_unresolved": uncertain[:MAX_OUTPUT_IMPORTS],
        "uncertain_omitted": max(0, len(uncertain) - MAX_OUTPUT_IMPORTS),
        "selected_matches": selected_matches,
        "selected_matches_omitted": max(0, selected_match_total - len(selected_matches)),
        "_target_match_counts": selected_match_counts,
        "_target_match_first": selected_match_first,
    }


def _short_list(values: set[str]) -> dict:
    ordered = sorted(values)
    return {
        "paths": ordered[:MAX_OUTPUT_PATHS],
        "omitted": max(0, len(ordered) - MAX_OUTPUT_PATHS),
        "total": len(ordered),
    }


def _target_is_valid(target: str) -> bool:
    return (
        len(target) <= MAX_TARGET_CHARS
        and target.isascii()
        and _is_target_name(target)
    )


def build_report(args) -> dict:
    archive_path = _checked_path(args.archive, "archive")
    source_root = _checked_path(args.source_root, "source root")
    roots: list[tuple[str, Path]] = [("source_snapshot", source_root)]
    if args.consumer_root:
        consumer_root = _checked_path(args.consumer_root, "consumer root")
        if consumer_root != source_root:
            roots.append(("consumer_root", consumer_root))

    targets = args.target
    if not 1 <= len(targets) <= MAX_TARGETS:
        raise InputError("provide between one and 32 selected import targets")
    if len(set(targets)) != len(targets) or any(not _target_is_valid(item) for item in targets):
        raise InputError("targets must be unique dotted Python names of at most 200 ASCII characters")
    consumer_args = args.consumer_file
    if not 1 <= len(consumer_args) <= MAX_CONSUMERS:
        raise InputError("provide between one and 16 explicit consumer files")

    archive_files, archive_dirs, archive_info = inspect_archive(archive_path)
    source_files, source_dirs, source_info = scan_source(source_root)

    consumers = []
    seen_consumers: set[str] = set()
    consumer_total = 0
    for value in consumer_args:
        path, root_label, relative = _checked_consumer(value, roots)
        identity = os.path.normcase(os.fspath(path))
        if identity in seen_consumers:
            raise InputError("duplicate consumer file path")
        seen_consumers.add(identity)
        size = path.stat().st_size
        if size > MAX_CONSUMER_FILE_BYTES:
            raise InputError("consumer file exceeds the per-file size bound")
        consumer_total += size
        if consumer_total > MAX_CONSUMER_TOTAL_BYTES:
            raise InputError("consumer file bytes exceed the total bound")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise InputError("consumer file could not be read") from exc
        consumers.append(inspect_consumer(path, root_label, relative, data, targets))

    archive_uncertain = not archive_files
    source_uncertain = (
        source_info["links_skipped"] > 0 or not source_files
    )
    target_results = []
    for target in targets:
        target_results.append({
            "target": target,
            "archive": classify_target(target, archive_files, archive_dirs, archive_uncertain),
            "source_snapshot": classify_target(target, source_files, source_dirs, source_uncertain),
        })

    archive_set = archive_files
    source_set = source_files
    archive_only = archive_set - source_set
    source_only = source_set - archive_set
    match_counts_by_target = {target: 0 for target in targets}
    match_evidence_by_target = {target: [] for target in targets}
    unknown_by_target = {target: False for target in targets}
    for consumer in consumers:
        if consumer["parse_status"] != "STATIC_AST_PARSED":
            for target in targets:
                unknown_by_target[target] = True
            continue
        for target, count in consumer["_target_match_counts"].items():
            match_counts_by_target[target] += count
            first = consumer["_target_match_first"].get(target)
            if first and len(match_evidence_by_target[target]) < MAX_OUTPUT_MATCHES_PER_TARGET:
                match_evidence_by_target[target].append(first)
        if consumer.get("dynamic_or_unresolved"):
            for target in targets:
                unknown_by_target[target] = True
    consumer_target_results = []
    for target in targets:
        match_count = match_counts_by_target[target]
        evidence = match_evidence_by_target[target]
        if match_count:
            status = "STATIC_MATCHES_FOUND"
        elif unknown_by_target[target]:
            status = "UNKNOWN"
        else:
            status = "NO_MATCHING_STATIC_IMPORT"
        consumer_target_results.append({
            "target": target,
            "status": status,
            "evidence": evidence,
            "evidence_omitted": max(0, match_count - len(evidence)),
            "meaning": "static syntax in the explicit consumer files only; runtime behavior is untested",
        })
    for consumer in consumers:
        consumer.pop("_target_match_counts", None)
        consumer.pop("_target_match_first", None)

    overall = "LIMITED" if (
        source_uncertain
        or any(item["parse_status"] != "STATIC_AST_PARSED" or item.get("dynamic_or_unresolved") for item in consumers)
    ) else "BOUNDED_STATIC"
    return {
        "tool": "Yumi Python Import Layout Evidence",
        "status": overall,
        "scope": "bounded archive/source file-layout comparison and static AST inspection of explicit consumer files",
        "subject_code_executed": False,
        "archive": {
            **archive_info,
            "python_file_count": len(archive_files),
            "python_files": _short_list(archive_files),
            "module_names": _short_list({name for path in archive_files if (name := module_name(path))}),
        },
        "source_snapshot": {
            **source_info,
            "python_files": _short_list(source_files),
            "module_names": _short_list({name for path in source_files if (name := module_name(path))}),
        },
        "layout_comparison": {
            "content_compared": False,
            "same_python_file_layout": archive_set == source_set,
            "archive_only": _short_list(archive_only),
            "source_only": _short_list(source_only),
            "meaning": "path-only comparison; matching version labels do not establish byte identity or intent",
        },
        "selected_targets": target_results,
        "consumers": consumers,
        "consumer_target_evidence": consumer_target_results,
        "limitations": [
            "No subject code is installed, imported, executed, extracted, fetched, or persisted.",
            "No runtime import, installability, dependency resolution, behavior, vulnerability, authenticity, or scientific reproducibility is established.",
            "Only the supplied archive, bounded snapshot root, selected targets, and explicit consumer files are inspected.",
        ],
        "limits": {
            "archive_bytes": MAX_ARCHIVE_BYTES,
            "archive_members": MAX_ARCHIVE_MEMBERS,
            "archive_member_uncompressed_bytes": MAX_ARCHIVE_MEMBER_BYTES,
            "archive_total_uncompressed_bytes": MAX_ARCHIVE_TOTAL_BYTES,
            "tar_decompressed_bytes": MAX_TAR_DECOMPRESSED_BYTES,
            "archive_inspected_python_bytes": MAX_ARCHIVE_INSPECTED_PY_BYTES,
            "archive_compression_ratio": MAX_ARCHIVE_COMPRESSION_RATIO,
            "source_entries": MAX_SOURCE_ENTRIES,
            "source_python_files": MAX_SOURCE_PY_FILES,
            "source_python_file_bytes": MAX_SOURCE_FILE_BYTES,
            "source_python_total_bytes": MAX_SOURCE_TOTAL_BYTES,
            "consumer_files": MAX_CONSUMERS,
            "consumer_file_bytes": MAX_CONSUMER_FILE_BYTES,
            "consumer_total_bytes": MAX_CONSUMER_TOTAL_BYTES,
            "ast_nodes_per_file": MAX_AST_NODES,
            "ast_depth": MAX_AST_DEPTH,
            "selected_targets": MAX_TARGETS,
            "selected_target_characters": MAX_TARGET_CHARS,
            "match_evidence_per_target": MAX_OUTPUT_MATCHES_PER_TARGET,
            "output_bytes": MAX_OUTPUT_BYTES,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare one local wheel/sdist layout with one local source snapshot and inspect explicit consumer files."
    )
    parser.add_argument("--archive", required=True, metavar="PATH")
    parser.add_argument("--source-root", required=True, metavar="DIR")
    parser.add_argument(
        "--consumer-root",
        metavar="DIR",
        help="optional second allowed root; only explicitly named --consumer-file paths are read",
    )
    parser.add_argument(
        "--consumer-file",
        action="append",
        required=True,
        metavar="PATH",
        help="literal .py path inside --source-root or --consumer-root; repeat for each file",
    )
    parser.add_argument(
        "--target",
        action="append",
        required=True,
        metavar="DOTTED_NAME",
        help="selected import/module name; repeat for each target",
    )
    args = parser.parse_args(argv)
    try:
        report = build_report(args)
        encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
        if len(encoded) > MAX_OUTPUT_BYTES:
            report = {
                "tool": "Yumi Python Import Layout Evidence",
                "status": "LIMITED",
                "error": "bounded detail report exceeded the output ceiling",
                "archive_python_files": len(report["archive"]["python_files"]["paths"]),
                "source_python_files": len(report["source_snapshot"]["python_files"]["paths"]),
                "limits": {"output_bytes": MAX_OUTPUT_BYTES},
            }
            encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise InputError("minimal JSON report exceeded the output bound")
        sys.stdout.buffer.write(encoded)
        return 0
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, ValueError) as exc:
        print(f"error: input could not be safely inspected ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
