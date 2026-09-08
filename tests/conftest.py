# tests/conftest.py
import os

import pytest

os.environ["PREFECT_TEST_MODE"] = "1"


@pytest.fixture
def tmp_db(tmp_path):
    """Fresh ManifestDB in a temp directory."""
    from core.manifest import ManifestDB

    db = ManifestDB(str(tmp_path / "test_manifest.db"))
    yield db
    db.close()


@pytest.fixture
def populated_db(tmp_db):
    """Database pre-populated with files synced to both cloud and lan."""
    tmp_db.bulk_upsert_synced(
        [{"path": "docs/client_report.pdf", "size": 2048, "mtime": 1700000000.0}],
        mode="lan",
    )
    tmp_db.bulk_upsert_synced(
        [{"path": "docs/client_report.pdf", "size": 2048, "mtime": 1700000000.0}],
        mode="cloud",
    )
    return tmp_db
