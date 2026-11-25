"""Inserimento della storia clinica: impianti, espianti, patologie.

Le regole di obbligatorietà sono nel database (vincoli CHECK e trigger
differiti), quindi valgono per qualunque interfaccia userà queste tabelle.
Queste funzioni rendono l'inserimento comodo e atomico: impianto e
patologie vanno nella stessa transazione, così i controlli al COMMIT passano.

Patologie: si indicano come "I48" (ICD-10-IM di default), "ICD-9-CM:427.31"
o "LOCAL:NONE" (nessuna patologia pregressa); per aggiungere una nota si usa
Condition(...). La data della prima diagnosi (Condition(..., first_diagnosis_date=...))
si indica solo per le patologie diagnosticate all'espianto.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from cied.db.identity import add_identifier, create_patient, save_cied_patient, save_device


@dataclass
class Condition:
    code: str
    coding_system: str = "ICD-10-IM"
    first_diagnosis_date: date | None = None
    note: str | None = None


def as_condition(c: str | Condition) -> Condition:
    if isinstance(c, Condition):
        return c
    if ":" in c:
        system, code = c.split(":", 1)
        return Condition(code=code, coding_system=system)
    return Condition(code=c)


def add_conditions(cur, implant_pk: int, role: str, conditions) -> None:
    for c in map(as_condition, conditions or []):
        if c.first_diagnosis_date is not None and role != "DIAGNOSTICATA":
            raise ValueError(f"Patologia {c.coding_system}:{c.code}: la data della prima diagnosi "
                             "si indica solo per le patologie diagnosticate")
        cur.execute("SELECT condition_pk FROM cied.condition WHERE coding_system = %s AND code = %s",
                    (c.coding_system, c.code))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Patologia {c.coding_system}:{c.code} non presente nel catalogo cied.condition")
        cur.execute(
            """INSERT INTO cied.implant_condition (implant_pk, condition_pk, role, first_diagnosis_date, note)
               VALUES (%s,%s,%s,%s,%s)""",
            (implant_pk, row[0], role, c.first_diagnosis_date, c.note),
        )


def add_condition_to_catalog(conn, code: str, label: str, coding_system: str = "ICD-10-IM") -> int:
    row = conn.execute(
        """INSERT INTO cied.condition (coding_system, code, label) VALUES (%s,%s,%s)
           ON CONFLICT (coding_system, code) DO UPDATE SET label = EXCLUDED.label
           RETURNING condition_pk""",
        (coding_system, code, label),
    ).fetchone()
    return row[0]


def register_patient(conn, pseudo_key: str, name_family: str | None = None, name_given: str | None = None,
                     birth_date: date | None = None, sex: str | None = None,
                     identifiers: list[tuple[str, str, str]] | None = None) -> int:
    """Paziente inserito a mano (es. con un dispositivo di un altro produttore). Restituisce patient_pk.

    identifiers: ID esterni facoltativi, es. [("MEDTRONIC_CARELINK", "clinica", "ID-123")].
    """
    demo = {"name_family": name_family, "name_given": name_given, "birth_date": birth_date, "sex": sex}
    with conn.transaction(), conn.cursor() as cur:
        patient_no, pseudo = create_patient(cur, pseudo_key, demo)
        for system, namespace, value in identifiers or []:
            add_identifier(cur, patient_no, system, namespace, value)
        return save_cied_patient(cur, pseudo, demo)


def register_device(conn, pseudo_key: str, patient_pk: int, manufacturer: str, serial: str,
                    model: str | None, device_type: str, implant_dt: date | None = None) -> int:
    """Dispositivo inserito a mano (non arrivato da Home Monitoring). Restituisce device_pk.

    device_type secondo MDC_IDC_ENUM_DEV_TYPE: PM, ICD, CRT_P, CRT_D, Monitor (loop recorder).
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""SELECT vp.patient_no FROM cied.patient cp
                         JOIN vault.patient vp ON vp.patient_pseudo = cp.patient_pseudo
                        WHERE cp.patient_pk = %s""", (patient_pk,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Paziente {patient_pk} inesistente")
        return save_device(cur, pseudo_key, patient_no=row[0], patient_pk=patient_pk,
                           manufacturer=manufacturer, model=(model or "").strip(), serial=serial,
                           device_type=device_type, implant_dt=implant_dt, source="MANUAL")


def register_implant(conn, device_pk: int, pregresse, indicazioni, implant_date: date | None = None,
                     patient_pk: int | None = None, notes: str | None = None, *,
                     nao_therapy: bool | None = None, sospette=None) -> int:
    """Crea la scheda di impianto.

    Obbligatori: patologie pregresse, motivazione (indicazioni) e nao_therapy
    (True/False: il paziente è in terapia anticoagulante NAO?). Il valore viene
    salvato sull'impianto (stato al momento dell'impianto) e come stato attuale
    del paziente.
    sospette: facoltativo, solo per i loop recorder: patologie sospette che il
    loop recorder deve confermare o escludere (es. ["I48"] = sospetta FA).
    implant_date e patient_pk, se omessi, si prendono dal dispositivo (dati Home Monitoring).
    """
    if not isinstance(nao_therapy, bool):
        raise ValueError("Indicare se il paziente è in terapia NAO: nao_therapy=True oppure nao_therapy=False")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT patient_pk, implant_dt FROM cied.device WHERE device_pk = %s", (device_pk,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Dispositivo {device_pk} inesistente")
        patient_pk = patient_pk or row[0]
        implant_date = implant_date or row[1]
        if patient_pk is None or implant_date is None:
            raise ValueError("Paziente e data di impianto sono obbligatori")
        cur.execute(
            """INSERT INTO cied.implant (patient_pk, device_pk, implant_date, notes, nao_at_implant)
               VALUES (%s,%s,%s,%s,%s) RETURNING implant_pk""",
            (patient_pk, device_pk, implant_date, notes, nao_therapy),
        )
        implant_pk = cur.fetchone()[0]
        cur.execute("UPDATE cied.patient SET nao_therapy = %s, nao_updated_at = now() WHERE patient_pk = %s",
                    (nao_therapy, patient_pk))
        add_conditions(cur, implant_pk, "PREGRESSA", pregresse)
        add_conditions(cur, implant_pk, "INDICAZIONE", indicazioni)
        add_conditions(cur, implant_pk, "SOSPETTA", sospette)
        return implant_pk


def register_explant(conn, implant_pk: int, explant_date: date, reason_code: str, diagnosticate,
                     switched_to_cied: bool, next_implant_pk: int | None = None,
                     ilr_decisive: bool | None = None, reason_note: str | None = None) -> None:
    """Registra l'espianto. Obbligatori: motivazione, patologie diagnosticate,
    passaggio ad altro CIED (sì/no) e, per un loop recorder seguito da un CIED,
    se il loop recorder è stato determinante.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """UPDATE cied.implant SET explant_date = %s, explant_reason_code = %s, explant_reason_note = %s,
                      switched_to_cied = %s, next_implant_pk = %s, ilr_decisive = %s
               WHERE implant_pk = %s""",
            (explant_date, reason_code, reason_note, switched_to_cied, next_implant_pk, ilr_decisive, implant_pk),
        )
        if cur.rowcount == 0:
            raise ValueError(f"Impianto {implant_pk} inesistente")
        add_conditions(cur, implant_pk, "DIAGNOSTICATA", diagnosticate)


def add_diagnoses(conn, implant_pk: int, diagnosticate) -> None:
    """Aggiunge patologie DIAGNOSTICATA a un impianto in qualsiasi momento,
    per qualsiasi dispositivo: mentre è ancora impiantato (es. FA rilevata da un
    pacemaker) o dopo l'espianto. Per ognuna si può indicare la data della prima
    diagnosi: Condition("I48", first_diagnosis_date=date(2026, 11, 20)).
    """
    conditions = [as_condition(c) for c in diagnosticate or []]
    if not conditions:
        raise ValueError("Indicare almeno una patologia diagnosticata")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT 1 FROM cied.implant WHERE implant_pk = %s", (implant_pk,))
        if cur.fetchone() is None:
            raise ValueError(f"Impianto {implant_pk} inesistente")
        for c in conditions:
            cur.execute("""SELECT 1 FROM cied.implant_condition ic JOIN cied.condition k USING (condition_pk)
                            WHERE ic.implant_pk = %s AND ic.role = 'DIAGNOSTICATA'
                              AND k.coding_system = %s AND k.code = %s""",
                        (implant_pk, c.coding_system, c.code))
            if cur.fetchone() is not None:
                raise ValueError(f"Patologia {c.coding_system}:{c.code} già registrata come diagnosticata "
                                 f"per l'impianto {implant_pk}")
        add_conditions(cur, implant_pk, "DIAGNOSTICATA", conditions)


def link_next_implant(conn, implant_pk: int, next_implant_pk: int) -> None:
    """Collega a posteriori il CIED successivo (se registrato dopo l'espianto)."""
    with conn.transaction():
        conn.execute("UPDATE cied.implant SET next_implant_pk = %s WHERE implant_pk = %s",
                     (next_implant_pk, implant_pk))
