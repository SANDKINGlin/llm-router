#!/usr/bin/env python3
"""S7.2 E2E schema validation tests.

Run with the project venv:
    .venv/bin/python tests/unit/test_schema_e2e.py

The test:
  T1  upgraded schema is valid Draft 2020-12 JSON Schema
  T2  S7.1.2 build_evidence output passes schema validation (E2E pipeline)
  T3  S7.1.2 build_evidence with empty profile list passes schema validation
  T4  a bundle missing verdict_reason is rejected
  T5  a bundle missing builder is rejected
  T6  a threeway review missing hit_line is rejected
  T7  a threeway review with parsed=false but verdict=PASS is rejected
      (consistency check from $defs/review allOf)
  T8  a threeway review with exists=false but size_bytes!=0 is rejected
  T9  a verification with status=PASS but exit_code!=0 is rejected
  T10 schema $id and title metadata present
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent.parent / ".harness"
sys.path.insert(0, str(HARNESS))

import build_evidence  # type: ignore  # noqa: E402

try:
    from jsonschema import Draft202012Validator
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class SchemaMetaTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((HARNESS / "evidence.schema.json").read_text())

    def test_T1_schema_is_valid_draft_2020_12(self):
        # Will raise SchemaError if invalid.
        Draft202012Validator.check_schema(self.schema)

    def test_T10_schema_metadata(self):
        self.assertEqual(self.schema["$schema"],
                         "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("$id", self.schema)


@unittest.skipUnless(HAS_JSONSCHEMA, "jsonschema not installed")
class BuildEvidenceSchemaE2ETests(unittest.TestCase):
    """Drive build_evidence.main() and validate the resulting bundle."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((HARNESS / "evidence.schema.json").read_text())
        cls.validator = Draft202012Validator(cls.schema)

    def _write_review(self, text: str) -> str:
        fd, p = tempfile.mkstemp(prefix="s72-review-", suffix=".md")
        os.close(fd)
        Path(p).write_text(text)
        self.addCleanup(lambda: os.unlink(p) if os.path.exists(p) else None)
        return p

    def _write_profile(self, status: str, exit_code: int = 0) -> str:
        fd, p = tempfile.mkstemp(prefix="s72-prof-", suffix=".json")
        os.close(fd)
        Path(p).write_text(json.dumps({
            "profile": "fast", "command": "echo", "exit_code": exit_code,
            "duration_ms": 1, "status": status, "artifact": None,
        }))
        self.addCleanup(lambda: os.unlink(p) if os.path.exists(p) else None)
        return p

    def _drive_build(self, profile_path: str, cc_text: str, codex_text: str,
                     hermes_text: str, expect_rc: int = 0) -> dict:
        out_fd, out = tempfile.mkstemp(prefix="s72-bundle-", suffix=".json")
        os.close(out_fd)
        self.addCleanup(lambda: os.unlink(out) if os.path.exists(out) else None)
        cc = self._write_review(cc_text)
        codex = self._write_review(codex_text)
        hermes = self._write_review(hermes_text)
        argv = [
            "build_evidence.py", "--task-id", "s72-e2e",
            "--changed-path", ".harness/build_evidence.py",
            "--profile", profile_path, "--cc", cc, "--codex", codex,
            "--hermes", hermes, "--output", out, "--commit", "s72test",
        ]
        saved = sys.argv
        try:
            sys.argv = argv
            rc = build_evidence.main()
        finally:
            sys.argv = saved
        self.assertEqual(rc, expect_rc,
                         f"build_evidence.main() returned {rc}, expected {expect_rc}")
        return json.loads(Path(out).read_text())

    def test_T2_happy_path_validates(self):
        bundle = self._drive_build(
            self._write_profile("PASS"),
            "VERDICT: PASS\n", "VERDICT: PASS\n", "VERDICT: PASS\n",
        )
        errors = list(self.validator.iter_errors(bundle))
        self.assertEqual(errors, [], f"schema errors: {[e.message for e in errors]}")
        self.assertEqual(bundle["verdict"], "PASS")
        self.assertIn("verdict_reason", bundle)
        self.assertIn("builder", bundle)
        # Threeway review must have all 5 new fields.
        for k in ("cc", "codex", "hermes"):
            r = bundle["threeway"][k]
            for f in ("exists", "size_bytes", "parsed", "hit_line", "raw"):
                self.assertIn(f, r, f"threeway.{k} missing {f}")

    def test_T3_empty_profile_validates(self):
        # S7.1.2 path: empty profile list → BLOCKED with rc=2.
        empty_fd, empty_p = tempfile.mkstemp(prefix="s72-empty-", suffix=".json")
        os.close(empty_fd)
        Path(empty_p).write_text("[]")
        self.addCleanup(lambda: os.unlink(empty_p) if os.path.exists(empty_p) else None)
        out_fd, out = tempfile.mkstemp(prefix="s72-bundle-", suffix=".json")
        os.close(out_fd)
        self.addCleanup(lambda: os.unlink(out) if os.path.exists(out) else None)
        cc = self._write_review("VERDICT: PASS\n")
        codex = self._write_review("VERDICT: PASS\n")
        hermes = self._write_review("VERDICT: PASS\n")
        argv = [
            "build_evidence.py", "--task-id", "s72-empty",
            "--profile", empty_p, "--cc", cc, "--codex", codex,
            "--hermes", hermes, "--output", out, "--commit", "s72test",
        ]
        saved = sys.argv
        try:
            sys.argv = argv
            rc = build_evidence.main()
        finally:
            sys.argv = saved
        self.assertEqual(rc, 2)
        bundle = json.loads(Path(out).read_text())
        errors = list(self.validator.iter_errors(bundle))
        self.assertEqual(errors, [], f"empty-profile bundle should validate: {errors}")
        self.assertEqual(bundle["verdict"], "BLOCKED")
        # S7.2 schema-compatible placeholder: one BLOCKED verification
        # (so verifications: minItems 1 is satisfied) and three BLOCKED
        # reviews (so threeway required keys are satisfied).
        self.assertEqual(len(bundle["verifications"]), 1)
        self.assertEqual(bundle["verifications"][0]["status"], "BLOCKED")
        self.assertEqual(set(bundle["threeway"].keys()), {"cc", "codex", "hermes"})
        self.assertTrue(all(v["verdict"] == "BLOCKED"
                            for v in bundle["threeway"].values()))

    def test_T4_missing_verdict_reason_rejected(self):
        bundle = self._drive_build(
            self._write_profile("PASS"),
            "VERDICT: PASS\n", "VERDICT: PASS\n", "VERDICT: PASS\n",
        )
        del bundle["verdict_reason"]
        errors = list(self.validator.iter_errors(bundle))
        self.assertTrue(errors, "expected schema to reject bundle without verdict_reason")
        self.assertTrue(any("verdict_reason" in e.message for e in errors),
                        f"error should mention verdict_reason: {[e.message for e in errors]}")

    def test_T5_missing_builder_rejected(self):
        bundle = self._drive_build(
            self._write_profile("PASS"),
            "VERDICT: PASS\n", "VERDICT: PASS\n", "VERDICT: PASS\n",
        )
        del bundle["builder"]
        errors = list(self.validator.iter_errors(bundle))
        self.assertTrue(errors)
        self.assertTrue(any("builder" in e.message for e in errors))

    def test_T6_review_missing_hit_line_rejected(self):
        bundle = self._drive_build(
            self._write_profile("PASS"),
            "VERDICT: PASS\n", "VERDICT: PASS\n", "VERDICT: PASS\n",
        )
        del bundle["threeway"]["cc"]["hit_line"]
        errors = list(self.validator.iter_errors(bundle))
        self.assertTrue(errors)
        self.assertTrue(any("hit_line" in e.message for e in errors))

    def test_T7_review_parsed_false_but_verdict_pass_rejected(self):
        bundle = self._drive_build(
            self._write_profile("PASS"),
            "VERDICT: PASS\n", "VERDICT: PASS\n", "VERDICT: PASS\n",
        )
        # Force inconsistency: parsed=False but verdict=PASS.
        bundle["threeway"]["cc"]["parsed"] = False
        bundle["threeway"]["cc"]["hit_line"] = 0
        bundle["threeway"]["cc"]["raw"] = ""
        bundle["threeway"]["cc"]["verdict"] = "PASS"
        errors = list(self.validator.iter_errors(bundle))
        self.assertTrue(errors, "allOf consistency check should reject parsed=False + verdict=PASS")
        self.assertTrue(any("must be 'BLOCKED'" in e.message or "BLOCKED" in e.message
                            for e in errors),
                        f"error should mention BLOCKED: {[e.message for e in errors]}")

    def test_T8_review_exists_false_but_size_nonzero_rejected(self):
        bundle = self._drive_build(
            self._write_profile("PASS"),
            "VERDICT: PASS\n", "VERDICT: PASS\n", "VERDICT: PASS\n",
        )
        # Force inconsistency: exists=False but size_bytes=100.
        bundle["threeway"]["cc"]["exists"] = False
        bundle["threeway"]["cc"]["size_bytes"] = 100
        bundle["threeway"]["cc"]["verdict"] = "BLOCKED"
        errors = list(self.validator.iter_errors(bundle))
        self.assertTrue(errors, "allOf consistency check should reject exists=False + size>0")
        self.assertTrue(any("size_bytes" in e.message or "0" in e.message
                            for e in errors),
                        f"error should mention size_bytes: {[e.message for e in errors]}")

    def test_T9_verification_status_pass_but_exit_code_nonzero_rejected(self):
        bundle = self._drive_build(
            self._write_profile("PASS"),
            "VERDICT: PASS\n", "VERDICT: PASS\n", "VERDICT: PASS\n",
        )
        # Force inconsistency: status=PASS but exit_code=1.
        bundle["verifications"][0]["status"] = "PASS"
        bundle["verifications"][0]["exit_code"] = 1
        errors = list(self.validator.iter_errors(bundle))
        self.assertTrue(errors, "allOf consistency check should reject status=PASS + exit_code!=0")
        # Schema uses const: 0 in the then branch, so the message is about
        # the expected value being 0, not the literal field name. Verify
        # the path/absolute path instead.
        offending_paths = [list(e.absolute_path) for e in errors]
        self.assertTrue(
            any(path[:2] == ["verifications", 0] for path in offending_paths),
            f"error should point at verifications[0]: {offending_paths}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)