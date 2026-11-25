"""Fixture comune: un database temporaneo creato e distrutto a ogni esecuzione dei test.

Usa le credenziali del file .env; l'utente deve poter creare database
(l'utente "postgres" può). Se PostgreSQL non è raggiungibile i test DB
vengono saltati.
"""
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import make_conninfo  # noqa: E402

from cied.config import settings  # noqa: E402
from cied.db import connect, init_schema  # noqa: E402

PSEUDO_KEY = "chiave-solo-per-i-test"


@pytest.fixture(scope="session")
def conn():
    try:
        admin = connect()
    except Exception:
        pytest.skip("PostgreSQL non raggiungibile")
    name = f"cied_test_{secrets.token_hex(4)}"
    try:
        admin.execute(f"CREATE DATABASE {name}")
    except psycopg.Error:
        admin.close()
        pytest.skip("l'utente del DB non può creare database di test")
    c = connect(make_conninfo(settings.db_conninfo, dbname=name))
    init_schema(c)
    yield c
    c.close()
    admin.execute(f"DROP DATABASE {name}")
    admin.close()
