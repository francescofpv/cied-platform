r"""Simulazione di una coorte di pazienti con loop recorder (dati SINTETICI).

Crea (o ricrea) il database separato `cied_simulazione`, genera N pazienti con
loop recorder Biotronik e li carica con il codice vero della piattaforma:
  - i report mensili di Home Monitoring passano da load_export (parser, vault/cied,
    pseudonimi, idempotenza), con batteria residua e burden di FA;
  - le schede cliniche passano da clinical.py (impianto con indicazione e patologia
    sospetta, diagnosi aggiunte con add_diagnoses, pacemaker/ICD, espianto).
Alla fine esporta le viste in CSV (export\simulazione\) con l'esportazione della
piattaforma: da lì le legge scripts\analizza_ilr.py.

I parametri sono ipotesi ispirate alla letteratura (vedi PARAMETRI) e NON sono
dati reali: servono a provare la piattaforma e il metodo di analisi.

Uso:
  python scripts\simula_ilr.py                 300 pazienti, seme 2026
  python scripts\simula_ilr.py --n 500 --seed 7
"""
import argparse
import math
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

from cied.clinical import (Condition, add_diagnoses, register_device, register_explant,  # noqa: E402
                           register_implant)
from cied.config import settings  # noqa: E402
from cied.db import connect, init_schema  # noqa: E402
from cied.db.loader import load_export  # noqa: E402
from cied.export import export_csv, exportable  # noqa: E402

DB_NAME = "cied_simulazione"
PSEUDO_KEY = "chiave-solo-per-la-simulazione"
CUTOFF = date(2026, 11, 20)                     # data di estrazione dei dati simulati

# ---------------------------------------------------------------------------
# PARAMETRI (ipotesi; fonti indicative nel README della simulazione)
#   sincope inspiegata: diagnosi in circa 1/3 dei pazienti, mediana ~3 mesi,
#     soprattutto bradiaritmie che portano al pacemaker in ~2 settimane;
#   ictus criptogenetico: FA nel 9% a 6 mesi, 12% a 12 mesi, 30% a 36 mesi (CRYSTAL-AF);
#   longevità dichiarata: BIOMONITOR III 4 anni, BIOMONITOR IIIm 5,5 anni.
# ---------------------------------------------------------------------------
INDICATIONS = {
    #             quota  codici indicazione        età media
    "sincope":     (0.50, [("R55", 1.0)],                    66),
    "ictus":       (0.30, [("I63", 0.8), ("G45", 0.2)],      68),
    "palpitazioni": (0.20, [("R00.2", 1.0)],                 58),
}
SUSPECTED = {          # patologia sospetta all'impianto
    "sincope":      [("I44.2", 0.45), ("I49.5", 0.35), ("I47.2", 0.10), ("I48", 0.10)],
    "ictus":        [("I48", 1.0)],
    "palpitazioni": [("I48", 0.70), ("I47.2", 0.30)],
}
DIAGNOSIS = {          # probabilità di diagnosi, tempo (lognormale: mediana in giorni, sigma), tipo
    "sincope":      (0.45, 120, 1.1, [("I44.2", 0.38), ("I49.5", 0.30), ("R00.1", 0.07),
                                      ("I47.2", 0.07), ("I48", 0.18)]),
    "ictus":        (0.36, 450, 0.9, [("I48", 1.0)]),
    "palpitazioni": (0.40, 180, 1.0, [("I48", 0.65), ("I47.2", 0.35)]),
}
THERAPY = {            # diagnosi -> (dispositivo terapeutico, mediana giorni all'impianto)
    "I44.2": ("PM", 10), "I49.5": ("PM", 20), "R00.1": ("PM", 30), "I47.2": ("ICD", 30),
}
MODELS = [("BIOMONITOR III", 4.0, 0.70), ("BIOMONITOR IIIm", 5.5, 0.30)]   # modello, anni, quota
PREVIOUS = [("I10", 0.55), ("E11", 0.20), ("I25", 0.10), ("N18", 0.08), ("I50", 0.05)]
ANNUAL_DEATH = 0.025


def pick(rng, items):
    r, acc = rng.random(), 0.0
    for value, p in items:
        acc += p
        if r < acc:
            return value
    return items[-1][0]


def lognormal_days(rng, median, sigma):
    return max(1, round(math.exp(rng.gauss(math.log(median), sigma))))


def xml(serial, model, implant, session, birth, sex, patient_id, battery, burden, n):
    status = "MOS" if battery > 10 else ("RRT" if battery > 0 else "EOS")
    at = (f'<section name="STAT"><section name="AT">'
          f'<value code="737665" name="DTM_START" type="DateTime">{(session - timedelta(days=30)):%Y%m%dT%H%M%S}</value>'
          f'<value code="737696" name="BURDEN_PERCENT" type="Numeric" unit="%">{burden:.2f}</value>'
          f'</section></section>')
    doc = f"""<?xml version="1.0" encoding="UTF-16"?>
<biotronik-ieee11073-export creator="SIMULAZIONE" creator-version="0" format-version="4.10"><dataset>
 <section name="MDC"><section name="IDC">
  <section name="DEV">
   <value code="720897" name="TYPE" type="MDC_IDC_ENUM_DEV_TYPE">Monitor</value>
   <value code="720898" name="MODEL" type="String">{model}</value>
   <value code="720899" name="SERIAL" type="String">{serial}</value>
   <value code="720900" name="MFG" type="MDC_IDC_ENUM_MFG">BIO</value>
   <value code="720901" name="IMPLANT_DT" type="DateTime">{implant:%Y%m%d}T000000</value>
  </section>
  <section name="SESS"><value code="721025" name="DTM" type="DateTime">{session:%Y%m%dT%H%M%S}+0100</value></section>
  <section name="BATTERY">
   <value name="STATUS" type="MDC_IDC_ENUM_BATTERY_STATUS">{status}</value>
   <value name="REMAINING_PERCENTAGE" type="Numeric" unit="%">{battery:.1f}</value>
  </section>
  {at}
 </section>
 <section name="ATTR"><section name="PT">
  <value code="67930" name="ID" type="String">{patient_id}</value>
  <value name="NAME_FAMILY" type="String">Simulato</value>
  <value name="NAME_GIVEN" type="String">Paziente {patient_id[-3:]}</value>
  <value name="SEX" type="String">{sex}</value>
  <value name="DOB" type="DateTime">{birth:%Y%m%d}T000000</value>
 </section></section></section>
 <section name="BIO"><section name="REQUEST"><value name="ID" type="String">SIM-{serial}-{n}</value></section></section>
</dataset></biotronik-ieee11073-export>"""
    return doc.encode("utf-16")


def simulate_patient(conn, rng, i):
    ind = pick(rng, [(k, v[0]) for k, v in INDICATIONS.items()])
    _, ind_codes, mean_age = INDICATIONS[ind]
    model, years, _ = pick(rng, [((m, y, q), q) for m, y, q in MODELS])
    implant = date(2019, 1, 1) + timedelta(days=rng.randrange((date(2025, 6, 30) - date(2019, 1, 1)).days))
    age = min(90, max(25, round(rng.gauss(mean_age, 11))))
    birth = date(implant.year - age, rng.randint(1, 12), rng.randint(1, 28))
    sex = rng.choice("12")
    serial, patient_id = f"SIM{i:05d}", f"sim-paz-{i:03d}"

    longevity = round(rng.gauss(years, 0.25) * 365.25)                 # giorni di batteria
    death = round(rng.expovariate(ANNUAL_DEATH) * 365.25)
    p_dx, median, sigma, types = DIAGNOSIS[ind]
    t_dx = lognormal_days(rng, median, sigma) if rng.random() < p_dx else None
    dx_code = pick(rng, types) if t_dx else None

    end = min(implant + timedelta(days=longevity), implant + timedelta(days=death), CUTOFF)
    dx_date = implant + timedelta(days=t_dx) if t_dx and implant + timedelta(days=t_dx) < end else None
    therapy = THERAPY.get(dx_code) if dx_date else None
    therapy_date = None
    if therapy:
        therapy_date = dx_date + timedelta(days=lognormal_days(rng, therapy[1], 0.7))
        if therapy_date < end:
            end = therapy_date                           # il loop recorder viene espiantato al nuovo impianto
        else:
            therapy = therapy_date = None                # il nuovo impianto avverrebbe dopo l'estrazione

    # report mensili di Home Monitoring (il primo il giorno dopo l'impianto)
    reports, day = 0, 1
    patient_pk = device_pk = None
    while implant + timedelta(days=day) <= end:
        session = datetime.combine(implant + timedelta(days=day), datetime.min.time()) + timedelta(hours=rng.randint(1, 5))
        battery = max(0.0, 100 * (1 - day / longevity) + rng.gauss(0, 0.8))
        af = dx_code == "I48" and dx_date and session.date() >= dx_date
        burden = round(min(60, rng.lognormvariate(math.log(3), 1.0)), 2) if af and rng.random() < 0.8 else 0.0
        r = load_export(conn, xml(serial, model, implant, session, birth, sex, patient_id,
                                  min(100, battery), burden, reports), PSEUDO_KEY)
        patient_pk, device_pk = r["patient_pk"], r["device_pk"]
        reports += 1
        day += 30
    if device_pk is None:
        return None

    previous = [c for c, p in PREVIOUS if rng.random() < p] or ["LOCAL:NONE"]
    ilr = register_implant(conn, device_pk, previous, [pick(rng, ind_codes)],
                           nao_therapy=(ind != "ictus" and rng.random() < 0.05),
                           sospette=[pick(rng, SUSPECTED[ind])])
    diagnosis = [Condition(dx_code, first_diagnosis_date=dx_date)] if dx_date else []

    if therapy:
        kind, _ = therapy
        dev = register_device(conn, PSEUDO_KEY, patient_pk, "BIO", f"SIM{kind}{i:05d}",
                              "Pacemaker (simulato)" if kind == "PM" else "ICD (simulato)", kind, therapy_date)
        new = register_implant(conn, dev, ["LOCAL:NONE"], [dx_code], nao_therapy=False)
        register_explant(conn, ilr, therapy_date, "DIAGNOSIS_REACHED", diagnosis,
                         switched_to_cied=True, next_implant_pk=new, ilr_decisive=True)
        return ind
    if diagnosis:                                         # es. FA: si registra la diagnosi, il monitoraggio continua
        add_diagnoses(conn, ilr, diagnosis)
    if end < CUTOFF:                                      # batteria esaurita o decesso prima dell'estrazione
        reason = "BATTERY_EOL" if end == implant + timedelta(days=longevity) else "DEATH"
        register_explant(conn, ilr, end, reason, [] if diagnosis else ["LOCAL:NONE"], switched_to_cied=False)
    return ind


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300, help="numero di pazienti (default 300)")
    ap.add_argument("--seed", type=int, default=2026, help="seme casuale, per risultati riproducibili")
    ap.add_argument("--out", default="export/simulazione", help="cartella dei CSV")
    args = ap.parse_args()

    admin = connect()
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DB_NAME)))
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
    admin.close()
    conn = connect(make_conninfo(settings.db_conninfo, dbname=DB_NAME))
    init_schema(conn)

    rng, start, loaded = random.Random(args.seed), time.time(), 0
    for i in range(1, args.n + 1):
        loaded += simulate_patient(conn, rng, i) is not None
        if i % 50 == 0:
            print(f"{i}/{args.n} pazienti simulati ({time.time() - start:.0f} s)")
    (sessions,) = conn.execute("SELECT count(*) FROM cied.session").fetchone()
    print(f"Caricati {loaded} pazienti e {sessions} report nel database '{DB_NAME}'.")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in [n for n in exportable(conn) if n.startswith("v_")]:
        rows = export_csv(conn, name, out / f"{name}.csv")
        print(f"  {name}: {rows} righe -> {out / f'{name}.csv'}")
    print("Analisi: python scripts\\analizza_ilr.py " + str(out))


if __name__ == "__main__":
    main()
