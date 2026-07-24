#!/usr/bin/env python3
"""S7.1 unit tests for build_evidence.py — strict verdict enforcement.

Run with the project venv:
    .venv/bin/python tests/unit/test_build_evidence_strict.py

Tests (no mocks; uses tmp files only):
  T1  missing file             → BLOCKED
  T2  zero-byte file            → BLOCKED
  T3  non-empty but no VERDICT  → BLOCKED
  T4  VERDICT outside enum      → BLOCKED
  T5  VERDICT = PASS            → PASS
  T6  VERDICT = FAIL            → FAIL
  T7  VERDICT = NOT_APPLICABLE  → NOT_APPLICABLE
  T8  VERDICT = BLOCKED         → BLOCKED
  T9  PASS_WITH_FIXES           → FAIL (never collapses to PASS)
  T10 VERDICT in markdown header (# VERDICT: PASS) → PASS, hit_line reported
  T11 overall: any threeway BLOCKED → overall BLOCKED
  T12 overall: profile FAIL → overall FAIL
  T13 overall: all PASS + profiles PASS → overall PASS
  T14 overall: profile NOT_APPLICABLE + threeway PASS → overall PASS
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make build_evidence importable.
HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent.parent / ".harness"
sys.path.insert(0, str(HARNESS))

import build_evidence  # type: ignore  # noqa: E402


class ReviewTests(unittest.TestCase):

    def _write(self, text: str) -> str:
        fd, p = tempfile.mkstemp(prefix="s71-review-", suffix=".md")
        os.close(fd)
        Path(p).write_text(text)
        self.addCleanup(lambda: os.unlink(p) if os.path.exists(p) else None)
        return p

    def test_T1_missing_file(self):
        r = build_evidence.review("/nonexistent/path/to/review.md")
        self.assertFalse(r["exists"])
        self.assertEqual(r["verdict"], "BLOCKED")

    def test_T2_zero_byte(self):
        p = self._write("")
        r = build_evidence.review(p)
        self.assertTrue(r["exists"])
        self.assertEqual(r["size_bytes"], 0)
        self.assertEqual(r["verdict"], "BLOCKED")

    def test_T3_no_verdict_line(self):
        p = self._write("# Title\n\nSome prose but no VERDICT line.\n")
        r = build_evidence.review(p)
        self.assertTrue(r["parsed"] is False)
        self.assertEqual(r["verdict"], "BLOCKED")

    def test_T4_verdict_outside_enum(self):
        p = self._write("VERDICT: MAYBE\n")
        r = build_evidence.review(p)
        # Tighter than spec: tokens outside the enum are treated as if
        # no VERDICT line existed (BLOCKED). parsed=False is acceptable.
        self.assertEqual(r["verdict"], "BLOCKED")

    def test_T5_verdict_pass(self):
        p = self._write("VERDICT: PASS\n")
        r = build_evidence.review(p)
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["hit_line"], 1)

    def test_T6_verdict_fail(self):
        p = self._write("VERDICT: FAIL\n")
        self.assertEqual(build_evidence.review(p)["verdict"], "FAIL")

    def test_T7_verdict_not_applicable(self):
        p = self._write("VERDICT: NOT_APPLICABLE\n")
        self.assertEqual(build_evidence.review(p)["verdict"], "NOT_APPLICABLE")

    def test_T8_verdict_blocked(self):
        p = self._write("VERDICT: BLOCKED\n")
        self.assertEqual(build_evidence.review(p)["verdict"], "BLOCKED")

    def test_T9_pass_with_fix_never_passes(self):
        p = self._write("VERDICT: PASS_WITH_FIXES\n")
        r = build_evidence.review(p)
        self.assertEqual(r["verdict"], "FAIL")

    def test_T10_markdown_header(self):
        body = "# Review\n\n## VERDICT: PASS\n"
        p = self._write(body)
        r = build_evidence.review(p)
        self.assertEqual(r["verdict"], "PASS")
        # hit_line is 1-based and points at the line containing the VERDICT.
        self.assertGreater(r["hit_line"], 0)
        self.assertIn("VERDICT: PASS", Path(p).read_text().splitlines()[r["hit_line"] - 1])

    def test_T11_chinese_overall_marker(self):
        p = self._write("总体裁决: PASS\n")
        self.assertEqual(build_evidence.review(p)["verdict"], "PASS")

    # ── S7.1.1 adversarial tests (R1/R2 from Hermes review) ──

    def test_T12_token_right_boundary_blocks_PASSENGER(self):
        p = self._write("VERDICT: PASSENGER\n")
        r = build_evidence.review(p)
        # PASSENGER must NOT be parsed as PASS — right-boundary anchor.
        self.assertNotEqual(r["verdict"], "PASS")

    def test_T13_token_right_boundary_blocks_PASS_NOT_REALLY(self):
        p = self._write("VERDICT: PASS_NOT_REALLY\n")
        r = build_evidence.review(p)
        self.assertNotEqual(r["verdict"], "PASS")

    def test_T14_last_verdict_authoritative(self):
        # Two VERDICT lines: example first, real second. The real one wins.
        p = self._write("VERDICT: PASS\n\n# Final\n\nVERDICT: FAIL\n")
        r = build_evidence.review(p)
        self.assertEqual(r["verdict"], "FAIL")
        self.assertEqual(r["raw"], "FAIL")

    def test_T15_token_right_boundary_allows_normal_PASS(self):
        # End-of-line is a valid boundary — normal PASS still works.
        p = self._write("VERDICT: PASS\n")
        r = build_evidence.review(p)
        self.assertEqual(r["verdict"], "PASS")


class OverallVerdictTests(unittest.TestCase):

    def _rv(self, verdict: str) -> dict:
        return {
            "artifact": "x", "exists": True, "size_bytes": 100,
            "parsed": True, "hit_line": 1, "raw": verdict, "verdict": verdict,
        }

    def test_any_threeway_blocked_forces_blocked(self):
        threeway = {"cc": self._rv("PASS"), "codex": self._rv("BLOCKED"), "hermes": self._rv("PASS")}
        profiles = [{"status": "PASS"}]
        v, reason = build_evidence.overall_verdict(profiles, threeway)
        self.assertEqual(v, "BLOCKED")
        self.assertIn("threeway BLOCKED", reason)

    def test_profile_fail_forces_fail(self):
        threeway = {"cc": self._rv("PASS"), "codex": self._rv("PASS"), "hermes": self._rv("PASS")}
        profiles = [{"status": "FAIL"}]
        v, _ = build_evidence.overall_verdict(profiles, threeway)
        self.assertEqual(v, "FAIL")

    def test_threeway_fail_forces_fail(self):
        threeway = {"cc": self._rv("PASS"), "codex": self._rv("FAIL"), "hermes": self._rv("PASS")}
        profiles = [{"status": "PASS"}]
        v, _ = build_evidence.overall_verdict(profiles, threeway)
        self.assertEqual(v, "FAIL")

    def test_all_pass(self):
        threeway = {"cc": self._rv("PASS"), "codex": self._rv("PASS"), "hermes": self._rv("PASS")}
        profiles = [{"status": "PASS"}]
        v, reason = build_evidence.overall_verdict(profiles, threeway)
        self.assertEqual(v, "PASS")

    def test_profile_na_with_passes(self):
        threeway = {"cc": self._rv("PASS"), "codex": self._rv("PASS"), "hermes": self._rv("PASS")}
        profiles = [{"status": "NOT_APPLICABLE"}]
        v, _ = build_evidence.overall_verdict(profiles, threeway)
        self.assertEqual(v, "PASS")


class EndToEndBundleTests(unittest.TestCase):

    def _write(self, text: str) -> str:
        fd, p = tempfile.mkstemp(prefix="s71-e2e-", suffix=".md")
        os.close(fd)
        Path(p).write_text(text)
        self.addCleanup(lambda: os.unlink(p) if os.path.exists(p) else None)
        return p

    def _profile(self, status: str) -> str:
        fd, p = tempfile.mkstemp(prefix="s71-profile-", suffix=".json")
        os.close(fd)
        Path(p).write_text(json.dumps({
            "profile": "fast", "command": "echo", "exit_code": 0,
            "duration_ms": 1, "status": status, "artifact": None,
        }))
        self.addCleanup(lambda: os.unlink(p) if os.path.exists(p) else None)
        return p

    def test_e2e_pass(self):
        out_fd, out = tempfile.mkstemp(prefix="s71-bundle-", suffix=".json")
        os.close(out_fd)
        self.addCleanup(lambda: os.unlink(out) if os.path.exists(out) else None)
        argv = [
            "build_evidence.py",
            "--task-id", "s71-test",
            "--changed-path", ".harness/build_evidence.py",
            "--profile", self._profile("PASS"),
            "--cc", self._write("VERDICT: PASS\n"),
            "--codex", self._write("VERDICT: PASS\n"),
            "--hermes", self._write("VERDICT: PASS\n"),
            "--output", out,
        ]
        saved = sys.argv
        try:
            sys.argv = argv
            rc = build_evidence.main()
        finally:
            sys.argv = saved
        self.assertEqual(rc, 0)
        b = json.loads(Path(out).read_text())
        self.assertEqual(b["verdict"], "PASS")
        self.assertEqual(b["threeway"]["cc"]["verdict"], "PASS")
        self.assertEqual(b["threeway"]["codex"]["verdict"], "PASS")
        self.assertEqual(b["threeway"]["hermes"]["verdict"], "PASS")

    def test_e2e_blocked_when_one_review_empty(self):
        out_fd, out = tempfile.mkstemp(prefix="s71-bundle-", suffix=".json")
        os.close(out_fd)
        self.addCleanup(lambda: os.unlink(out) if os.path.exists(out) else None)
        # Empty file → BLOCKED.
        empty = self._write("")
        argv = [
            "build_evidence.py",
            "--task-id", "s71-test",
            "--profile", self._profile("PASS"),
            "--cc", self._write("VERDICT: PASS\n"),
            "--codex", empty,
            "--hermes", self._write("VERDICT: PASS\n"),
            "--output", out,
        ]
        saved = sys.argv
        try:
            sys.argv = argv
            rc = build_evidence.main()
        finally:
            sys.argv = saved
        self.assertEqual(rc, 0)
        b = json.loads(Path(out).read_text())
        self.assertEqual(b["verdict"], "BLOCKED")
        self.assertEqual(b["threeway"]["codex"]["verdict"], "BLOCKED")
        self.assertIn("BLOCKED", b["verdict_reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)