"""Codice paziente, identità del dispositivo, NAO, gruppi e ricerca."""
import itertools

import psycopg
import pytest
from conftest import PSEUDO_KEY
from test_loader import make_xml

from cied import patients as P
from cied.clinical import register_device, register_implant, register_patient
from cied.db.loader import load_export

_n = itertools.count(500)


def export(conn, *, patient, family, given="Mario", dob="19500101", model="BIOMONITOR III",
           serial="S-1", session="20261023T022001+0200"):
    xml = (make_xml(next(_n), serial=serial, patient=patient).decode("utf-16")
           .replace("TEST DEVICE", model).replace("Bianchi", family).replace("Anna", given)
           .replace("19580312T000000", dob + "T000000").replace("20261023T022001+0200", session))
    return load_export(conn, xml.encode("utf-16"), PSEUDO_KEY)


def nos(rows):
    return {r["patient_no"] for r in rows}


# --- identità ------------------------------------------------------------
def test_patient_no_is_progressive(conn):
    a = export(conn, patient="prog-a", family="Progressivo", serial="PRG-1")
    b = export(conn, patient="prog-b", family="Progressivo", serial="PRG-2")
    assert a["patient_no"] >= 100001 and b["patient_no"] > a["patient_no"]


def test_device_is_manufacturer_model_serial(conn):
    """Stesso seriale su due modelli diversi = due dispositivi diversi."""
    a = export(conn, patient="ser-a", family="Seriale", model="BIOMONITOR III", serial="DUP-123")
    b = export(conn, patient="ser-b", family="Seriale", model="BIOMONITOR IV", serial="DUP-123")
    assert a["status"] == b["status"] == "loaded"
    assert a["device_pk"] != b["device_pk"] and a["patient_no"] != b["patient_no"]
    (n,) = conn.execute("SELECT count(*) FROM vault.device_identity WHERE serial_number = 'DUP-123'").fetchone()
    assert n == 2


def test_changed_service_center_id_keeps_same_patient(conn):
    first = export(conn, patient="ROSSI 1950 FA", family="Cambiaid", serial="CID-1")
    # nel Service Center l'ID viene modificato (aggiunta una patologia): nuovo export dello stesso dispositivo
    second = export(conn, patient="ROSSI 1950 FA CMI", family="Cambiaid", serial="CID-1",
                    session="20261110T080000+0100")
    assert second["status"] == "loaded" and second["patient_no"] == first["patient_no"]
    ids = {v for (v,) in conn.execute("SELECT value FROM vault.patient_identifier WHERE patient_no = %s",
                                      (first["patient_no"],)).fetchall()}
    assert ids == {"ROSSI 1950 FA", "ROSSI 1950 FA CMI"}
    # da qui in poi entrambi gli ID portano allo stesso paziente
    third = export(conn, patient="ROSSI 1950 FA", family="Cambiaid", serial="CID-1",
                   session="20261120T080000+0100")
    assert third["patient_no"] == first["patient_no"]


def test_pseudonym_is_not_derived_from_external_id(conn):
    r = export(conn, patient="pseudo-x", family="Pseudo", serial="PSD-1")
    pseudo = conn.execute("SELECT patient_pseudo FROM vault.patient WHERE patient_no = %s",
                          (r["patient_no"],)).fetchone()[0]
    from cied.pseudo import patient_pseudonym
    assert pseudo == patient_pseudonym(PSEUDO_KEY, r["patient_no"])


def test_old_database_is_detected(conn):
    from psycopg.conninfo import make_conninfo

    from cied.config import settings
    from cied.db import connect, init_schema
    name = "cied_test_old_schema"
    admin = connect()
    admin.execute(f"DROP DATABASE IF EXISTS {name}")
    admin.execute(f"CREATE DATABASE {name}")
    try:
        with connect(make_conninfo(settings.db_conninfo, dbname=name)) as c:
            c.execute("CREATE SCHEMA vault; CREATE TABLE vault.patient (patient_pseudo text PRIMARY KEY)")
            with pytest.raises(RuntimeError, match="--reset"):
                init_schema(c)
            init_schema(c, reset=True)          # con il reset si ricrea correttamente
    finally:
        admin.execute(f"DROP DATABASE {name}")
        admin.close()


# --- NAO -----------------------------------------------------------------
def test_nao_is_mandatory_at_implant(conn):
    r = export(conn, patient="nao-1", family="Naotest", serial="NAO-1")
    with pytest.raises(ValueError, match="terapia NAO"):
        register_implant(conn, r["device_pk"], ["I10"], ["R55"])
    with pytest.raises(psycopg.errors.NotNullViolation):    # anche il database lo impone
        conn.execute("INSERT INTO cied.implant (patient_pk, device_pk, implant_date) VALUES (%s, %s, '2024-01-01')",
                     (r["patient_pk"], r["device_pk"]))

    impl = register_implant(conn, r["device_pk"], ["I10"], ["R55"], nao_therapy=True)
    assert conn.execute("SELECT nao_at_implant, nao_current FROM cied.v_implant WHERE implant_pk = %s",
                        (impl,)).fetchone() == (True, True)

    P.set_nao_therapy(conn, r["patient_no"], False)       # cambia lo stato attuale, non quello all'impianto
    assert conn.execute("SELECT nao_at_implant, nao_current FROM cied.v_implant WHERE implant_pk = %s",
                        (impl,)).fetchone() == (True, False)
    nao = {x["patient_no"]: x["nao_therapy"] for x in P.search(conn, "naotest")[0]}
    assert nao == {r["patient_no"]: False}


# --- ricerca ---------------------------------------------------------------
@pytest.fixture(scope="module")
def people(conn):
    p = {
        "damico": export(conn, patient="s-1", family="D'Amico", given="Lucia", dob="19560330",
                         model="BIOMONITOR III", serial="NUMERODISERIE123"),
        "deluca": export(conn, patient="s-2", family="De Luca", given="Giùseppe", dob="19601212",
                         model="BIOMONITOR IV", serial="12345678"),
        "rossi1": export(conn, patient="s-3", family="Rossi", given="Mario", dob="19470101",
                         model="BIOMONITOR III", serial="R-111"),
        "rossi2": export(conn, patient="s-4", family="Rossi", given="Anna", dob="19520505",
                         model="BIOMONITOR IV", serial="R-222"),
    }
    out = {k: v["patient_no"] for k, v in p.items()}
    # paziente con dispositivo di un altro produttore inserito a mano
    pk = register_patient(conn, PSEUDO_KEY, "Rossini", "Carlo")
    register_device(conn, PSEUDO_KEY, pk, "MDT", "MDT-999", "Azure XT", "PM")
    out["rossini"] = P.patient_no_of(conn, pk)
    return out


@pytest.mark.parametrize("query, expected", [
    ("rossi", {"rossi1", "rossi2", "rossini"}),          # inizio di parola: trova anche Rossini
    ("rossi mario", {"rossi1"}),
    ("d'amico", {"damico"}), ("damico", {"damico"}), ("DAMÌCO", {"damico"}), ("amico", {"damico"}),
    ("de luca", {"deluca"}), ("deluca", {"deluca"}), ("giuseppe", {"deluca"}),
    ("Biotronik III", {"damico", "rossi1"}),              # III non trova i IV
    ("biomonitor iv", {"deluca", "rossi2"}),
    ("biomonitoriv", {"deluca", "rossi2"}),
    ("BIOMONITOR III NUMERODISERIE123", {"damico"}),
    ("numerodis", {"damico"}),                            # seriale parziale
    ("iv 95056", {"deluca"}),
    ("rossi 1952", {"rossi2"}),                           # cognome + anno di nascita
    ("azure", {"rossini"}),
])
def test_text_search(conn, people, query, expected):
    rows, total = P.search(conn, query)
    found = nos(rows) & set(people.values())
    assert found == {people[k] for k in expected}


def test_search_by_patient_code(conn, people):
    rows, _ = P.search(conn, str(people["deluca"]))
    assert people["deluca"] in nos(rows)


def test_filters_manufacturer_and_model(conn, people):
    bio = nos(P.search(conn, manufacturer="BIO", limit=500)[0])
    assert {people[k] for k in ("damico", "deluca", "rossi1", "rossi2")} <= bio and people["rossini"] not in bio
    iii = nos(P.search(conn, manufacturer="BIO", model="BIOMONITOR III", limit=500)[0])
    assert people["damico"] in iii and people["rossi1"] in iii and people["deluca"] not in iii
    # filtro + testo insieme
    assert nos(P.search(conn, "rossi", model="BIOMONITOR IV")[0]) & set(people.values()) == {people["rossi2"]}


def test_groups(conn, people):
    g = P.create_group(conn, "Pazienti Dott. X")
    assert P.create_group(conn, "Pazienti Dott. X") == g          # stesso nome = stesso gruppo
    assert P.add_to_group(conn, g, [people["rossi1"], people["deluca"]]) == 2
    assert P.add_to_group(conn, "Pazienti Dott. X", [people["rossi1"]]) == 0  # già presente
    P.create_group(conn, "Gruppo Biotronik")
    P.add_to_group(conn, "Gruppo Biotronik", [people["rossi1"], people["damico"]])

    assert nos(P.search(conn, group=g)[0]) == {people["rossi1"], people["deluca"]}
    assert nos(P.search(conn, "rossi", group="Pazienti Dott. X")[0]) == {people["rossi1"]}
    row = next(r for r in P.search(conn, "rossi mario")[0] if r["patient_no"] == people["rossi1"])
    assert row["groups"] == "Gruppo Biotronik, Pazienti Dott. X"

    assert P.remove_from_group(conn, g, [people["deluca"]]) == 1
    assert nos(P.search(conn, group=g)[0]) == {people["rossi1"]}


def test_nao_filter(conn, people):
    P.set_nao_therapy(conn, people["rossi2"], True)
    si = nos(P.search(conn, "rossi", nao="SI")[0])
    nd = nos(P.search(conn, "rossi", nao="NON_INDICATO")[0])
    assert people["rossi2"] in si and people["rossi2"] not in nd and people["rossi1"] in nd
    with pytest.raises(ValueError):
        P.search(conn, nao="FORSE")


def test_search_options_for_dropdowns(conn, people):
    o = P.search_options(conn)
    assert {"code": "BIO", "name": "Biotronik"} in [{k: m[k] for k in ("code", "name")} for m in o["manufacturers"]]
    assert any(m["code"] == "MDT" and m["name"] == "MDT" for m in o["manufacturers"])   # nome non in catalogo
    models = {(m["manufacturer"], m["model"]) for m in o["models"]}
    assert {("BIO", "BIOMONITOR III"), ("BIO", "BIOMONITOR IV"), ("MDT", "Azure XT")} <= models
    assert all(isinstance(g["group_pk"], int) for g in o["groups"])


def test_pagination_and_total(conn, people):
    page1, total = P.search(conn, "rossi", limit=2)
    page2, total2 = P.search(conn, "rossi", limit=2, offset=2)
    assert total == total2 >= 3 and len(page1) == 2 and not (nos(page1) & nos(page2))


def test_search_text_follows_device_changes(conn, people):
    conn.execute("UPDATE vault.device_identity SET model = 'AZURE XT DR' WHERE serial_number = 'MDT-999'")
    assert people["rossini"] in nos(P.search(conn, "azure dr")[0])


def test_index_is_usable(conn, people):
    conn.execute("SET enable_seqscan = off")
    try:
        plan = "\n".join(r[0] for r in conn.execute(
            "EXPLAIN SELECT 1 FROM vault.patient WHERE search_text LIKE '% rossi%'").fetchall())
    finally:
        conn.execute("RESET enable_seqscan")
    assert "ix_patient_search" in plan
