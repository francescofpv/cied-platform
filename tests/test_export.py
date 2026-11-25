"""Export CSV (COPY nativo di PostgreSQL) su DB temporaneo."""
import csv

import pytest
from conftest import PSEUDO_KEY
from test_loader import make_xml

from cied.db.loader import load_export
from cied.export import export_csv, exportable, groups


def test_export_measurements(conn, tmp_path):
    load_export(conn, make_xml(1, serial="EXP-1"), PSEUDO_KEY)
    path = tmp_path / "m.csv"
    rows = export_csv(conn, "v_measurement", path)
    data = list(csv.DictReader(path.open(encoding="utf-8")))
    assert rows == len(data) > 0
    assert {"patient_pseudo", "device_pseudo", "term", "value_num"} <= set(data[0])
    assert any(r["term"] == "MDC_IDC_STAT_AT_BURDEN_PERCENT" and float(r["value_num"]) == 12 for r in data)


def test_export_contains_no_identifying_data(conn, tmp_path):
    load_export(conn, make_xml(1, serial="EXP-2"), PSEUDO_KEY)
    for name in exportable(conn):
        export_csv(conn, name, tmp_path / f"{name}.csv")
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.csv"))
    for secret in ("Bianchi", "Anna", "EXP-2", "paziente-x", "Dottor Mario Rossi", "example.invalid"):
        assert secret not in text


def test_excel_format(conn, tmp_path):
    path = tmp_path / "x.csv"
    export_csv(conn, "v_measurement", path, excel=True)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b";" in raw.splitlines()[0]


@pytest.mark.parametrize("name", ["vault.patient", "clinical_summary", 'v_measurement"; DROP TABLE x; --'])
def test_vault_and_strange_names_are_refused(conn, tmp_path, name):
    with pytest.raises(ValueError):
        export_csv(conn, name, tmp_path / "x.csv")


# --- esportazione di un solo gruppo di pazienti ---------------------------
@pytest.fixture(scope="module")
def two_patients(conn):
    """Due pazienti con il proprio dispositivo; solo il primo è nel gruppo 'Studio FA'."""
    a = load_export(conn, make_xml(1, serial="GRP-A", patient="paz-gruppo-a"), PSEUDO_KEY)
    b = load_export(conn, make_xml(1, serial="GRP-B", patient="paz-gruppo-b"), PSEUDO_KEY)
    gpk = conn.execute("INSERT INTO cied.patient_group (name) VALUES ('Studio FA') RETURNING group_pk").fetchone()[0]
    conn.execute("INSERT INTO cied.patient_group_member (group_pk, patient_pk) VALUES (%s, %s)",
                 (gpk, a["patient_pk"]))
    pseudo = dict(conn.execute("SELECT patient_pk, patient_pseudo FROM cied.patient WHERE patient_pk IN (%s, %s)",
                               (a["patient_pk"], b["patient_pk"])).fetchall())
    return pseudo[a["patient_pk"]], pseudo[b["patient_pk"]]


def test_group_export_keeps_only_group_members(conn, tmp_path, two_patients):
    inside, outside = two_patients
    rows = export_csv(conn, "v_measurement", tmp_path / "m.csv", group="Studio FA")
    data = list(csv.DictReader((tmp_path / "m.csv").open(encoding="utf-8")))
    assert rows == len(data) > 0
    assert {r["patient_pseudo"] for r in data} == {inside}


@pytest.mark.parametrize("name", ["v_measurement", "v_episode", "v_zone", "v_device_pending",
                                  "session", "observation", "notification", "device", "patient"])
def test_group_filter_on_views_and_tables(conn, tmp_path, two_patients, name):
    """Ogni vista o tabella si filtra attraverso la colonna che porta al paziente."""
    _, outside = two_patients
    full = export_csv(conn, name, tmp_path / "all.csv")
    part = export_csv(conn, name, tmp_path / "grp.csv", group="Studio FA")
    assert part <= full
    assert outside not in (tmp_path / "grp.csv").read_text(encoding="utf-8")


def test_group_export_by_pk_and_excel(conn, tmp_path, two_patients):
    gpk = conn.execute("SELECT group_pk FROM cied.patient_group WHERE name = 'Studio FA'").fetchone()[0]
    by_name = export_csv(conn, "v_measurement", tmp_path / "a.csv", group="Studio FA")
    by_pk = export_csv(conn, "v_measurement", tmp_path / "b.csv", excel=True, group=gpk)
    assert by_name == by_pk


def test_group_errors(conn, tmp_path, two_patients):
    with pytest.raises(ValueError, match="inesistente"):
        export_csv(conn, "v_measurement", tmp_path / "x.csv", group="Gruppo che non esiste")
    with pytest.raises(ValueError, match="non contiene dati dei pazienti"):
        export_csv(conn, "condition", tmp_path / "x.csv", group="Studio FA")


def test_groups_list(conn, two_patients):
    assert ("Studio FA", 1) in groups(conn)


# --- età al posto dell'anno di nascita, anno al posto della data d'impianto ---
PRECISE = {"birth_year", "birth_date", "implant_date", "implant_dt", "ilr_implant_date", "next_implant_date"}


def test_no_birth_or_precise_implant_date_is_exported(conn, tmp_path):
    from datetime import date

    from cied.clinical import register_implant
    r = load_export(conn, make_xml(1, serial="EXP-AGE", patient="paz-eta"), PSEUDO_KEY)
    implant_pk = register_implant(conn, r["device_pk"], ["I10"], ["R55"], nao_therapy=False)
    birth_year, implant_date = conn.execute(
        "SELECT p.birth_year, i.implant_date FROM cied.implant i JOIN cied.patient p USING (patient_pk) "
        "WHERE i.implant_pk = %s", (implant_pk,)).fetchone()
    assert birth_year and implant_date

    for name in exportable(conn):
        export_csv(conn, name, tmp_path / f"{name}.csv")
        header = (tmp_path / f"{name}.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
        assert not PRECISE & set(header), (name, PRECISE & set(header))

    patients = list(csv.DictReader((tmp_path / "patient.csv").open(encoding="utf-8")))
    assert str(date.today().year - birth_year) in {p["age"] for p in patients}

    implants = {row["implant_pk"]: row for row in csv.DictReader((tmp_path / "v_implant.csv").open(encoding="utf-8"))}
    row = implants[str(implant_pk)]
    assert row["implant_year"] == str(implant_date.year)
    assert row["age_at_implant"] == str(implant_date.year - birth_year)
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.csv"))
    assert implant_date.isoformat() not in text            # la data precisa non compare da nessuna parte

    obs = [o for o in csv.DictReader((tmp_path / "observation.csv").open(encoding="utf-8"))
           if o["term"] == "MDC_IDC_DEV_IMPLANT_DT"]
    assert obs and all(o["value_text"] == str(implant_date.year) and o["value_dtm"] == "" for o in obs)
