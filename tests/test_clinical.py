"""Regole cliniche: obbligatorietà all'impianto e all'espianto, esito del loop recorder."""
from datetime import date

import psycopg
import pytest
from conftest import PSEUDO_KEY
from test_loader import make_xml

from cied.clinical import (Condition, add_diagnoses, register_device, register_explant, register_implant,
                           register_patient)
from cied.db.loader import load_export


@pytest.fixture(scope="module")
def ilr(conn):
    """Loop recorder arrivato da Home Monitoring (paziente 'paz-clin')."""
    r = load_export(conn, make_xml(10, serial="ILR-0001", patient="paz-clin"), PSEUDO_KEY)
    return r["device_pk"], r["patient_pk"]


def implant_count(conn):
    return conn.execute("SELECT count(*) FROM cied.implant").fetchone()[0]


# --- impianto ---------------------------------------------------------
def test_implant_requires_pregresse(conn, ilr):
    before = implant_count(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="patologia pregressa"):
        register_implant(conn, ilr[0], pregresse=[], indicazioni=["R55"], nao_therapy=False)
    assert implant_count(conn) == before          # nulla salvato: transazione annullata


def test_implant_requires_indicazione(conn, ilr):
    with pytest.raises(psycopg.errors.RaiseException, match="motivazione"):
        register_implant(conn, ilr[0], pregresse=["I10"], indicazioni=[], nao_therapy=False)


def test_none_cannot_be_combined(conn, ilr):
    with pytest.raises(psycopg.errors.RaiseException, match="nessuna patologia"):
        register_implant(conn, ilr[0], pregresse=["LOCAL:NONE", "I10"], indicazioni=["R55"], nao_therapy=False)


def test_unknown_condition(conn, ilr):
    with pytest.raises(ValueError, match="catalogo"):
        register_implant(conn, ilr[0], pregresse=["XYZ"], indicazioni=["R55"], nao_therapy=False)


# --- percorso completo: loop recorder -> ICD ---------------------------
def test_ilr_to_icd_pathway(conn, ilr):
    device_pk, patient_pk = ilr
    ilr_impl = register_implant(
        conn, device_pk,                                     # data di impianto dal dispositivo
        pregresse=["I42.2", "I10"],
        indicazioni=["R55"], nao_therapy=False,
    )
    # espianto senza motivazione -> vincolo CHECK immediato
    with pytest.raises(psycopg.errors.CheckViolation):
        register_explant(conn, ilr_impl, date(2026, 11, 1), None, ["I47.2"], switched_to_cied=True)
    # espianto senza patologie diagnosticate -> errore al commit
    with pytest.raises(psycopg.errors.RaiseException, match="diagnosticate"):
        register_explant(conn, ilr_impl, date(2026, 11, 1), "DIAGNOSIS_REACHED", [], switched_to_cied=True)
    # loop recorder seguito da CIED senza dire se è stato determinante -> errore
    with pytest.raises(psycopg.errors.RaiseException, match="determinante"):
        register_explant(conn, ilr_impl, date(2026, 11, 1), "DIAGNOSIS_REACHED", ["I47.2"],
                         switched_to_cied=True)
    # motivazione "OTHER" senza nota -> errore
    with pytest.raises(psycopg.errors.CheckViolation):
        register_explant(conn, ilr_impl, date(2026, 11, 1), "OTHER", ["I47.2"], switched_to_cied=False)

    # nuovo ICD (altro produttore, inserito a mano) per lo stesso paziente
    icd_dev = register_device(conn, PSEUDO_KEY, patient_pk, "MDT", "ICD-777", "Modello ICD", "ICD")
    icd_impl = register_implant(conn, icd_dev, pregresse=["I42.2", "I10"], indicazioni=["I47.2"],
                                implant_date=date(2026, 11, 1), nao_therapy=False)
    register_explant(conn, ilr_impl, date(2026, 11, 1), "DIAGNOSIS_REACHED",
                     [Condition("I47.2", first_diagnosis_date=date(2026, 10, 15))],
                     switched_to_cied=True, next_implant_pk=icd_impl, ilr_decisive=True)

    row = conn.execute(
        "SELECT switched_to_cied, ilr_decisive, next_device_type, days_to_therapy, therapeutic_after_ilr "
        "FROM cied.v_ilr_outcome WHERE ilr_implant_pk = %s", (ilr_impl,)).fetchone()
    assert row == (True, True, "ICD", (date(2026, 11, 1) - date(2024, 4, 5)).days, 1)

    preg, ind, diag = conn.execute(
        "SELECT pregresse, indicazioni, diagnosticate FROM cied.v_implant WHERE implant_pk = %s",
        (ilr_impl,)).fetchone()
    assert "I42.2" in preg and "I10" in preg and ind.startswith("R55") and diag.startswith("I47.2")


def test_next_implant_must_be_same_patient(conn):
    p1 = register_patient(conn, PSEUDO_KEY, "Altro", "Uno")
    p2 = register_patient(conn, PSEUDO_KEY, "Altro", "Due")
    d1 = register_device(conn, PSEUDO_KEY, p1, "BIO", "PM-1", "PM test", "PM", date(2020, 1, 1))
    d2 = register_device(conn, PSEUDO_KEY, p2, "BIO", "PM-2", "PM test", "PM", date(2025, 1, 1))
    i1 = register_implant(conn, d1, ["LOCAL:NONE"], ["I44.2"], nao_therapy=False)
    i2 = register_implant(conn, d2, ["LOCAL:NONE"], ["I44.2"], nao_therapy=False)
    with pytest.raises(psycopg.errors.RaiseException, match="stesso paziente"):
        register_explant(conn, i1, date(2025, 1, 1), "BATTERY_EOL", ["I44.2"],
                         switched_to_cied=True, next_implant_pk=i2)


def test_explant_fields_need_explant_date(conn):
    p = register_patient(conn, PSEUDO_KEY, "Altro", "Tre")
    d = register_device(conn, PSEUDO_KEY, p, "BIO", "PM-3", "PM test", "PM", date(2021, 1, 1))
    i = register_implant(conn, d, ["I10"], ["I49.5"], nao_therapy=False)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE cied.implant SET explant_reason_code = 'INFECTION' WHERE implant_pk = %s", (i,))


# --- patologia sospetta (solo loop recorder) ---------------------------
def new_device(conn, serial, device_type):
    p = register_patient(conn, PSEUDO_KEY, "Sospetta", serial)
    return register_device(conn, PSEUDO_KEY, p, "BIO", serial, "Modello test", device_type, date(2025, 1, 1))


def ilr_outcome(conn, implant_pk):
    return conn.execute("SELECT sospette, sospetto_confermato FROM cied.v_ilr_outcome "
                        "WHERE ilr_implant_pk = %s", (implant_pk,)).fetchone()


def test_sospetta_only_for_loop_recorder(conn):
    d = new_device(conn, "PM-SOSP", "PM")
    before = implant_count(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="solo per i loop recorder"):
        register_implant(conn, d, ["LOCAL:NONE"], ["I44.2"], nao_therapy=False, sospette=["I48"])
    assert implant_count(conn) == before


def test_sospetta_confirmed(conn):
    i = register_implant(conn, new_device(conn, "ILR-SOSP-1", "Monitor"), ["I10"], ["R55"],
                         nao_therapy=False, sospette=["I48"])
    (sosp,) = conn.execute("SELECT sospette FROM cied.v_implant WHERE implant_pk = %s", (i,)).fetchone()
    assert sosp.startswith("I48")
    assert ilr_outcome(conn, i) == (sosp, None)          # ancora impiantato: esito non noto
    register_explant(conn, i, date(2026, 6, 1), "DIAGNOSIS_REACHED", ["I48"], switched_to_cied=False)
    assert ilr_outcome(conn, i) == (sosp, True)


def test_sospetta_not_confirmed(conn):
    i = register_implant(conn, new_device(conn, "ILR-SOSP-2", "Monitor"), ["I10"], ["R55"],
                         nao_therapy=False, sospette=["I48"])
    register_explant(conn, i, date(2026, 6, 1), "DIAGNOSIS_REACHED", ["I47.2"], switched_to_cied=False)
    assert ilr_outcome(conn, i)[1] is False


def test_sospetta_is_optional(conn):
    i = register_implant(conn, new_device(conn, "ILR-SOSP-3", "Monitor"), ["I10"], ["R55"], nao_therapy=False)
    assert ilr_outcome(conn, i) == (None, None)


# --- data della prima diagnosi: solo per le patologie diagnosticate ----
@pytest.mark.parametrize("ruolo", ["pregresse", "indicazioni", "sospette"])
def test_first_diagnosis_date_only_for_diagnosticate(conn, ruolo):
    d = new_device(conn, f"ILR-DATA-{ruolo}", "Monitor")
    kwargs = {"pregresse": ["I10"], "indicazioni": ["R55"], "nao_therapy": False}
    kwargs[ruolo] = [Condition("I48", first_diagnosis_date=date(2024, 1, 1))]
    with pytest.raises(ValueError, match="solo per le patologie diagnosticate"):
        register_implant(conn, d, **kwargs)


def test_first_diagnosis_date_enforced_by_database(conn):
    """Anche scrivendo direttamente nella tabella, senza passare da clinical.py."""
    i = register_implant(conn, new_device(conn, "ILR-DATA-SQL", "Monitor"), ["I10"], ["R55"], nao_therapy=False)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE cied.implant_condition SET first_diagnosis_date = '2024-01-01' "
                     "WHERE implant_pk = %s AND role = 'PREGRESSA'", (i,))
    (d,) = conn.execute("SELECT first_diagnosis_date FROM cied.implant_condition "
                        "WHERE role = 'DIAGNOSTICATA' AND first_diagnosis_date IS NOT NULL LIMIT 1").fetchone()
    assert d == date(2026, 10, 15)                    # salvata dal percorso loop recorder -> ICD


# --- diagnosi aggiunte in un secondo momento, per qualsiasi dispositivo ----
def diagnosticate(conn, implant_pk):
    return conn.execute("""SELECT c.code, ic.first_diagnosis_date FROM cied.implant_condition ic
                             JOIN cied.condition c USING (condition_pk)
                            WHERE ic.implant_pk = %s AND ic.role = 'DIAGNOSTICATA' ORDER BY c.code""",
                        (implant_pk,)).fetchall()


@pytest.mark.parametrize("device_type", ["PM", "ICD", "Monitor"])
def test_add_diagnosis_while_implanted(conn, device_type):
    """Es. FA non nota rilevata da un pacemaker: il dispositivo resta impiantato."""
    i = register_implant(conn, new_device(conn, f"DIAG-{device_type}", device_type),
                         ["LOCAL:NONE"], ["I44.2"], nao_therapy=False)
    add_diagnoses(conn, i, [Condition("I48", first_diagnosis_date=date(2026, 11, 20))])
    assert diagnosticate(conn, i) == [("I48", date(2026, 11, 20))]
    assert conn.execute("SELECT explant_date FROM cied.implant WHERE implant_pk = %s", (i,)).fetchone() == (None,)


def test_add_diagnosis_after_explant(conn):
    i = register_implant(conn, new_device(conn, "DIAG-POST", "PM"), ["I10"], ["I44.2"], nao_therapy=False)
    register_explant(conn, i, date(2026, 6, 1), "BATTERY_EOL", ["I44.2"], switched_to_cied=False)
    add_diagnoses(conn, i, ["I48"])
    assert [c for c, _ in diagnosticate(conn, i)] == ["I44.2", "I48"]


def test_add_diagnosis_errors(conn):
    i = register_implant(conn, new_device(conn, "DIAG-ERR", "ICD"), ["I10"], ["I47.2"], nao_therapy=False)
    add_diagnoses(conn, i, ["I48"])
    with pytest.raises(ValueError, match="già registrata"):
        add_diagnoses(conn, i, ["I48"])
    with pytest.raises(ValueError, match="almeno una"):
        add_diagnoses(conn, i, [])
    with pytest.raises(ValueError, match="inesistente"):
        add_diagnoses(conn, 999999, ["I48"])
    with pytest.raises(ValueError, match="catalogo"):
        add_diagnoses(conn, i, ["XYZ"])
    assert diagnosticate(conn, i) == [("I48", None)]   # gli errori non hanno salvato nulla


# --- riepilogo clinico -------------------------------------------------
def test_clinical_summary_is_identified_and_logged(conn, ilr):
    rows = conn.execute("SELECT name_family, name_given, device_type, clinical_record "
                        "FROM vault.clinical_summary()").fetchall()
    assert ("Bianchi", "Anna", "Monitor", "completa") in rows
    (n,) = conn.execute("SELECT count(*) FROM vault.access_log WHERE action='clinical_summary'").fetchone()
    assert n >= 1


def test_ilr_outcome_intervals(conn):
    """Giorni di monitoraggio e alla diagnosi: esportabili al posto delle date precise."""
    i = register_implant(conn, new_device(conn, "ILR-GIORNI", "Monitor"), ["I10"], ["R55"], nao_therapy=False)
    add_diagnoses(conn, i, [Condition("I48", first_diagnosis_date=date(2025, 3, 2))])   # impianto: 01/01/2025
    register_explant(conn, i, date(2025, 7, 20), "BATTERY_EOL", [], switched_to_cied=False)
    row = conn.execute("SELECT days_monitored, days_to_diagnosis FROM cied.v_ilr_outcome "
                       "WHERE ilr_implant_pk = %s", (i,)).fetchone()
    assert row == (200, 60)
