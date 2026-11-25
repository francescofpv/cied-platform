"""Caricamento nel DB su XML sintetico (DB temporaneo, vedi conftest.py)."""
import threading

import psycopg
import pytest
from conftest import PSEUDO_KEY
from psycopg.conninfo import make_conninfo

from cied.config import settings
from cied.db import connect
from cied.db.loader import load_export

SESSION = "20261023T022001+0200"       # data/ora della sessione nell'XML di prova

XML = """<?xml version="1.0" encoding="UTF-16"?>
<biotronik-ieee11073-export creator="BioHMSC" creator-version="3.68.0" format-version="4.10"><dataset>
 <section name="MDC">
  <section name="IDC">
   <section name="DEV">
    <value code="720897" name="TYPE" type="MDC_IDC_ENUM_DEV_TYPE">Monitor</value>
    <value code="720898" name="MODEL" type="String">TEST DEVICE</value>
    <value code="720899" name="SERIAL" type="String">TEST-000001</value>
    <value code="720900" name="MFG" type="MDC_IDC_ENUM_MFG">BIO</value>
    <value code="720901" name="IMPLANT_DT" type="DateTime">20240405T000000</value>
   </section>
   <section name="SESS">
    <value code="721025" name="DTM" type="DateTime">20261023T022001+0200</value>
    <value code="721031" name="CLINICIAN_NAME" type="String">Dottor Mario Rossi</value>
   </section>
   <section name="STAT"><section name="AT">
    <value code="737665" name="DTM_START" type="DateTime">20261003T013302</value>
    <value code="737696" name="BURDEN_PERCENT" type="Numeric" unit="%">12</value>
   </section></section>
  </section>
  <section name="ATTR"><section name="PT">
   <value code="67930" name="ID" type="String">paziente-x</value>
   <value name="NAME_FAMILY" type="String">Bianchi</value>
   <value name="NAME_GIVEN" type="String">Anna</value>
   <value name="DOB" type="DateTime">19580312T000000</value>
  </section></section>
 </section>
 <section name="BIO"><section name="REQUEST">
  <value name="ID" type="String">REQ-{n}</value>
  <section name="HM"><section name="PATIENT">
   <value name="CLINIC_ID" type="String">clinica-test</value>
   <value name="COMMENT" type="String">nota clinica riservata</value>
   <value name="LINK" type="String">https://example.invalid/paziente</value>
  </section></section>
 </section></section>
</dataset></biotronik-ieee11073-export>"""


def make_xml(n: int = 1, serial: str = "TEST-000001", patient: str = "paziente-x") -> bytes:
    return (XML.replace("{n}", str(n)).replace("TEST-000001", serial)
            .replace("paziente-x", patient).encode("utf-16"))


# --- idempotenza: chiave naturale (dispositivo + data/ora della sessione) ---------
def count_sessions(conn, serial):
    return conn.execute(
        """SELECT count(*) FROM cied.session s JOIN cied.device d USING (device_pk)
             JOIN vault.device_identity di USING (device_pseudo) WHERE di.serial_number = %s""",
        (serial,)).fetchone()[0]


def test_same_file_twice_is_duplicate(conn):
    first = load_export(conn, make_xml(1, serial="IDEM-1"), PSEUDO_KEY)
    second = load_export(conn, make_xml(1, serial="IDEM-1"), PSEUDO_KEY)
    assert first["status"] == "loaded" and first["observations"] > 0
    assert second["status"] == "duplicate" and second["session_pk"] == first["session_pk"]
    assert count_sessions(conn, "IDEM-1") == 1


def test_same_session_in_a_different_file_is_duplicate(conn):
    """Stesso report con byte diversi: altro ID di richiesta, altra codifica (UTF-16 big endian)."""
    load_export(conn, make_xml(1, serial="IDEM-2"), PSEUDO_KEY)
    other_request = make_xml(2, serial="IDEM-2")
    big_endian = b"\xfe\xff" + make_xml(3, serial="IDEM-2").decode("utf-16").encode("utf-16-be")
    assert load_export(conn, other_request, PSEUDO_KEY)["status"] == "duplicate"
    assert load_export(conn, big_endian, PSEUDO_KEY)["status"] == "duplicate"
    assert count_sessions(conn, "IDEM-2") == 1
    (n_exports,) = conn.execute(
        """SELECT count(*) FROM cied.export e JOIN cied.device d USING (device_pk)
             JOIN vault.device_identity di USING (device_pseudo) WHERE di.serial_number = 'IDEM-2'""").fetchone()
    assert n_exports == 1                               # nessun export in più


def test_new_session_of_same_device_is_loaded(conn):
    load_export(conn, make_xml(1, serial="IDEM-3"), PSEUDO_KEY)
    later = make_xml(2, serial="IDEM-3").decode("utf-16").replace(SESSION, "20261110T080000+0100")
    r = load_export(conn, later.encode("utf-16"), PSEUDO_KEY)
    assert r["status"] == "loaded" and count_sessions(conn, "IDEM-3") == 2


def test_same_instant_with_different_offset_is_duplicate(conn):
    """02:20 a +02:00 e 00:20 UTC sono lo stesso istante: stessa sessione."""
    load_export(conn, make_xml(1, serial="IDEM-4"), PSEUDO_KEY)
    utc = make_xml(2, serial="IDEM-4").decode("utf-16").replace(SESSION, "20261023T002001+0000")
    assert load_export(conn, utc.encode("utf-16"), PSEUDO_KEY)["status"] == "duplicate"


def test_duplicate_updates_notifications(conn):
    """La stessa sessione può arrivare con una notifica cambiata di stato: si aggiorna solo quella."""
    def with_notification(n, status, update):
        notif = ('<section name="NOTIFICATION"><value name="ID" type="String">NOTIF-IDEM-5</value>'
                 f'<value name="STATUS" type="String">{status}</value>'
                 f'<value name="UPDATE_DTM" type="DateTime">{update}</value></section>')
        xml = make_xml(n, serial="IDEM-5").decode("utf-16")
        return xml.replace('<section name="HM"><section name="PATIENT">',
                           '<section name="HM">' + notif + '<section name="PATIENT">').encode("utf-16")

    first = load_export(conn, with_notification(1, "UNACKNOWLEDGED", "20261023T030000+0200"), PSEUDO_KEY)
    again = load_export(conn, with_notification(2, "ACKNOWLEDGED", "20261024T090000+0200"), PSEUDO_KEY)
    assert first["notifications"] == 1
    assert again["status"] == "duplicate" and again["notifications"] == 1
    status, export_pk = conn.execute("SELECT status, last_export_pk FROM cied.notification "
                                     "WHERE vendor_id = 'NOTIF-IDEM-5'").fetchone()
    assert status == "ACKNOWLEDGED" and export_pk == first["export_pk"]


def test_database_refuses_duplicate_session(conn):
    """Anche scrivendo direttamente nel database il vincolo impedisce il doppione."""
    r = load_export(conn, make_xml(1, serial="IDEM-6"), PSEUDO_KEY)
    with pytest.raises(psycopg.errors.UniqueViolation, match="uq_session_device_dtm"):
        conn.execute("""INSERT INTO cied.session (device_pk, export_pk, session_dtm)
                        SELECT device_pk, export_pk, session_dtm FROM cied.session WHERE session_pk = %s""",
                     (r["session_pk"],))


def test_simultaneous_imports_of_same_report(conn):
    """Lo stesso report importato nello stesso momento da 4 connessioni: una sola sessione."""
    dbname = conn.info.dbname
    data = make_xml(1, serial="IDEM-7")
    statuses, errors = [], []

    def worker():
        try:
            with connect(make_conninfo(settings.db_conninfo, dbname=dbname)) as c:
                statuses.append(load_export(c, data, PSEUDO_KEY)["status"])
        except Exception as e:           # pragma: no cover - deve restare vuoto
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(statuses) == ["duplicate", "duplicate", "duplicate", "loaded"]
    assert count_sessions(conn, "IDEM-7") == 1


def test_identity_only_in_vault(conn):
    load_export(conn, make_xml(3, serial="VAULT-1"), PSEUDO_KEY)
    texts = {t for (t,) in conn.execute("SELECT value_text FROM cied.observation").fetchall()}
    for secret in ("VAULT-1", "Dottor Mario Rossi", "paziente-x", "Bianchi", "Anna",
                   "nota clinica riservata", "https://example.invalid/paziente"):
        assert secret not in texts
    assert "12" in texts

    fam, giv, dob = conn.execute(
        "SELECT name_family, name_given, birth_date FROM vault.patient p "
        "JOIN vault.patient_identifier i USING (patient_no) WHERE i.value='paziente-x'"
    ).fetchone()
    assert (fam, giv, str(dob)) == ("Bianchi", "Anna", "1958-03-12")
    assert conn.execute("SELECT birth_year FROM cied.patient").fetchone()[0] == 1958


def test_naive_datetime_gets_session_offset(conn):
    load_export(conn, make_xml(4, serial="TZ-1"), PSEUDO_KEY)
    (tz_assumed,) = conn.execute(
        "SELECT tz_assumed FROM cied.observation WHERE name='DTM_START' LIMIT 1").fetchone()
    assert tz_assumed is True


def test_patient_fields_as_in_real_export(conn):
    """Sezione MDC/ATTR/PT con i nomi e i tipi del vero export BIOTRONIK (DOB di tipo Date, SEX numerico)."""
    real_pt = ('<value code="67930" name="ID" type="String">paziente-reale</value>'
               '<value code="67933" name="NAME_GIVEN" type="String">Maria</value>'
               '<value code="67932" name="NAME_FAMILY" type="String">Verdi</value>'
               '<value code="67937" name="SEX" type="String">2</value>'
               '<value code="67928" name="DOB" type="Date">19560330</value>')
    xml = XML.replace("{n}", "30").replace("TEST-000001", "TEST-REAL-PT")
    start = xml.index('<section name="ATTR"><section name="PT">') + len('<section name="ATTR"><section name="PT">')
    end = xml.index("</section></section>", start)
    load_export(conn, (xml[:start] + real_pt + xml[end:]).encode("utf-16"), PSEUDO_KEY)

    fam, giv, dob, sex = conn.execute(
        "SELECT name_family, name_given, birth_date, sex FROM vault.patient "
        "JOIN vault.patient_identifier i USING (patient_no) WHERE i.value='paziente-reale'").fetchone()
    assert (fam, giv, str(dob), sex) == ("Verdi", "Maria", "1956-03-30", "F")
    year, csex = conn.execute(
        "SELECT p.birth_year, p.sex FROM cied.patient p JOIN vault.patient v USING (patient_pseudo) "
        "JOIN vault.patient_identifier i USING (patient_no) WHERE i.value='paziente-reale'").fetchone()
    assert (year, csex) == (1956, "F")       # in cied solo anno di nascita e sesso
