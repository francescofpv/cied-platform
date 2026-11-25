r"""Analisi dei loop recorder a partire dai CSV esportati dalla piattaforma.

Legge v_ilr_outcome.csv, v_implant.csv e v_measurement.csv da una cartella
(esportazione standard, separatore ","), quindi funziona sia sui dati della
simulazione (export\simulazione) sia su un'esportazione reale.

Calcola:
  - resa diagnostica grezza e cumulativa (Kaplan-Meier) a 6/12/24/36 mesi, per indicazione;
  - tempo alla diagnosi (mediana e quartili) e tipo di diagnosi;
  - patologia sospetta confermata (codice esatto e stessa famiglia clinica);
  - passaggio a pacemaker/ICD e tempi diagnosi -> terapia;
  - durata della batteria: osservata agli espianti per esaurimento e stimata dal calo
    della carica residua nei report;
  - burden di FA nei pazienti con FA diagnosticata.

Uso:
  python scripts\analizza_ilr.py export\simulazione            stampa i risultati
  python scripts\analizza_ilr.py export\simulazione --json r.json  e li salva anche in JSON
"""
import argparse
import csv
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

INDICATION_LABEL = {"R55": "Sincope inspiegata", "I63": "Ictus criptogenetico / TIA",
                    "G45": "Ictus criptogenetico / TIA", "R00.2": "Palpitazioni"}
FAMILY = {"I44.2": "bradiaritmia", "I44.1": "bradiaritmia", "I49.5": "bradiaritmia", "R00.1": "bradiaritmia",
          "I47.2": "tachiaritmia ventricolare", "I49.0": "tachiaritmia ventricolare",
          "I48": "fibrillazione atriale"}
NOMINAL_YEARS = {"BIOMONITOR III": 4.0, "BIOMONITOR IIIm": 5.5}
BATTERY = "MDC_IDC_BATTERY_REMAINING_PERCENTAGE"
AF_BURDEN = "MDC_IDC_STAT_AT_BURDEN_PERCENT"
MILESTONES = {"6 mesi": 182, "12 mesi": 365, "24 mesi": 730, "36 mesi": 1095}


def read(folder: Path, name: str) -> list[dict]:
    with open(folder / f"{name}.csv", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def codes(text: str) -> list[str]:
    """'I44.2 Blocco AV; I48 Fibrillazione' -> ['I44.2', 'I48']"""
    return [part.strip().split(" ")[0] for part in text.split(";") if part.strip()] if text else []


def num(v):
    return float(v) if v not in ("", None) else None


def quartiles(values):
    if len(values) < 2:
        return {"n": len(values), "mediana": values[0] if values else None, "q1": None, "q3": None}
    q1, med, q3 = st.quantiles(values, n=4, method="inclusive")
    return {"n": len(values), "mediana": round(med), "q1": round(q1), "q3": round(q3)}


def kaplan_meier(times_events, horizon_days):
    """Incidenza cumulativa (1 - S(t)) con censura; restituisce la funzione t -> %."""
    times = sorted({t for t, e in times_events if e})
    surv, curve = 1.0, [(0, 0.0)]
    for t in times:
        at_risk = sum(1 for x, _ in times_events if x >= t)
        events = sum(1 for x, e in times_events if e and x == t)
        surv *= 1 - events / at_risk
        curve.append((t, 100 * (1 - surv)))

    def at(day):
        value = 0.0
        for t, v in curve:
            if t <= day:
                value = v
        return round(value, 1)
    return at, {"giorni": list(range(0, horizon_days + 1, 30)),
                "percentuale": [at(d) for d in range(0, horizon_days + 1, 30)]}


def analyse(folder: Path) -> dict:
    outcome = read(folder, "v_ilr_outcome")
    implants = {r["implant_pk"]: r for r in read(folder, "v_implant")}
    measures = read(folder, "v_measurement")

    rows = []
    for o in outcome:
        imp = implants[o["ilr_implant_pk"]]
        ind_codes = codes(imp["indicazioni"])
        rows.append({
            "indicazione": INDICATION_LABEL.get(ind_codes[0], ind_codes[0]) if ind_codes else "non indicata",
            "modello": o["ilr_model"], "eta": num(o["age_at_implant"]),
            "giorni_monitoraggio": num(o["days_monitored"]), "giorni_diagnosi": num(o["days_to_diagnosis"]),
            "giorni_terapia": num(o["days_to_therapy"]), "terapia": o["next_device_type"] or None,
            "motivo_espianto": o["explant_reason"] or "ancora impiantato",
            "sospette": codes(imp["sospette"]),
            "diagnosi": [c for c in codes(imp["diagnosticate"]) if c != "NONE" and not c.startswith("LOCAL")],
            "paziente": o["patient_pseudo"],
        })
    groups = defaultdict(list)
    for r in rows:
        groups[r["indicazione"]].append(r)
    groups = {"Tutti": rows, **dict(sorted(groups.items()))}

    res = {"coorte": {}, "resa": {}, "curve": {}, "tempo_diagnosi": {}, "sospetto": {}, "terapia": {},
           "tipo_diagnosi": {}, "batteria": {}, "burden_fa": {}, "espianti": {}}

    # --- coorte -----------------------------------------------------------
    res["coorte"] = {
        "pazienti": len(rows),
        "per_indicazione": {k: len(v) for k, v in groups.items() if k != "Tutti"},
        "per_modello": dict(Counter(r["modello"] for r in rows)),
        "eta_media": round(st.mean(r["eta"] for r in rows if r["eta"] is not None), 1),
        "follow_up_giorni": quartiles([r["giorni_monitoraggio"] for r in rows]),
        "ancora_impiantati": sum(r["motivo_espianto"] == "ancora impiantato" for r in rows),
        "report": sum(1 for m in measures if m["term"] == BATTERY),
    }

    # --- resa diagnostica e tempi -----------------------------------------
    for name, g in groups.items():
        diagnosed = [r for r in g if r["giorni_diagnosi"] is not None]
        at, curve = kaplan_meier([(r["giorni_diagnosi"] if r["giorni_diagnosi"] is not None
                                   else r["giorni_monitoraggio"], r["giorni_diagnosi"] is not None) for r in g], 1800)
        res["resa"][name] = {"pazienti": len(g), "con_diagnosi": len(diagnosed),
                             "grezza_%": round(100 * len(diagnosed) / len(g), 1),
                             **{f"cumulativa_{k}_%": at(d) for k, d in MILESTONES.items()}}
        res["curve"][name] = curve
        res["tempo_diagnosi"][name] = quartiles([r["giorni_diagnosi"] for r in diagnosed])

        # patologia sospetta: tra i pazienti con diagnosi, il sospetto era giusto?
        with_suspect = [r for r in diagnosed if r["sospette"]]
        exact = sum(bool(set(r["sospette"]) & set(r["diagnosi"])) for r in with_suspect)
        family = sum(bool({FAMILY.get(c) for c in r["sospette"]} & {FAMILY.get(c) for c in r["diagnosi"]})
                     for r in with_suspect)
        res["sospetto"][name] = {"diagnosi_con_sospetto": len(with_suspect),
                                 "codice_esatto_%": round(100 * exact / len(with_suspect), 1) if with_suspect else None,
                                 "stessa_famiglia_%": round(100 * family / len(with_suspect), 1) if with_suspect else None}

        to_therapy = [r for r in g if r["terapia"]]
        res["terapia"][name] = {
            "pacemaker": sum(r["terapia"] == "PM" for r in to_therapy),
            "icd": sum(r["terapia"] == "ICD" for r in to_therapy),
            "verso_cied_%": round(100 * len(to_therapy) / len(g), 1),
            "giorni_impianto_terapia": quartiles([r["giorni_terapia"] for r in to_therapy]),
            "giorni_diagnosi_terapia": quartiles([r["giorni_terapia"] - r["giorni_diagnosi"] for r in to_therapy
                                                  if r["giorni_diagnosi"] is not None]),
        }
        res["tipo_diagnosi"][name] = dict(Counter(FAMILY.get(c, c) for r in diagnosed for c in r["diagnosi"]))

    res["espianti"] = dict(Counter(r["motivo_espianto"] for r in rows))

    # --- batteria -----------------------------------------------------------
    by_device = defaultdict(list)
    model_of = {}
    for m in measures:
        if m["term"] == BATTERY:
            by_device[m["device_pseudo"]].append((datetime.fromisoformat(m["session_dtm"]), float(m["value_num"])))
            model_of[m["device_pseudo"]] = m["model"]
    estimated = defaultdict(list)
    for dev, points in by_device.items():
        points.sort()
        (t0, b0), (t1, b1) = points[0], points[-1]
        days = (t1 - t0).days
        if days >= 180 and b0 > b1:                      # almeno 6 mesi di report per stimare il calo
            estimated[model_of[dev]].append(100 / ((b0 - b1) / days) / 365.25)
    observed = defaultdict(list)
    for r in rows:
        if r["motivo_espianto"].startswith("Fine vita della batteria"):
            observed[r["modello"]].append(r["giorni_monitoraggio"] / 365.25)
    for model in sorted(set(estimated) | set(observed)):
        res["batteria"][model] = {
            "dichiarata_anni": NOMINAL_YEARS.get(model),
            "osservata_n": len(observed[model]),
            "osservata_media_anni": round(st.mean(observed[model]), 2) if observed[model] else None,
            "stimata_n": len(estimated[model]),
            "stimata_media_anni": round(st.mean(estimated[model]), 2) if estimated[model] else None,
            "stimata_ds_anni": round(st.stdev(estimated[model]), 2) if len(estimated[model]) > 1 else None,
        }

    # --- burden di FA -------------------------------------------------------
    af_patients = {r["paziente"] for r in rows if "I48" in r["diagnosi"]}
    burden = defaultdict(list)
    for m in measures:
        if m["term"] == AF_BURDEN and m["patient_pseudo"] in af_patients and float(m["value_num"]) > 0:
            burden[m["patient_pseudo"]].append(float(m["value_num"]))
    means = [st.mean(v) for v in burden.values()]
    res["burden_fa"] = {"pazienti_con_fa": len(af_patients), "con_burden_registrato": len(means),
                        "burden_medio_per_paziente_%": {"mediana": round(st.median(means), 2) if means else None,
                                                        "massimo": round(max(means), 2) if means else None}}
    return res


def show(res: dict) -> None:
    c = res["coorte"]
    print(f"Coorte: {c['pazienti']} pazienti, {c['report']} report, età media {c['eta_media']} anni, "
          f"follow-up mediano {c['follow_up_giorni']['mediana']} giorni, {c['ancora_impiantati']} ancora impiantati")
    print("\nResa diagnostica (grezza e cumulativa Kaplan-Meier)")
    for name, r in res["resa"].items():
        t = res["tempo_diagnosi"][name]
        print(f"  {name:30} {r['con_diagnosi']:>3}/{r['pazienti']:<3} = {r['grezza_%']:5.1f}%   "
              f"6m {r['cumulativa_6 mesi_%']:5.1f}%  12m {r['cumulativa_12 mesi_%']:5.1f}%  "
              f"24m {r['cumulativa_24 mesi_%']:5.1f}%  36m {r['cumulativa_36 mesi_%']:5.1f}%   "
              f"tempo mediano {t['mediana']} gg (IQR {t['q1']}-{t['q3']})")
    print("\nPatologia sospetta confermata (pazienti con diagnosi)")
    for name, s in res["sospetto"].items():
        print(f"  {name:30} codice esatto {s['codice_esatto_%']}%   stessa famiglia {s['stessa_famiglia_%']}%")
    print("\nPassaggio a pacemaker / ICD")
    for name, t in res["terapia"].items():
        print(f"  {name:30} PM {t['pacemaker']:>3}  ICD {t['icd']:>3}  ({t['verso_cied_%']}%)   "
              f"diagnosi -> impianto mediana {t['giorni_diagnosi_terapia']['mediana']} gg")
    print("\nBatteria")
    for model, b in res["batteria"].items():
        print(f"  {model:18} dichiarata {b['dichiarata_anni']} anni   osservata {b['osservata_media_anni']} "
              f"(n={b['osservata_n']})   stimata dai report {b['stimata_media_anni']} ± {b['stimata_ds_anni']} "
              f"(n={b['stimata_n']})")
    f = res["burden_fa"]
    print(f"\nFA diagnosticata in {f['pazienti_con_fa']} pazienti; burden medio per paziente: "
          f"mediana {f['burden_medio_per_paziente_%']['mediana']}%")
    print("\nMotivi di espianto:", res["espianti"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="cartella con i CSV esportati (es. export\\simulazione)")
    ap.add_argument("--json", help="salva i risultati anche in questo file JSON")
    args = ap.parse_args()
    results = analyse(Path(args.folder))
    show(results)
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nRisultati salvati in {args.json}")
    sys.exit(0)
