"""Connessione al database e creazione dello schema."""
from pathlib import Path

import psycopg

from cied.config import settings

SQL_DIR = Path(__file__).resolve().parents[3] / "sql"


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """Connessione in autocommit: le transazioni si aprono con `with conn.transaction():`."""
    return psycopg.connect(conninfo or settings.db_conninfo, autocommit=True)


def column_exists(conn, schema: str, table: str, column: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = %s""",
        (schema, table, column)).fetchone()
    return row is not None


def table_exists(conn, schema: str, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",)).fetchone()
    return row[0] is not None


def check_old_database(conn) -> None:
    """Ferma l'avvio se il database è stato creato con una versione precedente."""
    if table_exists(conn, "vault", "patient") and not column_exists(conn, "vault", "patient", "patient_no"):
        raise RuntimeError("Il database è di una versione precedente (senza codice paziente). "
                           "Esegui: python scripts/init_db.py --reset  e poi reimporta gli XML.")
    if table_exists(conn, "cied", "export") and column_exists(conn, "cied", "export", "sha256"):
        raise RuntimeError("Il database è di una versione precedente (impronta SHA-256). "
                           "Esegui: python scripts/init_db.py --reset  e poi reimporta gli XML.")


def init_schema(conn, reset: bool = False) -> None:
    """Crea schemi, tabelle, viste, ruoli e cataloghi.

    reset=True cancella prima gli schemi cied e vault (TUTTI i dati).
    """
    if reset:
        conn.execute("DROP SCHEMA IF EXISTS cied CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS vault CASCADE")
    else:
        check_old_database(conn)
    for name in ("schema.sql", "seed.sql"):
        conn.execute((SQL_DIR / name).read_text(encoding="utf-8"))
