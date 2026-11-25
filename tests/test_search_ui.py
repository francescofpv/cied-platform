"""API della pagina di ricerca (scripts/search_ui.py) sul database di test."""
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from psycopg.conninfo import make_conninfo
from test_patients import export

from cied import db as cied_db
from cied.config import settings

spec = importlib.util.spec_from_file_location(
    "search_ui", Path(__file__).resolve().parents[1] / "scripts" / "search_ui.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


@pytest.fixture
def base(conn, monkeypatch):
    dbname = conn.info.dbname
    monkeypatch.setattr(ui, "connect", lambda: cied_db.connect(make_conninfo(settings.db_conninfo, dbname=dbname)))
    srv = ui.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read())


def post(url, body, origin=None):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    if origin:
        req.add_header("Origin", origin)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_page_and_search(base, conn):
    export(conn, patient="ui-1", family="Interfaccia", model="BIOMONITOR III", serial="UI-1")
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        assert "Applica filtro" in r.read().decode()
    _, opts = get(base + "/api/options")
    assert any(m["code"] == "BIO" for m in opts["manufacturers"])
    _, d = get(base + "/api/search?q=interfaccia&mfg=BIO&model=BIOMONITOR%20III")
    assert d["total"] == 1 and d["rows"][0]["name_family"] == "Interfaccia"


def test_group_actions(base, conn):
    r = export(conn, patient="ui-2", family="Gruppoui", serial="UI-2")
    assert "1 pazienti aggiunti" in post(base + "/api/groups",
                                         {"action": "add", "group": "Gruppo UI", "patients": [r["patient_no"]]})["message"]
    _, opts = get(base + "/api/options")
    gpk = next(g["group_pk"] for g in opts["groups"] if g["name"] == "Gruppo UI")
    _, d = get(base + f"/api/search?group={gpk}")
    assert [x["patient_no"] for x in d["rows"]] == [r["patient_no"]]
    assert "1 pazienti rimossi" in post(base + "/api/groups",
                                        {"action": "remove", "group": gpk, "patients": [r["patient_no"]]})["message"]


def test_rejects_foreign_requests(base):
    req = urllib.request.Request(base + "/api/options", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/groups", {"action": "add", "group": "x", "patients": []}, origin="https://evil.example")
    assert e.value.code == 403


def test_bad_filter_is_400(base):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/api/search?nao=FORSE")
    assert e.value.code == 400
