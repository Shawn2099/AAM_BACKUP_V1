# tests/test_fix_7_1_1_unc_resolve.py
import ast
from unittest.mock import MagicMock, patch

from core.lan_manifest import walk_lan_destination


def test_no_resolve_calls_in_lan_manifest():
    with open("core/lan_manifest.py") as f:
        tree = ast.parse(f.read())
    found = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "resolve"]
    assert not found, f"Found .resolve() call(s) at lines {found}"


def test_walk_lan_destination_unc_paths():
    unc_path = r"\\192.168.10.20\backups\share\\"
    mock_walk_results = [
        (r"\\192.168.10.20\backups\share", ["dept"], ["root.txt"]),
        (r"\\192.168.10.20\backups\share\dept", [], ["data.xlsx"]),
    ]
    with patch("os.walk", return_value=mock_walk_results), patch("os.stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=1024, st_mtime=1700000000.0)
        results = walk_lan_destination(unc_path)

    paths = {r["path"] for r in results}
    assert paths == {"root.txt", "dept/data.xlsx"}
    assert not any(p.startswith("\\") or p.startswith("/") for p in paths)
