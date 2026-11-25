"""Prova di carico della ricerca pazienti su dati SINTETICI.

Crea un database temporaneo, lo riempie con N pazienti inventati (cognomi italiani
comuni, 1-2 dispositivi ciascuno, gruppi, NAO), misura i tempi delle ricerche e
poi cancella il database. Non tocca il database "cied".

Uso:
  python scripts/benchmark_search.py               50.000 pazienti
  python scripts/benchmark_search.py --patients 100000 --repeat 30
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from psycopg.conninfo import make_conninfo  # noqa: E402

from cied import patients as P  # noqa: E402
from cied.config import settings  # noqa: E402
from cied.db import connect, init_schema  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--patients", type=int, default=50_000)
ap.add_argument("--repeat", type=int, default=20)
args = ap.parse_args()

SURNAMES = ("Rossi Russo Ferrari Esposito Bianchi Romano Colombo Ricci Marino Greco Bruno Gallo Conti "
            "De_Luca Mancini Costa Giordano Rizzo Lombardi Moretti Barbieri Fontana Santoro Mariani "
            "Rinaldi Caruso Ferrara Galli Martini Leone Longo Gentile Martinelli Vitale Lombardo Serra "
            "Coppola De_Santis D'Angelo Marchetti Parisi Villa Conte Ferraro Ferri Fabbri Bianco Marini "
            "Grasso Valentini Messina Sala De_Angelis Gatti Pellegrini Palumbo Sanna Farina Rizzi "
            "Monti Cattaneo Morelli Amato Silvestri Mazza Testa Grassi Pellegrino Carbone Giuliani "
            "Benedetti Barone Rossetti Caputo Montanari Guerra Palmieri Bernardi Martino Fiore De_Rosa "
            "Ferretti Bellini Basile Riva Donati Piras Vitali Battaglia Sartori Neri Costantini Milani "
            "Pagano Ruggiero Sorrentino D'Amico Orlando Damico Negri").replace("_", " ").split()
GIVEN = "Maria Giuseppe Anna Antonio Giovanni Rosa Francesco Angela Mario Lucia Luigi Teresa".split()
MODELS = [("BIO", "BIOMONITOR III"), ("BIO", "BIOMONITOR IV"), ("BIO", "Edora 8 DR-T"), ("BIO", "Acticor 7 HF-T QP"),
          ("MDT", "LINQ II"), ("MDT", "Azure XT DR"), ("BSX", "LUX-Dx"), ("STJ", "Confirm Rx")]

name = "cied_benchmark"
admin = connect()
admin.execute(f"DROP DATABASE IF EXISTS {name}")
admin.execute(f"CREATE DATABASE {name}")
try:
    with connect(make_conninfo(settings.db_conninfo, dbname=name)) as c:
        init_schema(c)
        t0 = time.perf_counter()
        c.execute("ALTER TABLE vault.patient DISABLE TRIGGER trg_patient_search")
        c.execute("ALTER TABLE vault.device_identity DISABLE TRIGGER trg_device_search")
        c.execute(
            """INSERT INTO vault.patient (patient_no, patient_pseudo, name_family, name_given, birth_date, sex)
               SELECT 100000 + g, 'ps' || g, (%(s)s::text[])[1 + (g * 7919) %% array_length(%(s)s::text[], 1)],
                      (%(n)s::text[])[1 + (g * 104729) %% array_length(%(n)s::text[], 1)],
                      date '1930-01-01' + ((g * 37) %% 25000)::int, CASE WHEN g %% 2 = 0 THEN 'F' ELSE 'M' END
                 FROM generate_series(1::bigint, %(N)s) g""", {"s": list(SURNAMES), "n": GIVEN, "N": args.patients})
        c.execute("""INSERT INTO cied.patient (patient_pseudo, sex, birth_year, nao_therapy)
                     SELECT patient_pseudo, sex, extract(year FROM birth_date),
                            CASE WHEN patient_no % 3 = 0 THEN NULL ELSE patient_no % 3 = 1 END
                       FROM vault.patient""")
        c.execute(
            """INSERT INTO vault.device_identity (device_pseudo, patient_no, manufacturer, model, serial_number)
               SELECT 'd' || p.patient_no || '-' || k, p.patient_no,
                      (%(m)s::text[])[1 + ((p.patient_no + k) %% %(L)s)][1], (%(m)s::text[])[1 + ((p.patient_no + k) %% %(L)s)][2],
                      lpad(((p.patient_no::bigint * 7919 + k * 13) %% 100000000)::text, 8, '0')
                 FROM vault.patient p, generate_series(1, 1 + (p.patient_no %% 2)) k""",
            {"m": [list(m) for m in MODELS], "L": len(MODELS)})
        c.execute("ALTER TABLE vault.patient ENABLE TRIGGER trg_patient_search")
        c.execute("ALTER TABLE vault.device_identity ENABLE TRIGGER trg_device_search")
        c.execute("INSERT INTO cied.manufacturer VALUES ('MDT','Medtronic'),('BSX','Boston Scientific'),"
                  "('STJ','Abbott') ON CONFLICT DO NOTHING")
        c.execute("""UPDATE vault.patient SET search_text =
                     vault.build_search_text(patient_no, name_family, name_given, birth_date)""")
        g = P.create_group(c, "Pazienti Dott. X")
        c.execute("""INSERT INTO cied.patient_group_member (group_pk, patient_pk)
                     SELECT %s, patient_pk FROM cied.patient WHERE patient_pk %% 10 = 0""", (g,))
        c.execute("VACUUM ANALYZE")
        n_dev = c.execute("SELECT count(*) FROM vault.device_identity").fetchone()[0]
        print(f"Dati sintetici: {args.patients:,} pazienti, {n_dev:,} dispositivi "
              f"(creati in {time.perf_counter() - t0:.1f} s)\n")

        serial = c.execute("SELECT serial_number FROM vault.device_identity LIMIT 1 OFFSET 777").fetchone()[0]
        cases = [
            ("cognome frequente", dict(query="rossi")),
            ("cognome + nome", dict(query="rossi maria")),
            ("cognome con apostrofo", dict(query="d'amico")),
            ("parte del cognome", dict(query="ross")),
            ("cognome + anno", dict(query="esposito 1950")),
            ("produttore + modello", dict(query="Biotronik III")),
            ("modello + seriale", dict(query=f"biomonitor iv {serial}")),
            ("seriale parziale", dict(query=serial[:5])),
            ("codice paziente", dict(query="123456")),
            ("solo tendine produttore+modello", dict(manufacturer="BIO", model="BIOMONITOR III")),
            ("testo + tendine + gruppo + NAO", dict(query="rossi", manufacturer="BIO", group=g, nao="SI")),
            ("tendine: elenco opzioni", None),
        ]
        print(f"{'ricerca':38} {'risultati':>10} {'mediana':>9} {'peggiore':>9}")
        for label, kw in cases:
            times = []
            for _ in range(args.repeat):
                t = time.perf_counter()
                if kw is None:
                    P.search_options(c)
                    total = "-"
                else:
                    _, total = P.search(c, **kw)
                times.append((time.perf_counter() - t) * 1000)
            print(f"{label:38} {total!s:>10} {statistics.median(times):7.1f}ms {max(times):7.1f}ms")
finally:
    admin.execute(f"DROP DATABASE IF EXISTS {name}")
    admin.close()
