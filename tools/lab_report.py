"""Turn the audit lab's junit.xml into a short REPORT.md: totals, every failure with its first error line.

usage: python tools/lab_report.py <junit.xml> <REPORT.md> <pytest exit code>
"""
import sys
import xml.etree.ElementTree as ET
from datetime import datetime


def main(junit: str, out: str, code: str) -> None:
    suites = ET.parse(junit).getroot()
    suites = [suites] if suites.tag == "testsuite" else list(suites)
    total = failed = errors = skipped = 0
    seconds = 0.0
    bad = []
    for suite in suites:
        for case in suite.iter("testcase"):
            total += 1
            seconds += float(case.get("time") or 0)
            name = f"{case.get('classname')}::{case.get('name')}"
            for tag in ("failure", "error"):
                node = case.find(tag)
                if node is not None:
                    failed += tag == "failure"
                    errors += tag == "error"
                    first = (node.get("message") or node.text or "").strip().splitlines()
                    bad.append((name, tag, first[0][:300] if first else ""))
            if case.find("skipped") is not None:
                skipped += 1
    lines = [
        f"# Audit lab report - {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"- Result: **{'ALL GREEN' if not (failed or errors) and code == '0' else 'NEEDS ATTENTION'}** (pytest exit code {code})",
        f"- {total} tests: {total - failed - errors - skipped} passed, {failed} failed, {errors} errors, {skipped} skipped",
        f"- Test time: {seconds / 60:.0f} minutes",
        "- Known intentional red: none expected (W16 was fixed by plan WP8). Anything below is new.",
        "",
    ]
    if bad:
        lines.append("## Failures")
        for name, tag, msg in bad:
            lines.append(f"- `{name}` ({tag}): {msg}")
    else:
        lines.append("No failures.")
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:4])
