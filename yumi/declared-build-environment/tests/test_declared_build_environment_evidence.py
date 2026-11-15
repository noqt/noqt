"""Focused tests for the conservative declaration evidence CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


CANDIDATE = Path(__file__).resolve().parents[1]
TASK_ROOT = CANDIDATE.parent
RUNTIME = Path(os.environ.get("YUMI_EVIDENCE_RUNTIME", TASK_ROOT / "runtime"))
sys.path.insert(0, str(CANDIDATE))

import declared_build_environment_evidence as evidence  # noqa: E402


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="evidence-test-", dir=RUNTIME)
        self.base = Path(self.temp.name)
        self.fixture_number = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fixture(
        self,
        *,
        pyproject: str | None = None,
        declaration: str = "setuptools>=68\nwheel\n",
        declaration_path: str = "reqs/build.pip",
        declaration_lines: list[int] | None = None,
        entrypoint: str = "pip install . --no-build-isolation\n",
        complete: bool = True,
    ) -> tuple[Path, Path]:
        self.fixture_number += 1
        root = self.base / f"repo-{self.fixture_number}"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            pyproject
            or '[build-system]\nrequires = ["setuptools>=68", "wheel"]\nbuild-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )
        (root / "entry.sh").write_text(entrypoint, encoding="utf-8")
        declaration_file = root / declaration_path
        declaration_file.parent.mkdir(parents=True, exist_ok=True)
        declaration_file.write_text(declaration, encoding="utf-8")
        manifest = {
            "schema": 1,
            "routes": [
                {
                    "name": "test-route",
                    "isolation": "non-isolated",
                    "entrypoints": ["entry.sh"],
                    "declarations": [
                        {"path": declaration_path, "lines": declaration_lines}
                        if declaration_lines is not None
                        else declaration_path
                    ],
                    "declarations_complete": complete,
                }
            ],
        }
        manifest_path = root / "routes.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return root, manifest_path

    def test_complete_negative_fixture_is_declared(self) -> None:
        root, manifest = self.fixture()
        result = evidence.analyze(root, manifest)
        self.assertEqual(result["status"], evidence.STATUS_DECLARED)
        route = result["routes"][0]
        self.assertEqual(route["status"], evidence.STATUS_DECLARED)
        self.assertEqual([item["status"] for item in route["requirements"]], ["DECLARED", "DECLARED"])

    def test_conditional_opaque_and_unsupported_inputs_are_unknown(self) -> None:
        cases = [
            {
                "name": "conditional requirement",
                "declaration": "setuptools>=68; python_version < '3.12'\nwheel\n",
            },
            {"name": "opaque include", "declaration": "-r other-build-requirements.txt\nwheel\n"},
            {"name": "unsupported format", "declaration_path": "build.lock", "declaration": "setuptools\n"},
            {
                "name": "dynamic build requirement",
                "pyproject": (
                    '[build-system]\nrequires = ["setuptools>=68", '
                    '"dep @ file://${PROJECT_ROOT}/dep"]\nbuild-backend = "setuptools.build_meta"\n'
                ),
            },
        ]
        for case in cases:
            with self.subTest(case=case["name"]):
                kwargs = {key: value for key, value in case.items() if key != "name"}
                root, manifest = self.fixture(**kwargs)
                result = evidence.analyze(root, manifest)
                self.assertEqual(result["routes"][0]["status"], evidence.STATUS_UNKNOWN)
                self.assertNotIn(
                    evidence.STATUS_GAP,
                    {item["status"] for item in result["routes"][0]["requirements"]},
                )

    def test_incomplete_manifest_cannot_assert_a_gap(self) -> None:
        root, manifest = self.fixture(declaration="wheel\n", complete=False)
        result = evidence.analyze(root, manifest)
        self.assertEqual(result["routes"][0]["status"], evidence.STATUS_UNKNOWN)
        setuptools = result["routes"][0]["requirements"][0]
        self.assertEqual(setuptools["status"], evidence.STATUS_UNKNOWN)

    def test_missing_literal_requirement_is_a_declaration_gap(self) -> None:
        root, manifest = self.fixture(declaration="wheel\n")
        result = evidence.analyze(root, manifest)
        self.assertEqual(result["routes"][0]["status"], evidence.STATUS_GAP)
        self.assertEqual(result["routes"][0]["requirements"][0]["status"], evidence.STATUS_GAP)

    def test_route_digest_is_deterministic_and_binds_source_bytes(self) -> None:
        root, manifest = self.fixture()
        first = evidence.analyze(root, manifest)
        second = evidence.analyze(root, manifest)
        self.assertEqual(first["binding_sha256"], second["binding_sha256"])
        self.assertEqual(first["routes"][0]["digest"], second["routes"][0]["digest"])
        (root / "reqs/build.pip").write_text("wheel\n", encoding="utf-8")
        changed = evidence.analyze(root, manifest)
        self.assertNotEqual(first["binding_sha256"], changed["binding_sha256"])
        self.assertNotEqual(first["routes"][0]["digest"], changed["routes"][0]["digest"])

    def test_nonisolated_mode_without_literal_flag_is_unknown(self) -> None:
        root, manifest = self.fixture(entrypoint="pip install .\n")
        result = evidence.analyze(root, manifest)
        self.assertEqual(result["routes"][0]["status"], evidence.STATUS_UNKNOWN)

    def test_line_selection_is_explicit_and_excludes_unrelated_commands(self) -> None:
        markdown = (
            "```bash\n"
            "uv pip install wheel\n"
            "```\n\n"
            "```bash\n"
            "uv pip install -r test-requirements.txt\n"
            "```\n"
        )
        root, manifest = self.fixture(
            pyproject=(
                '[build-system]\nrequires = ["setuptools-scm>=8", "wheel"]\n'
                'build-backend = "setuptools.build_meta"\n'
            ),
            declaration=markdown,
            declaration_path="BUILDING.md",
            declaration_lines=[1, 3],
        )
        selected = evidence.analyze(root, manifest)
        self.assertEqual(selected["routes"][0]["status"], evidence.STATUS_GAP)

        root, manifest = self.fixture(
            pyproject=(
                '[build-system]\nrequires = ["setuptools-scm>=8", "wheel"]\n'
                'build-backend = "setuptools.build_meta"\n'
            ),
            declaration=markdown,
            declaration_path="BUILDING.md",
        )
        whole_file = evidence.analyze(root, manifest)
        self.assertEqual(whole_file["routes"][0]["status"], evidence.STATUS_UNKNOWN)

    def test_path_traversal_is_rejected(self) -> None:
        root, manifest = self.fixture()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["routes"][0]["declarations"] = ["../outside.pip"]
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(evidence.InputError):
            evidence.analyze(root, manifest)

    def test_cli_emits_json_and_concise_stderr_evidence(self) -> None:
        root, manifest = self.fixture()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = evidence.main(["--root", str(root), "--routes", str(manifest)])
        self.assertEqual(exit_code, 0)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], evidence.STATUS_DECLARED)
        self.assertIn("Maintainer evidence:", stderr.getvalue())
        self.assertIn("binding sha256:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
