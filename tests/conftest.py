import os
import tempfile
from pathlib import Path
import pytest

# Import-time initialisation is isolated too.
os.environ["ONEJOURNEY_DB"] = str(
    Path(tempfile.mkdtemp(prefix="oj-tests-")) / "boot.sqlite3"
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    from app import main, access

    monkeypatch.setattr(main, "DB", tmp_path / "case.sqlite3")
    monkeypatch.setenv("ONEJOURNEY_DEMO_MODE", "1")
    monkeypatch.setenv("ONEJOURNEY_PARTNER_KEY", "test-partner")
    monkeypatch.setenv("ONEJOURNEY_OPS_KEY", "test-ops")
    access._buckets.clear()
    main.init()
