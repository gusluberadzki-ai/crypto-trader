from pathlib import Path

from fastapi.testclient import TestClient

from papertrader.api import create_app
from papertrader.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_password_login(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    cfg = load_config(ROOT / "config.yaml", {"data": {"source": "sim"},
                                             "storage": {"db_path": str(tmp_path / "a.db"), "model_dir": str(tmp_path / "m")}})
    c = TestClient(create_app(cfg, start_engine=False))
    assert c.get("/api/events").status_code == 401
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/login", data={"password": "nope"}, follow_redirects=False).status_code == 401
    r = c.post("/login", data={"password": "hunter2"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/api/events").status_code == 200
