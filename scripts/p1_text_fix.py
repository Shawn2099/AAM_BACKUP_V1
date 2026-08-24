"""P1-TEXT fixer v2 - replace em/en-dashes with ASCII '-' ONLY inside
machine-facing string literals (logger args / raise messages). Spans come
from the SAME context filter the CI scanner uses, so docstrings, comments
and HTML typography are untouched.

Usage: .venv\\Scripts\\python.exe scripts\\p1_text_fix.py   (idempotent)
"""
import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_p1_text_hygiene import _iter_target_files  # noqa: E402

BAD = {"\u2014": "-", "\u2013": "-"}


def _flagged_constants(tree: ast.AST):
    """Constant str nodes in logger.* args or raise messages (scanner logic)."""
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "logger"):
                targets = node.args
        elif isinstance(node, ast.Raise):
            targets = list(ast.walk(node))
        for t in targets:
            if isinstance(t, ast.Constant) and isinstance(t.value, str):
                yield t
            elif isinstance(t, ast.JoinedStr):
                for v in t.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        yield v


def fix_file(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    spans = []
    for const in _flagged_constants(tree):
        if any(ch in const.value for ch in BAD):
            spans.append((const.lineno, const.col_offset,
                          const.end_lineno, const.end_col_offset))
    if not spans:
        return 0

    lines = source.splitlines(keepends=True)

    def abs_pos(lineno, col):  # 1-based lineno -> absolute index
        return sum(len(l) for l in lines[:lineno - 1]) + col

    out = source
    for start, end in sorted(
        [(abs_pos(a, b), abs_pos(c, d)) for a, b, c, d in spans], reverse=True
    ):
        seg = out[start:end]
        for ch, rep in BAD.items():
            seg = seg.replace(ch, rep)
        out = out[:start] + seg + out[end:]

    path.write_text(out, encoding="utf-8", newline="")
    print(f"FIXED {path.relative_to(PROJECT_ROOT)}: {len(spans)} literal span(s)")
    return len(spans)


def main() -> int:
    total = sum(fix_file(p) for p in _iter_target_files())
    print(f"TOTAL_SPANS_FIXED={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
