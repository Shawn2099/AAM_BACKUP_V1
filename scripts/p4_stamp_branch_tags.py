"""P4-SID one-shot: stamp BRANCH_TAG constants into scenario files."""
from pathlib import Path

TAGS = {
    "test_scen_branch_a.py": "A", "test_scen_branch_b.py": "B",
    "test_scen_branch_c.py": "C", "test_scen_branch_d.py": "D",
    "test_scen_branch_e.py": "E", "test_scen_branch_f.py": "F",
    "test_scen_branch_g.py": "G", "test_scen_branch_h.py": "H",
    "test_scen_branch_i.py": "I", "test_scen_branch_j.py": "J",
    "test_scen_branch_k.py": "K",
}

for fname, tag in TAGS.items():
    p = Path(__file__).resolve().parent.parent / "tests" / fname
    if not p.exists():
        print("MISSING", fname)
        continue
    src = p.read_text(encoding="utf-8")
    if "BRANCH_TAG" in src:
        print("skip", fname)
        continue
    lines = src.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines[:60]):
        if line.startswith(("import ", "from ")) or not line.strip() or line.startswith("#"):
            insert_at = i + 1
        else:
            break
    lines.insert(insert_at, f'BRANCH_TAG = "{tag}"  # P4-SID: ledger rows read {tag}/<sid>\n')
    p.write_text("".join(lines), encoding="utf-8", newline="")
    print("tagged", fname, tag)
