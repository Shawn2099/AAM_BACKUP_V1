"""Map coverage misses to enclosing functions and rank the gaps."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
cov_path = REPO / "audit_lab" / "raw" / (sys.argv[1] if len(sys.argv) > 1 else "cov_v1.json")
data = json.loads(cov_path.read_text(encoding="utf-8"))

FUNC_EXEMPT = "__init__", "__repr__", "__str__", "__eq__", "main", "<module>"
rows = []
for path, info in sorted(data["files"].items()):
    summary = info["summary"]
    src = (REPO / path).read_text(encoding="utf-8", errors="replace").splitlines()

    import ast
    tree = ast.parse("\n".join(src))
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            spans.append((node.lineno, node.end_lineno, node.name))
    spans.sort()

    def owner(ln):
        best = "<module>"
        for start, end, name in spans:
            if start <= ln <= end:
                if best == "<module>" or start > max(
                    (s for s, e, n in spans if n == best), default=0
                ):
                    best = name
        return best

    missed = list(info["missing_lines"])
    partial = info.get("missing_branches", [])
    if not missed:
        continue
    by_fn: dict[str, list] = {}
    for ln in missed:
        by_fn.setdefault(owner(ln), []).append(ln)
    for fn, lines in sorted(by_fn.items(), key=lambda kv: -len(kv[1])):
        src_span = [l for l in lines]
        rows.append({
            "file": path, "cover%": round(summary["percent_covered"], 1),
            "function": fn, "missed": len(lines),
            "sample": ", ".join(str(l) for l in src_span[:8]),
        })

rows.sort(key=lambda r: (r["cover%"], -r["missed"]))
print(f"{'file':<28}{'cov%':>6}  {'function':<38}{'missed':>6}  sample lines")
print("-" * 110)
for r in rows[:45]:
    print(f"{r['file']:<28}{r['cover%']:>6}  {r['function']:<38}{r['missed']:>6}  {r['sample']}")
