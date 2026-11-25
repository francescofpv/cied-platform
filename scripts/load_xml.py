r"""Importa uno o più export XML nel database.

Uso:  python scripts\load_xml.py "data\samples\*.xml"

Per ogni file stampa "caricato correttamente" oppure "sessione già presente
(duplicato)": rilanciare lo stesso import non crea doppioni.
"""
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cied.config import settings  # noqa: E402
from cied.db import connect  # noqa: E402
from cied.db.loader import LOADED, MESSAGES, load_export  # noqa: E402

files = [f for pattern in sys.argv[1:] for f in glob.glob(pattern)]   # glob anche su Windows
if not files:
    sys.exit(r'Nessun file trovato. Uso: python scripts\load_xml.py "data\samples\*.xml"')

with connect() as conn:
    for f in files:
        name = Path(f).name
        try:
            r = load_export(conn, Path(f).read_bytes(), settings.pseudo_key)
        except Exception as e:            # un file errato non blocca gli altri
            print(f"{name}: ERRORE {e}")
            continue
        detail = (f" ({r['observations']} valori, {r['notifications']} notifiche)"
                  if r["status"] == LOADED else "")
        print(f"{name}: {MESSAGES[r['status']]}{detail}")
