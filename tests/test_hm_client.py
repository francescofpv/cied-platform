"""Client API Home Monitoring contro il server finto (HTTPS con certificato client reale).

I test senza database girano sempre; quelli di sincronizzazione usano il DB
temporaneo di conftest.py.
"""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from conftest import PSEUDO_KEY
from test_loader import make_xml

from cied.hm_client import HMClient, HMError, RateLimiter, format_ts
from cied.hm_mock import MockExport, MockHMServer, make_test_pki
from cied.sync import sync_jobs, sync_on_demand

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def pki(tmp_path_factory):
    return make_test_pki(tmp_path_factory.mktemp("pki"))


@pytest.fixture
def make_server(pki):
    servers = []

    def _make(**kw):
        s = MockHMServer(pki, **kw).start()
        servers.append(s)
        return s
    yield _make
    for s in servers:
        s.stop()


@pytest.fixture
def server(make_server):
    return make_server(max_per_min=None)


def client_for(srv, pki, *, secret="mock-secret", sleeps=None, **kw):
    return HMClient(srv.base_url, "mock-client", secret, pki["client_cert"], pki["client_key"],
                    ca_bundle=pki["ca"], max_per_min=kw.pop("max_per_min", 100),
                    sleep=(sleeps.append if sleeps is not None else (lambda s: None)), **kw)


# --- utilità -------------------------------------------------------------
def test_format_ts_is_utc():
    assert format_ts(datetime(2015, 4, 16, 18, 51, 19)) == "2015-04-16T18:51:19Z"
    assert format_ts(datetime(2026, 10, 23, 2, 20, 1, tzinfo=timezone(timedelta(hours=2)))) == "2026-10-23T00:20:01Z"


def test_rate_limiter_waits_after_max_calls():
    t, slept = [0.0], []

    def sleep(s):
        slept.append(s)
        t[0] += s
    rl = RateLimiter(4, 60, clock=lambda: t[0], sleep=sleep)
    for _ in range(4):
        assert rl.acquire() == 0
    t[0] = 10
    assert rl.acquire() == 50          # la quinta chiamata aspetta che la prima esca dalla finestra
    assert slept == [50]


def test_client_checks_certificate_files(tmp_path):
    with pytest.raises(FileNotFoundError, match="HM_CERT_PEM"):
        HMClient("https://x/rest/api", "id", "secret", tmp_path / "manca.pem", tmp_path / "manca.key")


# --- endpoint ------------------------------------------------------------
def test_list_get_delete(server, pki):
    a = server.add_export(make_xml(1, serial="S-1"))
    server.add_export(make_xml(2, serial="S-2"))
    c = client_for(server, pki)

    jobs = c.list_exports()
    assert {j.id for j in jobs} == set(server.exports)
    assert all(j.created_at.tzinfo is not None for j in jobs)

    xml = c.get_export(a.id)
    assert xml[:2] == b"\xff\xfe"                       # UTF-16 con BOM, come l'API reale
    assert "S-1" in xml.decode("utf-16")

    c.delete_export(a.id)
    assert [j.id for j in c.list_exports()] != [] and a.id not in {j.id for j in c.list_exports()}
    with pytest.raises(HMError) as e:
        c.get_export(a.id)                              # processato: non più disponibile
    assert e.value.status == 404 and e.value.codes == {"4040"}


def test_wrong_secret_is_401_4011(server, pki):
    with pytest.raises(HMError) as e:
        client_for(server, pki, secret="sbagliato").list_exports()
    assert e.value.status == 401 and e.value.codes == {"4011"}
    assert "Client ID o Client secret errati" in str(e.value)


def test_no_client_certificate_is_rejected(server, pki):
    s = requests.Session()
    s.auth = ("mock-client", "mock-secret")
    with pytest.raises((requests.exceptions.SSLError, requests.exceptions.ConnectionError)):
        s.get(server.base_url + "/exports", verify=str(pki["ca"]), timeout=10)


def test_retry_after_429(server, pki):
    server.forced.append(4290)
    sleeps = []
    c = client_for(server, pki, sleeps=sleeps)
    assert c.list_exports() == []
    assert sleeps == [60.0] and c.requests_made == 2    # ha aspettato il Retry-After e riprovato


def test_gives_up_after_retries(server, pki):
    server.forced.extend([5030] * 4)
    c = client_for(server, pki, retries=3)
    with pytest.raises(HMError) as e:
        c.list_exports()
    assert e.value.status == 503 and c.requests_made == 4


def test_mock_enforces_rate_limit(make_server, pki):
    srv = make_server(max_per_min=2)
    c = client_for(srv, pki, retries=0)
    c.list_exports()
    c.list_exports()
    with pytest.raises(HMError) as e:
        c.list_exports()
    assert e.value.status == 429 and e.value.codes == {"4290"}


def test_latest_export_on_demand(server, pki):
    server.add_export(make_xml(1, serial="S-9"))        # sessione 2026-10-23 00:20 UTC
    c = client_for(server, pki)
    assert c.latest_export("S-9", datetime(2026, 10, 1, tzinfo=timezone.utc)) is not None
    assert c.latest_export("S-9", datetime(2026, 11, 1, tzinfo=timezone.utc)) is None     # 204
    with pytest.raises(HMError) as e:
        c.latest_export("NON-ESISTE", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert e.value.status == 400 and e.value.codes == {"4000"}


def test_convert_p12(pki, tmp_path, server):
    out = tmp_path / "secrets"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "convert_p12.py"), str(pki["client_p12"]),
                        "--out", str(out), "--password", "test"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    c = HMClient(server.base_url, "mock-client", "mock-secret", out / "cert.pem", out / "private.key",
                 ca_bundle=pki["ca"])
    assert c.list_exports() == []                       # i PEM convertiti funzionano per l'mTLS
    bad = subprocess.run([sys.executable, str(ROOT / "scripts" / "convert_p12.py"), str(pki["client_p12"]),
                          "--out", str(out), "--password", "errata"], capture_output=True, text=True)
    assert bad.returncode != 0 and "Password errata" in bad.stderr


# --- sincronizzazione con il database --------------------------------------
def test_sync_jobs_imports_and_acknowledges(conn, server, pki):
    server.add_export(make_xml(100, serial="HM-0001", patient="paz-hm1"))
    server.add_export(make_xml(101, serial="HM-0002", patient="paz-hm2"))
    c = client_for(server, pki)

    res = sync_jobs(c, conn, PSEUDO_KEY, log=lambda s: None)
    assert (res.seen, res.loaded, res.acknowledged, res.errors) == (2, 2, 2, [])
    assert c.list_exports() == []                       # tutto segnato come processato
    ids = {i for (i,) in conn.execute(
        "SELECT vendor_export_id FROM cied.export WHERE vendor_export_id IS NOT NULL").fetchall()}
    assert set(server.exports) <= ids

    again = sync_jobs(c, conn, PSEUDO_KEY, log=lambda s: None)
    assert again.seen == 0


def test_sync_jobs_duplicate_is_acknowledged(conn, server, pki):
    server.add_export(make_xml(100, serial="HM-0001", patient="paz-hm1"))   # già importato sopra
    res = sync_jobs(client_for(server, pki), conn, PSEUDO_KEY, log=lambda s: None)
    assert (res.loaded, res.duplicates, res.acknowledged) == (0, 1, 1)


def test_failed_import_stays_in_queue(conn, server, pki):
    bad = MockExport(id="rotto", patient=None, serial=None, report_dtm=datetime.now(timezone.utc),
                     created_at=datetime.now(timezone.utc), data="<non-xml".encode("utf-16"))
    server.exports[bad.id] = bad
    c = client_for(server, pki)
    res = sync_jobs(c, conn, PSEUDO_KEY, log=lambda s: None)
    assert res.acknowledged == 0 and len(res.errors) == 1
    assert [j.id for j in c.list_exports()] == ["rotto"]


def test_sync_on_demand(conn, server, pki):
    # HM-0001 è nel DB (test precedente) con sessione 2026-10-23
    server.add_export(make_xml(100, serial="HM-0001", patient="paz-hm1"))
    c = client_for(server, pki)
    res = sync_on_demand(c, conn, PSEUDO_KEY, ["HM-0001"], log=lambda s: None)
    assert (res.up_to_date, res.loaded) == (1, 0)

    newer = (make_xml(102, serial="HM-0001", patient="paz-hm1").decode("utf-16")
             .replace("20261023T022001+0200", "20261110T080000+0100")).encode("utf-16")
    server.add_export(newer)
    res = sync_on_demand(c, conn, PSEUDO_KEY, ["HM-0001"], log=lambda s: None)
    assert (res.loaded, res.errors) == (1, [])
    assert sync_on_demand(c, conn, PSEUDO_KEY, ["HM-0001"], log=lambda s: None).up_to_date == 1
