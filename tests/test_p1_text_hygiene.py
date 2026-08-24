"""P1-TEXT - CI-greppable scanner: no em/en-dashes in machine-facing strings.

Scope (plan v1.1): string literals passed to logger.* calls or used in
raised exceptions across core/*.py plus flow.py/serve.py/launch.py/
watchdog.py. Docstrings and UI/e-mail HTML typography stay untouched.
Rationale: PowerShell findstr/grep pipelines and legacy log shippers mangle
U+2014/U+2013 (four live sightings in this campaign's findings).
"""
import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_TARGETS = [PROJECT_ROOT / "core"] + [
    PROJECT_ROOT / name for name in
    ("flow.py", "serve.py", "launch.py", "watchdog.py")
]
BAD_CODEPOINTS = {"\u2014": "em-dash", "\u2013": "en-dash"}


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "logger"
    )


def _machine_facing_strings(tree: ast.AST):
    """Yield (lineno, text) for literals in logger.* args or raise statements."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_logger_call(node):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    yield arg.lineno, arg.value
                elif isinstance(arg, ast.JoinedStr):
                    for value in arg.values:
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            yield value.lineno, value.value
        elif isinstance(node, ast.Raise):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    yield sub.lineno, sub.value
                elif isinstance(sub, ast.JoinedStr):
                    for value in sub.values:
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            yield value.lineno, value.value


def _iter_target_files():
    for target in SCAN_TARGETS:
        if target.is_dir():
            yield from sorted(target.glob("*.py"))
        else:
            yield target


def test_no_em_or_en_dash_in_machine_facing_strings():
    violations = []
    for path in _iter_target_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(PROJECT_ROOT)
        for lineno, text in _machine_facing_strings(tree):
            for ch, label in BAD_CODEPOINTS.items():
                if ch in text:
                    violations.append(f"{rel}:{lineno} {label}: {text[:90]!r}")
    assert not violations, (
        "Machine-facing strings must be ASCII-dash only:\n" + "\n".join(violations)
    )
