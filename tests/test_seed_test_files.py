"""tools/seed_test_files.py: random sub-2 MB files in many formats for checking a backup."""
import csv
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import seed_test_files as sf  # noqa: E402


def _files(dest):
    return [p for p in (dest / sf.SEED_DIR).rglob("*") if p.is_file() and p.name != sf.MANIFEST]


def test_creates_mixed_formats_all_under_2mb_inside_the_seed_folder(tmp_path):
    sf.seed(tmp_path, count=60, max_kb=5000, seed_value=1)      # asks for 5 MB: capped
    files = _files(tmp_path)
    assert len(files) == 60
    assert all(512 <= p.stat().st_size < 2 * 1024 * 1024 for p in files)
    assert len({p.suffix for p in files}) >= 8
    assert len({p.stat().st_size // 50_000 for p in files}) > 3          # varied sizes
    assert all(p.is_relative_to(tmp_path / sf.SEED_DIR) for p in files)


def test_files_are_valid_for_their_format(tmp_path):
    sf.seed(tmp_path, count=120, max_kb=300, seed_value=2)
    by_ext = {}
    for p in _files(tmp_path):
        by_ext.setdefault(p.suffix, p)
    assert by_ext[".png"].read_bytes().startswith(b"\x89PNG")
    assert by_ext[".pdf"].read_bytes().startswith(b"%PDF-")
    assert by_ext[".bmp"].read_bytes().startswith(b"BM")
    for ext in (".zip", ".docx", ".xlsx"):
        assert zipfile.is_zipfile(by_ext[ext])


def test_same_seed_gives_the_same_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    sf.seed(a, count=15, max_kb=100, seed_value=7)
    sf.seed(b, count=15, max_kb=100, seed_value=7)
    assert {p.relative_to(a): p.read_bytes() for p in _files(a)} == {p.relative_to(b): p.read_bytes() for p in _files(b)}


def test_modify_and_delete_touch_only_seeded_files(tmp_path):
    real = tmp_path / "client.docx"
    real.write_bytes(b"real client file")
    sf.seed(tmp_path, count=10, max_kb=50, seed_value=3)
    before = {p: p.read_bytes() for p in _files(tmp_path)}
    r = sf.seed(tmp_path, count=2, max_kb=50, seed_value=4, modify=3, delete=2)
    assert (r["added"], r["modified"], r["deleted"]) == (2, 3, 2)
    after = _files(tmp_path)
    assert len(after) == 10 - 2 + 2
    assert sum(1 for p in after if p in before and p.read_bytes() != before[p]) == 3
    assert real.read_bytes() == b"real client file"
    with (tmp_path / sf.SEED_DIR / sf.MANIFEST).open(newline="") as f:
        assert len(list(csv.DictReader(f))) == len(after)


def test_cli_refuses_a_missing_folder(tmp_path, capsys):
    try:
        sf.main(["--dest", str(tmp_path / "nope")])
    except SystemExit as e:
        assert e.code == 2
    assert "not an existing folder" in capsys.readouterr().err
