#!/usr/bin/env python3
"""Build final evidence bundle from profile summaries and independent review artifacts.

S7.1 fix (2026-07-24): tighten review() so evidence cannot be silently PASS.

Hard rules:
  - Missing file          → BLOCKED (artifact recorded, no default PASS).
  - Empty file (size==0)  → BLOCKED.
  - File present but no
    VERDICT line parseable → BLOCKED.
  - VERDICT present but
    outside ALLOWED set    → BLOCKED.
  - PASS_WITH_FIXES is
    mapped to FAIL         (no soft "almost passed" state leaks to PASS).
  - Overall verdict is
    PASS only when every
    profile.status ∈
    {PASS, NOT_APPLICABLE}
    AND every threeway
    verdict == 'PASS'.
  - Any threeway verdict
    == BLOCKED forces
    overall BLOCKED, not
    FAIL (so a missing
    review is distinguishable
    from a real negative).
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

ALLOWED = {"PASS", "FAIL", "BLOCKED", "NOT_APPLICABLE"}
# Match either Chinese 总体裁决/最终 VERDICT or English VERDICT.
# Tolerant of markdown headers (#), emphasis (**), parens after VERDICT.
# Note: use [ \t]* not \s* at line start so the ^ anchor stays on the
# correct line and hit_line computation is accurate.
_VERDICT_RE = re.compile(
    r'(?im)^[ \t]*(?:#+[ \t]*)?(?:总体裁决[ \t]*[:：][ \t]*\*{0,2}'
    r'|VERDICT(?:\s*\([^\n)]*\))?[ \t]*[:：][ \t]*\*{0,2}'
    r'|(?:最终[ \t]*)?VERDICT[ \t]*[:：][ \t]*\*{0,2})'
    r'(PASS_WITH_FIXES|PASS|FAIL|BLOCKED|NOT_APPLICABLE)'
)


def review(path: str) -> dict:
    """Parse a single threeway review artifact.

    Returns a dict with keys:
      - artifact:    the path string passed in
      - exists:      bool
      - size_bytes:  int (0 if missing)
      - parsed:      bool (did we find at least one VERDICT line)
      - hit_line:    int 1-based line number of the first VERDICT hit (0 if none)
      - raw:         the raw token from the first hit (uppercased) or ""
      - verdict:     one of ALLOWED, or BLOCKED on any failure
    """
    p = Path(path)
    artifact = str(p)
    if not p.exists():
        return {
            "artifact": artifact, "exists": False, "size_bytes": 0,
            "parsed": False, "hit_line": 0, "raw": "", "verdict": "BLOCKED",
        }
    size = p.stat().st_size
    if size == 0:
        return {
            "artifact": artifact, "exists": True, "size_bytes": 0,
            "parsed": False, "hit_line": 0, "raw": "", "verdict": "BLOCKED",
        }
    text = p.read_text(errors="replace")
    m = _VERDICT_RE.search(text)
    if not m:
        return {
            "artifact": artifact, "exists": True, "size_bytes": size,
            "parsed": False, "hit_line": 0, "raw": "", "verdict": "BLOCKED",
        }
    hit_line = text.count("\n", 0, m.start()) + 1
    raw = m.group(1).upper()
    # PASS_WITH_FIXES never collapses to PASS.
    if raw == "PASS_WITH_FIXES":
        verdict = "FAIL"
    elif raw in ALLOWED:
        verdict = raw
    else:
        verdict = "BLOCKED"
    return {
        "artifact": artifact, "exists": True, "size_bytes": size,
        "parsed": True, "hit_line": hit_line, "raw": raw, "verdict": verdict,
    }


def overall_verdict(profile_results: list, threeway: dict) -> tuple[str, str]:
    """Compute the overall verdict.

    Returns (verdict, reason) where reason is a short human-readable explanation.

    Precedence:
      1. Any threeway BLOCKED          → BLOCKED ("missing/empty/unparseable review")
      2. Any profile FAIL              → FAIL    ("profile command failed")
      3. Any threeway FAIL             → FAIL    ("review returned negative")
      4. All profile ∈ {PASS,NA} and
         all threeway == PASS         → PASS
      5. otherwise                     → BLOCKED
    """
    profile_statuses = {r["status"] for r in profile_results}
    threeway_verdicts = {k: v["verdict"] for k, v in threeway.items()}

    if "BLOCKED" in threeway_verdicts.values():
        bad = [k for k, v in threeway_verdicts.items() if v == "BLOCKED"]
        return "BLOCKED", f"threeway BLOCKED from: {bad}"
    if "FAIL" in profile_statuses:
        bad_cmds = [r.get("command") for r in profile_results if r["status"] == "FAIL"]
        return "FAIL", f"profile FAIL: {bad_cmds}"
    if "FAIL" in threeway_verdicts.values():
        bad = [k for k, v in threeway_verdicts.items() if v == "FAIL"]
        return "FAIL", f"threeway FAIL from: {bad}"
    profiles_ok = profile_statuses <= {"PASS", "NOT_APPLICABLE"}
    reviews_ok = set(threeway_verdicts.values()) == {"PASS"}
    if profiles_ok and reviews_ok:
        return "PASS", "all profiles ∈ {PASS,NOT_APPLICABLE} and all threeway PASS"
    return "BLOCKED", f"unresolved: profiles={profile_statuses} threeway={threeway_verdicts}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task-id", required=True)
    p.add_argument("--changed-path", action="append", default=[])
    p.add_argument("--profile", action="append", required=True)
    p.add_argument("--cc", required=True)
    p.add_argument("--codex", required=True)
    p.add_argument("--hermes", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--commit", default=None)
    a = p.parse_args()

    vals: list = []
    for f in a.profile:
        x = json.loads(Path(f).read_text())
        vals.extend(x if isinstance(x, list) else [x])

    threeway = {k: review(v) for k, v in [
        ("cc", a.cc), ("codex", a.codex), ("hermes", a.hermes),
    ]}

    verdict, reason = overall_verdict(vals, threeway)
    bundle = {
        "schema_version": 1,
        "task_id": a.task_id,
        "commit": a.commit,
        "changed_paths": a.changed_path,
        "verifications": vals,
        "threeway": threeway,
        "verdict": verdict,
        "verdict_reason": reason,
        "builder": "build_evidence.py S7.1 (strict-verdict)",
    }
    Path(a.output).write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
    print(verdict, a.output, "|", reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())