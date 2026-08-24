"""P2-CONC one-shot: wrap _run_cloud_pipeline/_run_lan_pipeline bodies in
`with _backup_slot(config):`. Idempotence NOT guaranteed - run once."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "flow.py"
src = p.read_text(encoding="utf-8")
lines = src.splitlines(keepends=True)


def is_top(s: str) -> bool:
    return bool(s) and not s[0].isspace()


out = []
i, n = 0, len(lines)
wrapped = 0
while i < n:
    line = lines[i]
    out.append(line)
    if line.startswith("def _run_cloud_pipeline") or line.startswith("def _run_lan_pipeline"):
        assert lines[i + 1].lstrip().startswith('"""'), lines[i + 1]
        k = i + 1
        if lines[k].count('"""') >= 2:
            k += 1
        else:
            k += 1
            while '"""' not in lines[k]:
                k += 1
            k += 1
        out.extend(lines[i + 1:k])
        out.append("    with _backup_slot(config):\n")
        m = k
        while m < n and not is_top(lines[m]):
            out.append(("    " + lines[m]) if lines[m].strip() else lines[m])
            m += 1
        wrapped += 1
        i = m
        continue
    i += 1

assert wrapped == 2, f"expected to wrap exactly 2 pipelines, got {wrapped}"
p.write_text("".join(out), encoding="utf-8", newline="")
print(f"WRAPPED={wrapped}")
