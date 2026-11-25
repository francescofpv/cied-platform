"""Pazienti: ricerca, valori per i filtri, gruppi e terapia NAO.

Il paziente si indica con il suo codice CIED (vault.patient.patient_no), cioè
il numero progressivo che vede il medico.
"""
from __future__ import annotations

from collections.abc import Iterable

NAO_FILTERS = ("SI", "NO", "NON_INDICATO")


def patient_pk_of(conn, patient_no: int) -> int:
    row = conn.execute(
        """SELECT cp.patient_pk FROM vault.patient vp
             JOIN cied.patient cp ON cp.patient_pseudo = vp.patient_pseudo
            WHERE vp.patient_no = %s""", (patient_no,)).fetchone()
    if row is None:
        raise ValueError(f"Paziente {patient_no} inesistente")
    return row[0]


def patient_no_of(conn, patient_pk: int) -> int:
    row = conn.execute(
        """SELECT vp.patient_no FROM cied.patient cp
             JOIN vault.patient vp ON vp.patient_pseudo = cp.patient_pseudo
            WHERE cp.patient_pk = %s""", (patient_pk,)).fetchone()
    if row is None:
        raise ValueError(f"Paziente (pk {patient_pk}) inesistente")
    return row[0]


# --- ricerca ----------------------------------------------------------
def search(conn, query: str | None = None, *, manufacturer: str | None = None, model: str | None = None,
           group: int | str | None = None, nao: str | None = None,
           limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Cerca pazienti. Restituisce (righe della pagina, totale dei risultati).

    query: testo libero, ogni parola deve trovare l'inizio di una parola tra cognome,
           nome, codice paziente, anno di nascita, produttore, modello, seriale, ID esterni.
           Esempi: "rossi", "de luca 1956", "Biotronik III", "biomonitor iv 12345678".
    group: group_pk oppure nome del gruppo.   nao: 'SI', 'NO' o 'NON_INDICATO'.
    """
    if nao is not None and nao not in NAO_FILTERS:
        raise ValueError(f"nao deve essere uno tra {NAO_FILTERS}")
    group_pk = group_pk_of(conn, group) if isinstance(group, str) else group
    cur = conn.execute(
        "SELECT * FROM vault.search_patients(%s, %s, %s, %s, %s, %s, %s)",
        (query or None, manufacturer or None, model or None, group_pk, nao, limit, offset))
    cols = [c.name for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    total = rows[0]["total"] if rows else 0
    for r in rows:
        del r["total"]
    return rows, total


def search_options(conn) -> dict:
    """Valori per le tendine: produttori, modelli (con il loro produttore) e gruppi."""
    out = {"manufacturers": [], "models": [], "groups": []}
    for kind, value, label, parent, n in conn.execute("SELECT * FROM vault.search_options()").fetchall():
        if kind == "manufacturer":
            out["manufacturers"].append({"code": value, "name": label, "patients": n})
        elif kind == "model":
            out["models"].append({"manufacturer": parent, "model": value, "patients": n})
        else:
            out["groups"].append({"group_pk": int(value), "name": label, "patients": n})
    return out


# --- gruppi -----------------------------------------------------------
def create_group(conn, name: str, description: str | None = None) -> int:
    """Crea il gruppo (o restituisce quello esistente con lo stesso nome)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Il nome del gruppo è obbligatorio")
    row = conn.execute(
        """INSERT INTO cied.patient_group (name, description) VALUES (%s, %s)
           ON CONFLICT (name) DO UPDATE SET description = COALESCE(EXCLUDED.description, cied.patient_group.description)
           RETURNING group_pk""", (name, description)).fetchone()
    return row[0]


def group_pk_of(conn, group: int | str) -> int:
    if isinstance(group, int):
        return group
    row = conn.execute("SELECT group_pk FROM cied.patient_group WHERE name = %s", (group,)).fetchone()
    if row is None:
        raise ValueError(f"Gruppo '{group}' inesistente")
    return row[0]


def add_to_group(conn, group: int | str, patient_nos: Iterable[int]) -> int:
    """Aggiunge i pazienti al gruppo; restituisce quanti sono stati aggiunti (i già presenti si ignorano)."""
    gpk = group_pk_of(conn, group)
    added = 0
    with conn.transaction():
        for no in patient_nos:
            cur = conn.execute(
                """INSERT INTO cied.patient_group_member (group_pk, patient_pk) VALUES (%s, %s)
                   ON CONFLICT DO NOTHING""", (gpk, patient_pk_of(conn, no)))
            added += cur.rowcount
    return added


def remove_from_group(conn, group: int | str, patient_nos: Iterable[int]) -> int:
    gpk = group_pk_of(conn, group)
    removed = 0
    with conn.transaction():
        for no in patient_nos:
            cur = conn.execute("DELETE FROM cied.patient_group_member WHERE group_pk = %s AND patient_pk = %s",
                               (gpk, patient_pk_of(conn, no)))
            removed += cur.rowcount
    return removed


def delete_group(conn, group: int | str) -> None:
    conn.execute("DELETE FROM cied.patient_group WHERE group_pk = %s", (group_pk_of(conn, group),))


# --- terapia NAO ------------------------------------------------------
def set_nao_therapy(conn, patient_no: int, nao_therapy: bool) -> None:
    """Aggiorna lo stato attuale della terapia NAO (quello all'impianto resta sull'impianto)."""
    if not isinstance(nao_therapy, bool):
        raise ValueError("nao_therapy deve essere True o False")
    conn.execute("UPDATE cied.patient SET nao_therapy = %s, nao_updated_at = now() WHERE patient_pk = %s",
                 (nao_therapy, patient_pk_of(conn, patient_no)))
