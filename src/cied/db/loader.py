"""Import di un export BIOTRONIK nel database.

Ogni export contiene UNA sessione (interrogazione) di UN dispositivo.

IDEMPOTENZA - chiave naturale
    Una sessione è identificata da:
        dispositivo (produttore + modello + seriale)  +  data/ora della sessione (MDC_IDC_SESS_DTM)
    Se la sessione è già nel database l'export NON viene importato di nuovo, anche se
    arriva in un file diverso (altra codifica, altro ID di richiesta, ecc.): si
    aggiornano solo le notifiche, che nel frattempo possono essere cambiate di stato.
    La garanzia finale è il vincolo UNIQUE uq_session_device_dtm su cied.session.

Passi, tutti nella stessa transazione (o tutto o niente):
    1. lettura dell'XML
    2. blocco sul dispositivo: due import dello stesso dispositivo non si sovrappongono
    3. la sessione esiste già?  sì -> aggiorna le notifiche e si ferma
    4. paziente  (anagrafica nel vault, pseudonimo in cied)
    5. dispositivo (seriale nel vault, pseudonimo in cied)
    6. export + XML originale (nel vault) + sessione
    7. valori misurati (tranne quelli identificativi) e notifiche
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone, tzinfo

from cied.db.identity import find_or_create_patient, save_device
from cied.parser import parse_export
from cied.parser.ieee11073 import ExportValue, ParsedExport, xml_text
from cied.pseudo import device_pseudonym

SOURCE = "BIOTRONIK_HM"
LOADED = "loaded"           # importato
DUPLICATE = "duplicate"     # sessione già presente: non importato di nuovo

MESSAGES = {LOADED: "caricato correttamente", DUPLICATE: "sessione già presente (duplicato)"}

# --- cosa NON entra nello schema pseudonimizzato --------------------------
IDENTIFYING_PATHS = {"MDC/IDC/DEV/SERIAL", "MDC/IDC/SESS/CLINICIAN_NAME"}
IDENTIFYING_PREFIXES = ("MDC/ATTR/PT/", "BIO/REQUEST/HM/USER/")      # anagrafica, utente HMSC
# Dati del paziente lato BIOTRONIK: si tengono in cied SOLO questi; tutto il resto
# (nome, link, commento, campi nuovi che potrebbero comparire) resta nel vault.
BIO_PATIENT = "BIO/REQUEST/HM/PATIENT/"
BIO_PATIENT_ALLOWED = {"ACTIVATION_STATUS", "ACTIVATION_DATE", "CLINIC_ID", "CLINIC_NAME", "GROUP"}
# Valori già salvati in tabelle dedicate (cied.export, cied.notification)
NOTIFICATION = "BIO/REQUEST/HM/NOTIFICATION"
ALREADY_SAVED = {"BIO/REQUEST/DATE", "BIO/REQUEST/ID", "BIO/REQUEST/CONTAINS_PII_DATA"}

# MDC_ATTR_PT_SEX: codici IEEE 11073 / ISO 5218
SEX_CODES = {"0": "U", "1": "M", "2": "F", "9": "U",
             "M": "M", "F": "F", "MALE": "M", "FEMALE": "F", "U": "U", "UNKNOWN": "U"}


def without_index(path: str) -> str:
    """'MDC/IDC/STAT/EPISODE[3]/TYPE' -> 'MDC/IDC/STAT/EPISODE/TYPE'"""
    return re.sub(r"\[\d+\]", "", path)


def is_identifying(path: str) -> bool:
    """True se il valore identifica il paziente e deve restare nel vault."""
    path = without_index(path)
    if path in IDENTIFYING_PATHS or path.startswith(IDENTIFYING_PREFIXES):
        return True
    if path.startswith(BIO_PATIENT):
        return path[len(BIO_PATIENT):] not in BIO_PATIENT_ALLOWED
    return False


def normalize_sex(raw: str | None) -> str | None:
    """'1' -> 'M', '2' -> 'F', '0'/'9' -> 'U'; un valore sconosciuto resta com'è."""
    if raw is None or not raw.strip():
        return None
    return SEX_CODES.get(raw.strip().upper(), raw.strip())


def to_date(v: ExportValue | None):
    """Data da un valore DateTime o Date ('19560330', '1956-03-30', '30/03/1956')."""
    if v is None or not v.raw:
        return None
    if v.dtm:
        return v.dtm.date()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v.raw, fmt).date()
        except ValueError:
            pass
    return None


def with_timezone(v: ExportValue | None, tz: tzinfo) -> tuple[datetime | None, bool | None]:
    """(data/ora con fuso, fuso_dedotto). Alle date senza fuso si dà quello della sessione."""
    if v is None or v.dtm is None:
        return None, None
    if v.tz_known:
        return v.dtm, False
    return v.dtm.replace(tzinfo=tz), True


# --- lettura dei dati principali -------------------------------------------
def read_patient(exp: ParsedExport) -> dict | None:
    """Anagrafica (MDC/ATTR/PT). None se l'export non contiene l'ID del paziente."""
    pt = {v.name: v for v in exp.values if without_index(v.group) == "MDC/ATTR/PT"}
    if "ID" not in pt or not pt["ID"].raw:
        return None

    def first(*names):
        return next((pt[n] for n in names if n in pt), None)

    family, given = first("NAME_FAMILY", "LAST_NAME"), first("NAME_GIVEN", "FIRST_NAME")
    sex = first("SEX", "GENDER")
    # tutti i campi anagrafici ricevuti, per non perdere nulla
    attributes = {without_index(v.path): v.raw for v in exp.values
                  if without_index(v.path).startswith(("MDC/ATTR/PT/", BIO_PATIENT)) and is_identifying(v.path)}
    return {
        "vendor_patient_id": pt["ID"].raw,
        "clinic_id": exp.text(BIO_PATIENT + "CLINIC_ID"),
        "name_family": family.raw if family else None,
        "name_given": given.raw if given else None,
        "birth_date": to_date(first("DOB", "BIRTH_DATE", "DATE_OF_BIRTH")),
        "sex": normalize_sex(sex.raw) if sex else None,
        "attributes": attributes,
    }


def read_device(exp: ParsedExport) -> dict:
    serial = exp.text("MDC/IDC/DEV/SERIAL")
    if not serial:
        raise ValueError("Seriale del dispositivo mancante (MDC/IDC/DEV/SERIAL)")
    implant = exp.get("MDC/IDC/DEV/IMPLANT_DT")
    return {
        "manufacturer": exp.text("MDC/IDC/DEV/MFG") or "UNKNOWN",
        "model": (exp.text("MDC/IDC/DEV/MODEL") or "").strip(),
        "serial": serial,
        "device_type": exp.text("MDC/IDC/DEV/TYPE"),
        "implant_dt": implant.dtm.date() if implant and implant.dtm else None,
    }


def read_session_time(exp: ParsedExport) -> tuple[datetime, tzinfo]:
    """Data/ora della sessione e fuso orario da usare per le date che non lo indicano."""
    v = exp.get("MDC/IDC/SESS/DTM")
    if v is None or v.dtm is None:
        raise ValueError("Data della sessione mancante (MDC/IDC/SESS/DTM)")
    tz = v.dtm.tzinfo if v.tz_known else timezone.utc
    return with_timezone(v, tz)[0], tz


# --- import ----------------------------------------------------------------
def load_export(conn, data: bytes, pseudo_key: str, vendor_export_id: str | None = None) -> dict:
    """Importa un export (byte del file XML). Restituisce un riepilogo con "status":
    "loaded" se importato, "duplicate" se la sessione era già nel database."""
    exp = parse_export(data)
    device = read_device(exp)
    session_dtm, tz = read_session_time(exp)
    patient = read_patient(exp)
    device_pseudo = device_pseudonym(pseudo_key, device["manufacturer"], device["model"], device["serial"])

    with conn.transaction(), conn.cursor() as cur:
        # Un import alla volta per lo stesso dispositivo: se lo stesso report arriva due
        # volte nello stesso momento, il secondo aspetta il primo e poi lo trova già salvato.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (device_pseudo,))

        existing = find_session(cur, device_pseudo, session_dtm)
        if existing:
            device_pk, session_pk, export_pk = existing
            n_notif = save_notifications(cur, device_pk, export_pk, exp, tz)
            return {"status": DUPLICATE, "device_pk": device_pk, "session_pk": session_pk,
                    "session_dtm": session_dtm.isoformat(), "notifications": n_notif}

        patient_no, patient_pk = find_or_create_patient(
            cur, pseudo_key, patient, device["manufacturer"], device["model"], device["serial"])
        device_pk = save_device(cur, pseudo_key, patient_no=patient_no, patient_pk=patient_pk,
                                hmsc_link=exp.text(BIO_PATIENT + "LINK"), **device)
        export_pk = save_export(cur, device_pk, exp, data, tz, vendor_export_id)
        session_pk = save_session(cur, device_pk, export_pk, exp, session_dtm, tz)
        n_obs = save_observations(cur, session_pk, exp, tz)
        n_notif = save_notifications(cur, device_pk, export_pk, exp, tz)

    return {"status": LOADED, "export_pk": export_pk, "session_pk": session_pk,
            "device_pk": device_pk, "device_pseudo": device_pseudo,
            "patient_pk": patient_pk, "patient_no": patient_no,
            "session_dtm": session_dtm.isoformat(), "observations": n_obs, "notifications": n_notif}


def find_session(cur, device_pseudo: str, session_dtm: datetime) -> tuple[int, int, int] | None:
    """(device_pk, session_pk, export_pk) se la sessione è già nel database (chiave naturale)."""
    cur.execute(
        """SELECT d.device_pk, s.session_pk, s.export_pk
             FROM cied.session s
             JOIN cied.device d USING (device_pk)
            WHERE d.device_pseudo = %s AND s.session_dtm = %s""",
        (device_pseudo, session_dtm))
    return cur.fetchone()


def save_export(cur, device_pk: int, exp: ParsedExport, data: bytes, tz: tzinfo,
                vendor_export_id: str | None) -> int:
    request_dtm, _ = with_timezone(exp.get("BIO/REQUEST/DATE"), tz)
    cur.execute(
        """INSERT INTO cied.export (source, vendor_export_id, vendor_request_id, request_dtm,
                                    format_version, creator_version, signed, device_pk)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING export_pk""",
        (SOURCE, vendor_export_id, exp.text("BIO/REQUEST/ID"), request_dtm,
         exp.format_version, exp.creator_version, exp.signed, device_pk))
    export_pk = cur.fetchone()[0]
    cur.execute("INSERT INTO vault.export_raw (export_pk, raw_xml) VALUES (%s, %s)",
                (export_pk, xml_text(data)))
    return export_pk


def save_session(cur, device_pk: int, export_pk: int, exp: ParsedExport,
                 session_dtm: datetime, tz: tzinfo) -> int:
    previous_dtm, _ = with_timezone(exp.get("MDC/IDC/SESS/DTM_PREVIOUS"), tz)
    offset_min = int(tz.utcoffset(None).total_seconds() // 60)
    cur.execute(
        """INSERT INTO cied.session (device_pk, export_pk, session_dtm, session_type, reprogrammed,
                                     previous_dtm, previous_type, clinic_name, utc_offset_min)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING session_pk""",
        (device_pk, export_pk, session_dtm, exp.text("MDC/IDC/SESS/TYPE"),
         exp.text("MDC/IDC/SESS/REPROGRAMMED"), previous_dtm, exp.text("MDC/IDC/SESS/TYPE_PREVIOUS"),
         exp.text("MDC/IDC/SESS/CLINIC_NAME"), offset_min))
    return cur.fetchone()[0]


def save_observations(cur, session_pk: int, exp: ParsedExport, tz: tzinfo) -> int:
    """Un valore per riga in cied.observation, con il codice IEEE e il periodo di riferimento."""
    groups: dict[str, dict[str, ExportValue]] = defaultdict(dict)
    for v in exp.values:
        groups[v.group][v.name] = v

    rows, terms = [], {}
    for v in exp.values:
        path = without_index(v.path)
        if is_identifying(path) or path in ALREADY_SAVED or path.startswith(NOTIFICATION + "/"):
            continue

        parts = path.split("/")
        branch, section = parts[0], "/".join(parts[1:-1])
        term = "_".join(parts)                     # es. MDC_IDC_STAT_AT_BURDEN_PERCENT
        terms.setdefault(term, (v.code, v.type, v.unit, branch == "MDC"))

        # periodo: <NOME>_DTM_START / _END se ci sono, altrimenti DTM_START / DTM_END della sezione
        g = groups[v.group]
        period_start, _ = with_timezone(g.get(f"{v.name}_DTM_START") or g.get("DTM_START"), tz)
        period_end, _ = with_timezone(g.get(f"{v.name}_DTM_END") or g.get("DTM_END"), tz)
        dtm, tz_assumed = with_timezone(v, tz)

        rows.append((session_pk, branch, section, v.group, v.name, term, v.code, v.enum_code,
                     v.type, v.unit, v.raw, v.num, dtm, tz_assumed, period_start, period_end))

    cur.executemany(
        """INSERT INTO cied.term (term, ieee_code, value_type, unit, is_standard)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (term) DO NOTHING""",
        [(term, *meta) for term, meta in terms.items()])
    cur.executemany(
        """INSERT INTO cied.observation (session_pk, branch, section, group_path, name, term,
               ieee_code, enum_code, value_type, unit, value_text, value_num, value_dtm,
               tz_assumed, period_start, period_end)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        rows)
    return len(rows)


def save_notifications(cur, device_pk: int, export_pk: int, exp: ParsedExport, tz: tzinfo) -> int:
    """Notifiche Home Monitoring: una riga per ID; si aggiorna solo con dati più recenti."""
    notifications: dict[str, dict[str, ExportValue]] = defaultdict(dict)
    for v in exp.values:
        if without_index(v.group) == NOTIFICATION:
            notifications[v.group][v.name] = v

    count = 0
    for n in notifications.values():
        if "ID" not in n:
            continue

        def text(name):
            return n[name].raw if name in n else None

        cur.execute(
            """INSERT INTO cied.notification (device_pk, vendor_id, dtm, update_dtm, universal_code,
                   vendor_code, priority, status, description_short, description_long, last_export_pk)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (vendor_id) DO UPDATE SET
                   update_dtm        = EXCLUDED.update_dtm,
                   status            = EXCLUDED.status,
                   priority          = EXCLUDED.priority,
                   description_short = EXCLUDED.description_short,
                   description_long  = EXCLUDED.description_long,
                   last_export_pk    = EXCLUDED.last_export_pk
               WHERE cied.notification.update_dtm IS NULL
                  OR EXCLUDED.update_dtm >= cied.notification.update_dtm""",
            (device_pk, text("ID"), with_timezone(n.get("DTM"), tz)[0],
             with_timezone(n.get("UPDATE_DTM"), tz)[0], text("VENDOR_UNIVERSAL_CODE"),
             text("VENDOR_CODE"), text("PRIORITY"), text("STATUS"),
             text("DESCRIPTION_SHORT"), text("DESCRIPTION_LONG"), export_pk))
        count += 1
    return count
