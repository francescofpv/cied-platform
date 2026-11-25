"""Avvia il server FINTO di BIOTRONIK Home Monitoring sul tuo PC, per provare il client.

Uso (dalla cartella del progetto, con .venv attivo):
  python scripts/mock_hm_server.py                       serve gli XML di data/samples
  python scripts/mock_hm_server.py --folder altra\\cartella --port 8443

Al primo avvio crea:
  data/mock_pki/   certificati di prova (CA, server, client, client.p12 con password "test")
  .env.mock        impostazioni per collegarsi al server finto

Poi, in un SECONDO terminale:
  python scripts/sync_hm.py --env .env.mock --check
  python scripts/sync_hm.py --env .env.mock

Il server resta acceso finché non premi Ctrl+C. I dati restano in memoria:
a ogni riavvio gli export tornano tutti "in coda".
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cied.hm_mock import MockHMServer, make_test_pki  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--folder", default=str(ROOT / "data" / "samples"), help="cartella con gli XML da servire")
ap.add_argument("--port", type=int, default=8443)
ap.add_argument("--max-per-min", type=int, default=5, help="limite di richieste al minuto (come l'API reale)")
args = ap.parse_args()

pki_dir = ROOT / "data" / "mock_pki"
names = ("ca.pem", "server.pem", "server.key", "client.pem", "client.key", "client.p12")
keys = ("ca", "server_cert", "server_key", "client_cert", "client_key", "client_p12")
if all((pki_dir / n).is_file() for n in names):
    pki = {k: pki_dir / n for k, n in zip(keys, names)}
else:
    pki = make_test_pki(pki_dir)
    print(f"Certificati di prova creati in {pki_dir}")

CLIENT_ID, SECRET = "mock-client", "mock-secret"
env_mock = ROOT / ".env.mock"
env_mock.write_text(
    "# Impostazioni per il server FINTO (scripts/mock_hm_server.py). Non usare con l'API reale.\n"
    f"HM_BASE_URL=https://127.0.0.1:{args.port}/rest/api\n"
    f"HM_CLIENT_ID={CLIENT_ID}\nHM_CLIENT_SECRET={SECRET}\n"
    f"HM_CERT_PEM={pki['client_cert']}\nHM_KEY_PEM={pki['client_key']}\nHM_CA_BUNDLE={pki['ca']}\n",
    encoding="utf-8")

srv = MockHMServer(pki, port=args.port, client_id=CLIENT_ID, client_secret=SECRET,
                   max_per_min=args.max_per_min, quiet=False)
exports = srv.add_folder(args.folder)
print(f"Server finto in ascolto su {srv.base_url}")
print(f"Export in coda: {len(exports)} (da {args.folder})")
for e in exports:
    print(f"  {e.id}  report del {e.report_dtm:%Y-%m-%d %H:%M} UTC")
print(f"Impostazioni scritte in {env_mock.name}. Nel secondo terminale:")
print("  python scripts\\sync_hm.py --env .env.mock --check")
print("Ctrl+C per fermare.")
try:
    srv.serve_forever()
except KeyboardInterrupt:
    print("\nServer fermato.")
    srv.server_close()
