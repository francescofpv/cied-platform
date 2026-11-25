"""Identità di pazienti e dispositivi, divisa tra i due schemi.

  vault (identificativo)       cied (pseudonimizzato)
  ----------------------       ----------------------
  vault.patient                cied.patient   (solo sesso e anno di nascita)
  vault.patient_identifier
  vault.device_identity        cied.device    (senza seriale)

I due lati sono collegati dagli pseudonimi (HMAC con la chiave PSEUDO_KEY).
Tutte le funzioni ricevono un cursore: le transazioni le apre chi le chiama.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from cied.pseudo import device_pseudonym, patient_pseudonym

HMSC = "BIOTRONIK_HMSC"     # sistema degli ID paziente del Service Center


# --- pazienti ------------------------------------------------------------
def create_patient(cur, pseudo_key: str, demo: dict) -> tuple[int, str]:
    """Nuovo paziente nel vault. Restituisce (patient_no, patient_pseudo).

    demo = anagrafica: name_family, name_given, birth_date, sex, attributes (tutti facoltativi).
    """
    cur.execute("SELECT nextval(pg_get_serial_sequence('vault.patient', 'patient_no'))")
    patient_no = cur.fetchone()[0]
    pseudo = patient_pseudonym(pseudo_key, patient_no)
    cur.execute(
        """INSERT INTO vault.patient
               (patient_no, patient_pseudo, name_family, name_given, birth_date, sex, attributes)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (patient_no, pseudo, demo.get("name_family"), demo.get("name_given"),
         demo.get("birth_date"), demo.get("sex"), Jsonb(demo.get("attributes") or {})))
    return patient_no, pseudo


def update_patient(cur, patient_no: int, demo: dict) -> str:
    """Aggiorna l'anagrafica (i campi assenti restano com'erano). Restituisce lo pseudonimo."""
    cur.execute(
        """UPDATE vault.patient SET
               name_family = COALESCE(%s, name_family),
               name_given  = COALESCE(%s, name_given),
               birth_date  = COALESCE(%s, birth_date),
               sex         = COALESCE(%s, sex),
               attributes  = attributes || %s,
               updated_at  = now()
           WHERE patient_no = %s
           RETURNING patient_pseudo""",
        (demo.get("name_family"), demo.get("name_given"), demo.get("birth_date"), demo.get("sex"),
         Jsonb(demo.get("attributes") or {}), patient_no))
    return cur.fetchone()[0]


def add_identifier(cur, patient_no: int, system: str, namespace: str | None, value: str) -> None:
    """Collega un ID esterno (es. ID nel Service Center) al paziente."""
    cur.execute(
        """INSERT INTO vault.patient_identifier (system, namespace, value, patient_no)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (system, namespace, value) DO UPDATE SET last_seen = now()""",
        (system, namespace or "", value, patient_no))


def save_cied_patient(cur, pseudo: str, demo: dict) -> int:
    """Riga pseudonimizzata in cied.patient. Restituisce patient_pk."""
    birth = demo.get("birth_date")
    cur.execute(
        """INSERT INTO cied.patient (patient_pseudo, sex, birth_year) VALUES (%s, %s, %s)
           ON CONFLICT (patient_pseudo) DO UPDATE SET
               sex        = COALESCE(EXCLUDED.sex, cied.patient.sex),
               birth_year = COALESCE(EXCLUDED.birth_year, cied.patient.birth_year)
           RETURNING patient_pk""",
        (pseudo, demo.get("sex"), birth.year if birth else None))
    return cur.fetchone()[0]


def find_patient_by_identifier(cur, system: str, namespace: str, value: str) -> int | None:
    cur.execute("""SELECT patient_no FROM vault.patient_identifier
                    WHERE system = %s AND namespace = %s AND value = %s""", (system, namespace, value))
    row = cur.fetchone()
    return row[0] if row else None


def find_patient_by_device(cur, manufacturer: str, model: str, serial: str) -> int | None:
    cur.execute("""SELECT patient_no FROM vault.device_identity
                    WHERE manufacturer = %s AND model = %s AND serial_number = %s""",
                (manufacturer, model, serial))
    row = cur.fetchone()
    return row[0] if row else None


def find_or_create_patient(cur, pseudo_key: str, demo: dict | None,
                           manufacturer: str, model: str, serial: str) -> tuple[int | None, int | None]:
    """Paziente di un export. Restituisce (patient_no, patient_pk).

    1. ID del Service Center già noto            -> quel paziente
    2. altrimenti dispositivo già noto           -> il suo paziente (l'ID nuovo diventa un alias,
                                                    es. ID modificato nel Service Center)
    3. altrimenti                                -> nuovo paziente con un nuovo codice
    Senza anagrafica e con un dispositivo mai visto restituisce (None, None).
    """
    external_id = (HMSC, demo.get("clinic_id") or "", demo["vendor_patient_id"]) if demo else None

    patient_no = find_patient_by_identifier(cur, *external_id) if external_id else None
    if patient_no is None:
        patient_no = find_patient_by_device(cur, manufacturer, model, serial)

    if patient_no is not None:
        pseudo = update_patient(cur, patient_no, demo or {})
    elif demo:
        patient_no, pseudo = create_patient(cur, pseudo_key, demo)
    else:
        return None, None

    if external_id:
        add_identifier(cur, patient_no, *external_id)
    return patient_no, save_cied_patient(cur, pseudo, demo or {})


# --- dispositivi ---------------------------------------------------------
def save_device(cur, pseudo_key: str, *, patient_no: int | None, patient_pk: int | None,
                manufacturer: str, model: str, serial: str, device_type: str | None,
                implant_dt=None, hmsc_link: str | None = None, source: str = "BIOTRONIK_HM") -> int:
    """Crea o aggiorna il dispositivo (identità nel vault, dati in cied). Restituisce device_pk.

    Il dispositivo è identificato da produttore + modello + seriale.
    """
    pseudo = device_pseudonym(pseudo_key, manufacturer, model, serial)
    cur.execute(
        """INSERT INTO vault.device_identity
               (device_pseudo, patient_no, manufacturer, model, serial_number, hmsc_link)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (device_pseudo) DO UPDATE SET
               patient_no = COALESCE(EXCLUDED.patient_no, vault.device_identity.patient_no),
               hmsc_link  = COALESCE(EXCLUDED.hmsc_link, vault.device_identity.hmsc_link)""",
        (pseudo, patient_no, manufacturer, model, serial, hmsc_link))
    cur.execute(
        """INSERT INTO cied.device
               (device_pseudo, patient_pk, manufacturer, model, device_type, implant_dt, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (device_pseudo) DO UPDATE SET
               model       = EXCLUDED.model,
               device_type = COALESCE(EXCLUDED.device_type, cied.device.device_type),
               patient_pk  = COALESCE(EXCLUDED.patient_pk, cied.device.patient_pk),
               implant_dt  = COALESCE(EXCLUDED.implant_dt, cied.device.implant_dt)
           RETURNING device_pk""",
        (pseudo, patient_pk, manufacturer, model or None, device_type, implant_dt, source))
    return cur.fetchone()[0]
