r"""Crea (o completa) schemi, tabelle, viste e cataloghi nel database.

Uso:
  python scripts\init_db.py            crea ciò che manca
  python scripts\init_db.py --reset    CANCELLA tutto e ricrea (solo in sviluppo!)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cied.db import connect, init_schema  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--reset", action="store_true", help="cancella gli schemi cied e vault prima di ricrearli")
args = ap.parse_args()

if args.reset and input("Cancellare TUTTI i dati di cied e vault? Scrivi SI per confermare: ") != "SI":
    sys.exit("Annullato.")

with connect() as conn:
    try:
        init_schema(conn, reset=args.reset)
    except RuntimeError as e:
        sys.exit(str(e))
    tables = conn.execute("""SELECT table_schema || '.' || table_name FROM information_schema.tables
                              WHERE table_schema IN ('cied', 'vault') ORDER BY 1""").fetchall()
print("Schema pronto:")
for (t,) in tables:
    print("  ", t)
