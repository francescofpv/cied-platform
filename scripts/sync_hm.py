"""Scarica i report da BIOTRONIK Home Monitoring e li importa nel database.

Uso (dalla cartella del progetto, con .venv attivo):
  python scripts/sync_hm.py --check            prova la connessione: mostra quanti export sono in coda
  python scripts/sync_hm.py                    scarica e importa gli export in coda, poi li segna come processati
  python scripts/sync_hm.py --keep             come sopra ma NON li segna come processati
  python scripts/sync_hm.py --on-demand        chiede l'ultimo report di ogni dispositivo già noto
  python scripts/sync_hm.py --on-demand --serial 12345678
  python scripts/sync_hm.py --env .env.mock    usa le impostazioni del server finto

Credenziali (nel file .env): HM_CLIENT_ID, HM_CLIENT_SECRET, HM_CERT_PEM, HM_KEY_PEM.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--check", action="store_true", help="solo test di connessione (nessun import)")
ap.add_argument("--on-demand", action="store_true", help="modalità export on demand per i dispositivi noti")
ap.add_argument("--serial", action="append", help="seriale del dispositivo (ripetibile, con --on-demand)")
ap.add_argument("--keep", action="store_true", help="non segnare gli export come processati")
ap.add_argument("--env", help="file .env alternativo (es. .env.mock per il server finto)")
args = ap.parse_args()

if args.env:  # va caricato PRIMA di importare cied.config
    from dotenv import load_dotenv
    env_path = Path(args.env) if Path(args.env).is_absolute() else ROOT / args.env
    if not env_path.is_file():
        sys.exit(f"File non trovato: {env_path}")
    load_dotenv(env_path, override=True)

import requests  # noqa: E402

from cied.config import settings  # noqa: E402
from cied.hm_client import HMClient, HMError  # noqa: E402

try:
    client = HMClient.from_settings(settings)
except (ValueError, FileNotFoundError) as e:
    sys.exit(f"Configurazione incompleta: {e}")

print(f"Server: {settings.hm_base_url}")
try:
    if args.check:
        jobs = client.list_exports()
        print(f"Connessione OK. Export in coda: {len(jobs)}")
        for j in jobs:
            print(f"  {j.id}  creato {j.created_at:%Y-%m-%d %H:%M} UTC" if j.created_at else f"  {j.id}")
        sys.exit(0)

    from cied.db import connect  # noqa: E402
    from cied.sync import sync_jobs, sync_on_demand  # noqa: E402

    with connect() as conn:
        if args.on_demand:
            res = sync_on_demand(client, conn, settings.pseudo_key, args.serial)
        else:
            res = sync_jobs(client, conn, settings.pseudo_key, acknowledge=not args.keep)
except requests.exceptions.SSLError as e:
    sys.exit("Errore TLS: certificato client mancante o rifiutato, oppure certificato del server "
             f"non riconosciuto (HM_CA_BUNDLE serve solo con il server finto).\nDettaglio: {e}")
except requests.exceptions.ConnectionError as e:
    sys.exit(f"Server non raggiungibile ({settings.hm_base_url}). Il server finto è avviato?\nDettaglio: {e}")
except HMError as e:
    sys.exit(f"Errore dal web service: {e}")

print("Riepilogo:", res.as_dict(), f"| richieste HTTP: {client.requests_made}")
sys.exit(1 if res.errors else 0)
